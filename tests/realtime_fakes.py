"""Shared deterministic doubles for the realtime harness tests.

Kept out of the ``test_*`` namespace so pytest does not collect it, and shared
rather than copied so every suite exercises the same retrieval shape.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from pipeline.realtime_plan import BRANCHES
from pipeline.realtime_session import RealtimeAgentSession
from pipeline.search import BranchRequest


def candidate(candidate_id: str, name: str, *, city: str = "Pune") -> SimpleNamespace:
    """One retrieval result, with every field the enrichment step reads."""
    return SimpleNamespace(
        candidate_id=candidate_id,
        full_name=name,
        city=city,
        country="India",
        years_exp=6,
        skills=["python", "postgresql"],
        best_chunk=f"{name} built Python services backed by PostgreSQL.",
        supporting_chunks=[],
        similarity_score=0.8,
        fused_rrf_score=0.8,
        feature_score=82.0,
        rerank_score=None,
        retrieval_paths=["dense", "skill", "keyword"],
        sort_basis="fused_rrf_score",
        explanation={
            "match_tier": "Strong match",
            "match_score": 82,
            "best_evidence": f"{name} built Python services backed by PostgreSQL.",
            "checks": {"required": [], "preferred": []},
            "score_breakdown": [],
        },
        doc_type="resume",
        document_title="Resume",
    )


class FakeEngine:
    """Retrieval double whose results depend on the city filter.

    Two different cities produce two disjoint candidate sets, which is what makes
    "did stale rows leak into the replacement revision" answerable.
    """

    def __init__(self, *, relaxations: list[dict[str, Any]] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.pool = None
        self.relaxations = relaxations or []
        self.fail = False

    async def smart_search(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("retrieval backend unavailable")
        filters = kwargs.get("explicit_filters") or {}
        city = filters.get("city")
        tag = str(city or "any")
        results = [candidate(f"cand-{tag}-1", f"Candidate {tag} one", city=tag)]
        if city is None:
            results.append(candidate("cand-any-2", "Candidate any two", city="Pune"))
        return SimpleNamespace(
            results=results,
            retrieval_policy={
                "keyword": {"action": "run", "reason": "specific_terms"},
                "timings_ms": {"dense_ms": 12.0, "keyword_ms": 4.0, "skill_ms": 3.0, "count_ms": 2.0},
                "row_counts": {
                    "dense_rows": 40,
                    "keyword_rows": 12,
                    "skill_rows": 8,
                    "filtered_candidates": 25,
                },
                "ranking_timings_ms": {"cross_encoder_ms": 9.0},
                "candidate_ids": [result.candidate_id for result in results],
            },
            phase_timings={"query_understanding_ms": 1.0, "retrieval_ms": 15.0, "ranking_ms": 10.0},
            candidate_ids=[result.candidate_id for result in results],
            deferred_candidate_ids=[],
            spec=SimpleNamespace(
                semantic_query=kwargs["query"],
                input_type="query",
                must=SimpleNamespace(
                    city=city, country=None, skills=[], status=["active"],
                    min_years_exp=None, max_years_exp=None,
                ),
                should=SimpleNamespace(skills=[], themes=[], roles=[], locations=[]),
            ),
            total_candidates_scanned=25,
            clarify=None,
            relaxations_applied=list(self.relaxations),
        )


class BranchEngine(FakeEngine):
    """A retrieval double that can also run one branch at a time.

    Having ``retrieve_branch`` is what makes the session take ownership of
    branch lifecycle, so this is the double used by the selective-cancellation
    tests. ``smart_search`` routes its branches through the supplied provider,
    mirroring the engine's real seam — without that, a session-level test would
    report branch activity that never happened.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.branch_calls: list[str] = []

    async def retrieve_branch(self, branch: str, spec: Any, cfg: dict, **kwargs: Any) -> Any:
        self.branch_calls.append(branch)
        return 12 if branch == "sql" else [{"candidate_id": f"{branch}-1"}]

    async def smart_search(self, **kwargs: Any) -> SimpleNamespace:
        provider = kwargs.get("branch_provider")
        if provider is not None:
            for branch in BRANCHES:
                await provider(BranchRequest(
                    branch=branch,
                    spec=None,
                    cfg={},
                    filter_sql="TRUE",
                    filter_params=[],
                ))
        return await super().smart_search(**kwargs)


def make_session(engine: Any | None = None, *, session_id: str = "test") -> RealtimeAgentSession:
    return RealtimeAgentSession(engine if engine is not None else FakeEngine(), session_id=session_id)
