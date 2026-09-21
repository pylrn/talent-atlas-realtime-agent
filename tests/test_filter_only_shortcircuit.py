"""Empty-query + chip-only searches must skip the LLM planner.

The old behaviour was: empty query → planner gets called with "" → returns a
low-confidence clarify spec → confidence gate short-circuits to 'clarify' and
the chips are ignored. This test pins the new behaviour where an empty query
with structured filters goes straight to filter_only.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from pipeline.search import (
    HybridSearchEngine,
    _has_filter_values,
    _spec_from_explicit_filters,
)


# ── Pure helpers (no DB / no LLM) ────────────────────────────────────────────


def test_has_filter_values_none_or_empty():
    assert _has_filter_values(None) is False
    assert _has_filter_values({}) is False


def test_has_filter_values_status_only_does_not_count():
    # Status defaults to ['active'] for every search; on its own it is not
    # a recruiter-supplied intent and should not bypass the LLM.
    assert _has_filter_values({"status": ["active"]}) is False


def test_has_filter_values_with_skills():
    assert _has_filter_values({"skills": ["crm-software"]}) is True


def test_has_filter_values_with_country():
    assert _has_filter_values({"country": "india"}) is True


def test_has_filter_values_with_min_years_exp():
    assert _has_filter_values({"min_years_exp": 3}) is True


def test_has_filter_values_empty_values_dont_count():
    assert _has_filter_values({"skills": [], "country": "", "min_salary": None}) is False


def test_spec_from_explicit_filters_builds_filters_only_spec():
    spec = _spec_from_explicit_filters({
        "skills": ["python", "docker"],
        "country": "India",
        "min_years_exp": 5,
        "skill_weights": {"python": 100, "docker": 25},
    })
    assert spec.input_type == "filters_only"
    assert spec.intent == "candidate_filter"
    assert spec.is_filter_only() is True
    assert spec.must.skills == ["python", "docker"]
    assert spec.must.country == "india"
    assert spec.must.min_years_exp == 5
    assert spec.skill_weights == {"python": 1.0, "docker": 0.25}
    # Filter-only specs come from the user directly, so confidence is high
    # and we should never report planner fallback for them.
    assert spec.confidence == 1.0
    assert spec.used_fallback is False


# ── End-to-end: smart_search bypasses the planner ────────────────────────────


def _make_engine_without_db() -> HybridSearchEngine:
    engine = HybridSearchEngine.__new__(HybridSearchEngine)
    engine.pool = SimpleNamespace()
    engine.embedder = SimpleNamespace()
    engine._reranker_cache = {}
    return engine


def test_smart_search_skips_planner_when_query_empty_and_filters_present():
    engine = _make_engine_without_db()

    async def _fake_filter_only(spec, top_k):
        # Pretend the SQL returned 2 candidates; we only need to confirm the
        # path was taken.
        return ["c1", "c2"]

    async def _fake_load_prefs(_):
        return None

    engine._filter_only_search = _fake_filter_only  # type: ignore[assignment]
    engine._load_recruiter_prefs = _fake_load_prefs  # type: ignore[assignment]

    plan_mock = AsyncMock(side_effect=AssertionError("planner must not be called"))
    with patch("pipeline.search._plan", plan_mock):
        resp = asyncio.run(engine.smart_search(
            query="",
            jd=None,
            explicit_filters={"skills": ["crm-software"], "skill_weights": {"crm-software": 50}},
            mode="quality",
            top_k=10,
        ))

    plan_mock.assert_not_called()
    assert resp.spec is not None
    assert resp.spec.intent == "candidate_filter"
    assert resp.spec.must.skills == ["crm-software"]
    assert resp.results == ["c1", "c2"]
    # Filter-only short-circuit never triggers clarify.
    assert resp.clarify is None


def test_smart_search_still_calls_planner_when_query_provided():
    """Sanity check: a real free-text query must still hit the planner."""
    engine = _make_engine_without_db()

    async def _fake_load_prefs(_):
        return None

    engine._load_recruiter_prefs = _fake_load_prefs  # type: ignore[assignment]

    # Build a minimal spec the rest of the pipeline can swallow.
    sentinel_spec = _spec_from_explicit_filters({"skills": ["python"]})
    sentinel_spec.intent = "candidate_search"  # force the full-pipeline branch
    sentinel_spec.input_type = "query"
    sentinel_spec.semantic_query = "python developer"
    plan_mock = AsyncMock(return_value=sentinel_spec)

    async def _fake_full(spec, cfg, top_k, recruiter_prefs, recruiter_id, recruiter_profile, phase1_ms, spec_dict, prefetched=None):
        from pipeline.search import SearchResponse
        return SearchResponse(results=["c1"], spec=spec, spec_dict=spec_dict)

    engine._full_pipeline = _fake_full  # type: ignore[assignment]

    with patch("pipeline.search._plan", plan_mock):
        resp = asyncio.run(engine.smart_search(
            query="python developer",
            jd=None,
            explicit_filters=None,
            mode="quality",
            top_k=10,
        ))

    plan_mock.assert_called_once()
    assert resp.results == ["c1"]
