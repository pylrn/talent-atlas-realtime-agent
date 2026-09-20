"""Tests for using deterministic fallback as a non-overriding repair pass."""

import pytest

from pipeline import planner
from pipeline.spec import CanonicalSearchSpec, MustFilters


def test_fallback_repair_appends_missing_lists_without_overriding_scalars():
    spec = CanonicalSearchSpec(
        input_type="query",
        intent="candidate_search",
        must=MustFilters(
            skills=["python"],
            city="mumbai",
            min_years_exp=3,
            status=["archived"],
        ),
        semantic_query="python backend engineer",
        lexical_terms=["python"],
        search_targets=["candidate_profile"],
        confidence=0.91,
        clarify=None,
    )
    fallback = CanonicalSearchSpec(
        input_type="query",
        intent="candidate_search",
        must=MustFilters(
            skills=["python", "fastapi"],
            country="india",
            city="bangalore",
            min_years_exp=5,
            status=["active"],
        ),
        semantic_query="python fastapi bangalore engineer 5 years",
        lexical_terms=["python", "fastapi"],
        search_targets=["candidate_profile", "resume_chunks"],
        confidence=0.5,
        used_fallback=True,
    )

    repaired = planner._repair_spec_with_fallback(spec, fallback)

    assert repaired.must.skills == ["python", "fastapi"]
    assert repaired.must.country == "india"
    assert repaired.must.city == "mumbai"
    assert repaired.must.min_years_exp == 3
    assert repaired.must.status == ["archived"]
    assert repaired.semantic_query == "python backend engineer"
    assert repaired.search_targets == ["candidate_profile", "resume_chunks"]
    assert repaired.lexical_terms == ["python", "fastapi"]
    assert repaired.confidence == 0.91
    assert repaired.used_fallback is False


@pytest.mark.asyncio
async def test_plan_repairs_llm_spec_with_fallback_when_llm_misses_skill(monkeypatch):
    async def fake_call_llm(*args, **kwargs):
        return {
            "input_type": "query",
            "intent": "candidate_search",
            "must": {
                "skills": ["python"],
                "country": None,
                "city": "bangalore",
                "min_years_exp": 5,
                "status": ["active"],
            },
            "should": {"skills": [], "themes": [], "locations": [], "roles": []},
            "must_not": {"skills": [], "status": [], "companies": []},
            "semantic_query": "python backend engineer in bangalore",
            "hyde_profile": None,
            "lexical_terms": ["python"],
            "search_targets": ["candidate_profile"],
            "confidence": 0.9,
            "clarify": None,
        }

    monkeypatch.setattr(planner, "_call_llm", fake_call_llm)

    spec = await planner.plan(
        raw_input="python fastapi engineer in bangalore with 5 years",
        cfg={
            "use_cache": False,
            "use_llm_planner": True,
            "use_fallback_repair": True,
        },
    )

    assert spec.used_fallback is False
    assert spec.must.skills == ["python", "fastapi"]
    assert spec.must.city == "bangalore"
    assert spec.must.min_years_exp == 5


@pytest.mark.asyncio
async def test_plan_exposes_llm_error_when_fallback_handles_failure(monkeypatch):
    async def fake_call_llm(*args, **kwargs):
        raise RuntimeError("Gemini API key not valid")

    monkeypatch.setattr(planner, "_call_llm", fake_call_llm)

    spec = await planner.plan(
        raw_input="python fastapi engineer in bangalore with 5 years",
        cfg={
            "use_cache": False,
            "use_llm_planner": True,
            "use_planner_fallback": True,
        },
    )

    assert spec.used_fallback is True
    assert spec.planner_error == "Gemini API key not valid"
    assert spec.to_dict()["planner_error"] == "Gemini API key not valid"
