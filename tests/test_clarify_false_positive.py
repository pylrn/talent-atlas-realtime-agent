"""Defense-in-depth for the LLM over-triggering the protected-class clarify.

Two layers tested:

  1. Validator override: when the LLM raises the protected-characteristics
     clarify but the original input has no actual trigger phrase, the
     validator clears the clarify and restores confidence.

  2. Partial-extraction fallback (smart_search): when clarify fires anyway
     and the spec carries real extracted filters, the pipeline runs the
     search and surfaces clarify as a soft notice instead of blocking.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pipeline.spec import MustFilters
from pipeline.validator import (
    _PROTECTED_CHAR_CLARIFY,
    _input_requests_protected_field,
    validate_spec,
)
from pipeline.search import HybridSearchEngine, _has_meaningful_must_values


# ── Trigger-phrase detection ─────────────────────────────────────────────────


def test_pronouns_are_not_protected_triggers():
    assert _input_requests_protected_field("he should be from new york") is False
    assert _input_requests_protected_field("she has 5 years of experience") is False
    assert _input_requests_protected_field("they led a team of 10") is False


def test_seniority_words_are_not_protected_triggers():
    assert _input_requests_protected_field("senior engineer with django") is False
    assert _input_requests_protected_field("junior developer") is False


def test_real_gender_filter_is_a_trigger():
    assert _input_requests_protected_field("only male engineers") is True
    assert _input_requests_protected_field("no women") is True
    assert _input_requests_protected_field("prefer female candidates") is True


def test_real_age_filter_is_a_trigger():
    assert _input_requests_protected_field("young candidates only") is True
    assert _input_requests_protected_field("over 50 years old") is True


def test_real_religion_filter_is_a_trigger():
    assert _input_requests_protected_field("must be hindu") is True
    assert _input_requests_protected_field("exclude muslim applicants") is True


def test_marital_pregnancy_disability_triggers():
    assert _input_requests_protected_field("must be married")    is True
    assert _input_requests_protected_field("not pregnant")        is True
    assert _input_requests_protected_field("able-bodied only")    is True


# ── Validator override ───────────────────────────────────────────────────────


def _build_raw(clarify_text: str, country: str | None = "us", confidence: float = 0.4):
    return {
        "input_type":   "jd",
        "intent":       "candidate_search",
        "must":         {
            "skills": [], "country": country, "city": None,
            "min_years_exp": None, "max_years_exp": None, "applied_role": None,
            "min_salary": None, "max_salary": None, "status": ["active"],
        },
        "should":       {"skills": [], "themes": [], "locations": [], "roles": []},
        "must_not":     {"skills": [], "status": [], "companies": []},
        "semantic_query": "warehouse coordinator from new york",
        "hyde_profile": None,
        "lexical_terms": [],
        "search_targets": ["candidate_profile", "resume_chunks"],
        "confidence":   confidence,
        "clarify":      clarify_text,
    }


def test_validator_clears_clarify_when_input_has_no_trigger():
    spec = validate_spec(
        _build_raw(_PROTECTED_CHAR_CLARIFY),
        original_input="warehouse coordinator who should also be from new york",
    )
    assert spec.clarify is None
    assert spec.confidence >= 0.7
    assert spec.must.country == "us"


def test_validator_keeps_clarify_when_input_actually_requests_protected_field():
    spec = validate_spec(
        _build_raw(_PROTECTED_CHAR_CLARIFY),
        original_input="only male candidates please",
    )
    assert spec.clarify == _PROTECTED_CHAR_CLARIFY


def test_validator_keeps_unrelated_clarify_messages():
    """The override targets the protected-char message specifically. Other
    clarify reasons (e.g. ambiguous query) must still go through."""
    spec = validate_spec(
        _build_raw("Did you mean Jane or John Smith?"),
        original_input="smith",
    )
    assert spec.clarify == "Did you mean Jane or John Smith?"


# ── Partial-extraction fallback ──────────────────────────────────────────────


def test_has_meaningful_must_values_status_only_is_not_meaningful():
    must = MustFilters()  # default status=['active']
    assert _has_meaningful_must_values(must) is False


def test_has_meaningful_must_values_with_country():
    assert _has_meaningful_must_values(MustFilters(country="us")) is True


def test_has_meaningful_must_values_with_skills():
    assert _has_meaningful_must_values(MustFilters(skills=["python"])) is True


def test_has_meaningful_must_values_with_years_zero():
    # 0 is a real lower bound and should count, not be treated as falsy.
    assert _has_meaningful_must_values(MustFilters(min_years_exp=0)) is True


# ── End-to-end: smart_search demotes clarify when filters survived ───────────


def _make_engine() -> HybridSearchEngine:
    engine = HybridSearchEngine.__new__(HybridSearchEngine)
    engine.pool = SimpleNamespace()
    engine.embedder = SimpleNamespace()
    engine._reranker_cache = {}
    return engine


def test_smart_search_demotes_clarify_to_notice_when_must_has_filters():
    """If LLM raised clarify but country=us was extracted, run the search
    and pass clarify through as a soft notice on the response."""
    from pipeline.spec import (
        CanonicalSearchSpec, MustFilters, ShouldFilters, MustNotFilters,
    )
    from unittest.mock import AsyncMock, patch

    engine = _make_engine()

    async def _fake_load_prefs(_): return None
    engine._load_recruiter_prefs = _fake_load_prefs  # type: ignore

    async def _fake_filter_only(spec, top_k):
        # Confirm the spec at this point has clarify cleared but the
        # extracted country survived.
        assert spec.clarify is None
        assert spec.must.country == "us"
        return ["c1", "c2"]
    engine._filter_only_search = _fake_filter_only  # type: ignore

    bad_spec = CanonicalSearchSpec(
        input_type="jd",
        intent="candidate_filter",
        must=MustFilters(country="us"),
        should=ShouldFilters(),
        must_not=MustNotFilters(),
        semantic_query="",  # filter-only path
        confidence=0.4,
        clarify=_PROTECTED_CHAR_CLARIFY,
    )
    plan_mock = AsyncMock(return_value=bad_spec)

    with patch("pipeline.search._plan", plan_mock):
        resp = asyncio.run(engine.smart_search(
            query="he should be from us",
            jd=None,
            explicit_filters=None,
            mode="quality",
            top_k=10,
        ))

    # Results came through, AND the clarify text is preserved on the response
    # as a soft notice for the UI to render.
    assert resp.results == ["c1", "c2"]
    assert resp.clarify == _PROTECTED_CHAR_CLARIFY


def test_smart_search_still_clarifies_when_no_filters_extracted():
    """If clarify fires AND no real filters were extracted, the clarify
    must still block (no partial-extraction fallback to fall back to)."""
    from pipeline.spec import (
        CanonicalSearchSpec, MustFilters, ShouldFilters, MustNotFilters,
    )
    from unittest.mock import AsyncMock, patch

    engine = _make_engine()
    async def _fake_load_prefs(_): return None
    engine._load_recruiter_prefs = _fake_load_prefs  # type: ignore

    blank_spec = CanonicalSearchSpec(
        input_type="query",
        intent="candidate_search",
        must=MustFilters(),  # nothing useful
        should=ShouldFilters(),
        must_not=MustNotFilters(),
        confidence=0.2,
        clarify="What kind of engineer are you looking for?",
    )
    plan_mock = AsyncMock(return_value=blank_spec)

    with patch("pipeline.search._plan", plan_mock):
        resp = asyncio.run(engine.smart_search(
            query="engineer",
            jd=None,
            explicit_filters=None,
            mode="quality",
            top_k=10,
        ))

    assert resp.results == []
    assert resp.clarify == "What kind of engineer are you looking for?"
