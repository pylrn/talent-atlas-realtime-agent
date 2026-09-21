"""The prefetch seam: reuse rows for a branch whose inputs did not change.

Without this seam an interruptible caller can only choose between re-running
every branch or discarding the revision entirely. With it, a revision that
changes the semantic query can keep the SQL, keyword and skill rows it already
paid for and re-query only the dense branch.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

from pipeline.modes import get_config
from pipeline.search import HybridSearchEngine, _spec_from_explicit_filters


class _FakeEmbedder:
    async def embed(self, texts: Any, **kwargs: Any) -> list[list[float]]:
        return [[0.0] * 4 for _ in texts]


def _engine() -> HybridSearchEngine:
    return HybridSearchEngine(pool=object(), embedder=_FakeEmbedder())


def _cfg() -> dict:
    cfg = get_config("no-llm")
    cfg["use_auto_relax"] = False
    cfg["use_cache"] = False
    cfg["use_retrieval_cache"] = False
    cfg["use_cross_encoder"] = False
    cfg["use_mmr"] = False
    return cfg


def _spec():
    return _spec_from_explicit_filters({"skills": ["python"], "city": "pune"})


def _run(engine: HybridSearchEngine, cfg: dict, *, prefetched=None):
    return asyncio.run(
        engine._full_pipeline(_spec(), cfg, 5, prefetched=prefetched)
    )


def _patched_retrievers():
    return (
        patch("pipeline.search.retrieve_dense", new=AsyncMock(return_value=[])),
        patch("pipeline.search.retrieve_bm25", new=AsyncMock(return_value=[])),
        patch("pipeline.search.retrieve_skill", new=AsyncMock(return_value=[])),
        patch.object(HybridSearchEngine, "_count_filtered", new=AsyncMock(return_value=0)),
    )


def test_a_cold_search_runs_every_branch():
    dense, bm25, skill, count = _patched_retrievers()
    with dense as dense_mock, bm25, skill, count as count_mock:
        response = _run(_engine(), _cfg())

    assert dense_mock.await_count == 1
    assert count_mock.await_count == 1
    # A cold search reports no prefetching, so nothing is claimed as reused.
    assert "prefetched_branches" not in (response.retrieval_policy or {})


def test_a_prefetched_branch_is_not_queried_again():
    dense, bm25, skill, count = _patched_retrievers()
    with dense as dense_mock, bm25, skill as skill_mock, count as count_mock:
        response = _run(_engine(), _cfg(), prefetched={"vector": []})

    assert dense_mock.await_count == 0, "the dense branch was re-queried despite a prefetch"
    # Every other branch still runs. The keyword branch is absent from these
    # assertions because it is policy-gated: with zero filtered candidates the
    # keyword policy skips it, which is unrelated to prefetching.
    assert count_mock.await_count == 1
    assert skill_mock.await_count == 1
    assert (response.retrieval_policy or {})["prefetched_branches"] == ["vector"]


def test_several_prefetched_branches_are_all_skipped():
    dense, bm25, skill, count = _patched_retrievers()
    with dense as dense_mock, bm25 as bm25_mock, skill as skill_mock, count as count_mock:
        response = _run(
            _engine(),
            _cfg(),
            prefetched={"vector": [], "bm25": [], "skills": []},
        )

    assert dense_mock.await_count == 0
    assert bm25_mock.await_count == 0
    assert skill_mock.await_count == 0
    assert count_mock.await_count == 1
    assert (response.retrieval_policy or {})["prefetched_branches"] == [
        "bm25",
        "skills",
        "vector",
    ]


def test_a_prefetched_branch_costs_no_retrieval_time():
    dense, bm25, skill, count = _patched_retrievers()
    with dense, bm25, skill, count:
        response = _run(_engine(), _cfg(), prefetched={"vector": []})

    policy = response.retrieval_policy or {}
    # Reused work must not be billed as fresh work.
    assert (policy.get("timings_ms") or {}).get("dense_ms") == 0.0
    assert (policy.get("row_counts") or {}).get("dense_rows") == 0


def test_an_unknown_prefetch_key_is_ignored():
    """A key that matches no branch must not silently disable a real branch."""
    dense, bm25, skill, count = _patched_retrievers()
    with dense as dense_mock, bm25, skill, count:
        response = _run(_engine(), _cfg(), prefetched={"not_a_branch": []})

    assert dense_mock.await_count == 1
    assert "prefetched_branches" not in (response.retrieval_policy or {})


def test_a_prefetched_count_short_circuits_the_sql_branch():
    """The sql branch retrieves a count rather than rows, so its prefetched
    value is the integer itself."""
    dense, bm25, skill, count = _patched_retrievers()
    with dense as dense_mock, bm25, skill, count as count_mock:
        response = _run(_engine(), _cfg(), prefetched={"sql": 42})

    assert count_mock.await_count == 0
    assert dense_mock.await_count == 1
    policy = response.retrieval_policy or {}
    assert policy["prefetched_branches"] == ["sql"]
    assert (policy.get("row_counts") or {}).get("filtered_candidates") == 42


def test_a_branch_provider_owns_branch_execution():
    """The provider hook is what lets an interruptible session run branches as
    separate tasks and cancel only the invalidated ones."""
    seen: list[str] = []

    async def provider(request):
        seen.append(request.branch)
        return 7 if request.branch == "sql" else []

    response = asyncio.run(_engine()._full_pipeline(_spec(), _cfg(), 5, branch_provider=provider))

    # The keyword branch is policy-gated, so it is not asserted on here.
    assert "vector" in seen
    assert "skills" in seen
    assert "sql" in seen
    assert (response.retrieval_policy or {})["provided_branches"] == sorted(seen)


def test_a_branch_provider_takes_precedence_over_prefetched_rows():
    """Two reuse mechanisms must not both claim a branch."""
    seen: list[str] = []

    async def provider(request):
        seen.append(request.branch)
        return 3 if request.branch == "sql" else []

    response = asyncio.run(
        _engine()._full_pipeline(
            _spec(), _cfg(), 5,
            prefetched={"vector": []},
            branch_provider=provider,
        )
    )

    assert "vector" in seen
    policy = response.retrieval_policy or {}
    assert "prefetched_branches" not in policy
    assert policy["provided_branches"] == sorted(seen)
