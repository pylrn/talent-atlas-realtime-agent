"""End-to-end integration: stubbed Langfuse client receives the
observations we expect from a planner run."""

from unittest.mock import MagicMock, patch

import pytest

from pipeline import observability as obs
import pipeline.planner as planner_mod


@pytest.mark.asyncio
async def test_planner_creates_spans_for_each_phase(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    fake_client = MagicMock()
    fake_span   = MagicMock()
    fake_span.__enter__ = lambda self: self
    fake_span.__exit__  = lambda *a: False
    fake_client.start_as_current_observation.return_value = fake_span
    monkeypatch.setattr(obs, "_client", fake_client)

    async def fake_call_llm(*a, **kw):
        return {
            "input_type": "query", "intent": "candidate_search", 
            "must": {"skills": [], "country": None, "city": None,
                     "min_years_exp": None, "max_years_exp": None,
                     "applied_role": None, "min_salary": None,
                     "max_salary": None, "status": ["active"]},
            "should":   {"skills": [], "themes": [], "locations": [], "roles": []},
            "must_not": {"skills": [], "status": [], "companies": []},
            "semantic_query": "x", "hyde_profile": None, "lexical_terms": [],
            "search_targets": ["candidate_profile"],
            "confidence": 0.8, "clarify": None,
        }
    monkeypatch.setattr(planner_mod, "_call_llm", fake_call_llm)

    await planner_mod.plan("I need someone who can build python backends with experience", cfg={"use_cache": True})

    span_names = [c.kwargs.get("name") or c.args[1]
                  for c in fake_client.start_as_current_observation.call_args_list]
    assert "search.plan.cache_lookup" in span_names
    assert "search.plan.generate" in span_names
    assert "search.plan.validate" in span_names
    assert "search.plan.normalize" in span_names
