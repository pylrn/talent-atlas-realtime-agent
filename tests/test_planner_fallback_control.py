import pytest

from pipeline import settings
from pipeline import cache as _cache
from pipeline import planner


QUERY = (
    "looking for sales executive with expertise in finance domain "
    "with minimum experience at least 1 year"
)


@pytest.mark.asyncio
async def test_planner_raises_when_llm_fails_and_fallback_disabled(monkeypatch):
    async def fake_call_llm(*args, **kwargs):
        return None

    monkeypatch.setattr(planner, "_call_llm", fake_call_llm)

    with pytest.raises(RuntimeError, match="LLM planner failed"):
        await planner.plan(
            raw_input="Find an HR assistant",
            cfg={
                "use_cache": False,
                "use_llm_planner": True,
                "use_planner_fallback": False,
            },
        )


@pytest.mark.asyncio
async def test_no_llm_planner_extracts_finance_domain_skill():
    spec = await planner.plan(
        raw_input=QUERY,
        cfg={
            "use_cache": False,
            "use_llm_planner": False,
            "use_planner_fallback": True,
        },
    )

    assert "finance" in spec.must.skills
    assert spec.must.min_years_exp == 1


@pytest.mark.asyncio
async def test_no_llm_planner_does_not_reuse_llm_plan_cache(monkeypatch):
    _cache.clear_all()

    async def fake_call_llm(*args, **kwargs):
        return {
            "input_type": "query",
            "intent": "candidate_search",
            "must": {
                "skills": [],
                "country": None,
                "city": None,
                "min_years_exp": None,
                "max_years_exp": None,
                "applied_role": None,
                "min_salary": None,
                "max_salary": None,
                "status": ["active"],
            },
            "should": {
                "skills": ["llm"],
                "themes": ["hosting"],
                "locations": [],
                "roles": [],
            },
            "must_not": {"skills": [], "status": [], "companies": []},
            "semantic_query": "llm hosting specialist",
            "hyde_profile": None,
            "lexical_terms": ["llm", "hosting"],
            "search_targets": ["candidate_profile", "resume_chunks"],
            "confidence": 0.9,
            "clarify": None,
        }

    monkeypatch.setattr(planner, "_call_llm", fake_call_llm)

    raw = "someone with linux in profile and hosting llm stuff"
    llm_spec = await planner.plan(
        raw_input=raw,
        cfg={
            "use_cache": True,
            "use_llm_planner": True,
            "use_planner_fallback": True,
            "use_fallback_repair": False,
            "llm_provider": "test-provider",
            "llm_model": "test-model",
        },
    )
    assert llm_spec.used_fallback is False
    assert llm_spec.should.skills == ["llm"]

    no_llm_spec = await planner.plan(
        raw_input=raw,
        cfg={
            "use_cache": True,
            "use_llm_planner": False,
            "use_planner_fallback": True,
            "llm_provider": "test-provider",
            "llm_model": "test-model",
        },
    )

    assert no_llm_spec.used_fallback is True
    assert no_llm_spec.should.skills == []
    assert "linux" in no_llm_spec.must.skills

    _cache.clear_all()


@pytest.mark.asyncio
async def test_llm_failure_fallback_extracts_finance_domain_skill(monkeypatch):
    async def fake_call_llm(*args, **kwargs):
        return None

    monkeypatch.setattr(planner, "_call_llm", fake_call_llm)

    spec = await planner.plan(
        raw_input=QUERY,
        cfg={
            "use_cache": False,
            "use_llm_planner": True,
            "use_planner_fallback": True,
        },
    )

    assert "finance" in spec.must.skills
    assert spec.must.min_years_exp == 1
    assert spec.used_fallback is True


@pytest.mark.asyncio
async def test_llm_repair_adds_finance_domain_skill_when_llm_omits_it(monkeypatch):
    async def fake_call_llm(*args, **kwargs):
        return {
            "input_type": "query",
            "intent": "candidate_search",
            "must": {
                "skills": [],
                "country": None,
                "city": None,
                "min_years_exp": 1,
                "max_years_exp": None,
                "applied_role": None,
                "min_salary": None,
                "max_salary": None,
                "status": ["active"],
            },
            "should": {"skills": [], "themes": [], "locations": [], "roles": []},
            "must_not": {"skills": [], "status": [], "companies": []},
            "semantic_query": QUERY,
            "hyde_profile": None,
            "lexical_terms": [],
            "search_targets": ["candidate_profile", "resume_chunks"],
            "confidence": 0.9,
            "clarify": None,
        }

    monkeypatch.setattr(planner, "_call_llm", fake_call_llm)

    spec = await planner.plan(
        raw_input=QUERY,
        cfg={
            "use_cache": False,
            "use_llm_planner": True,
            "use_planner_fallback": True,
            "use_fallback_repair": True,
        },
    )

    assert "finance" in spec.must.skills
    assert spec.must.min_years_exp == 1
    assert spec.used_fallback is False


@pytest.mark.asyncio
async def test_gemini_planner_requests_json_with_enough_output_tokens(monkeypatch):
    captured = {}

    class FakeModels:
        def generate_content(self, model, contents, config):
            captured.update(model=model, contents=contents, config=config)

            class Response:
                text = '{"input_type":"query","semantic_query":"python engineer","confidence":0.9}'

            return Response()

    class FakeClient:
        def __init__(self, api_key):
            self.models = FakeModels()

    import google.genai

    monkeypatch.setattr(google.genai, "Client", FakeClient)
    monkeypatch.setattr(settings, "google_api_key", "configured-google-key", raising=False)

    planned = await planner._call_gemini(
        "python engineer",
        model_override="gemini-3.5-flash",
        thinking_level="high",
    )

    assert planned["semantic_query"] == "python engineer"
    assert captured["config"]["response_mime_type"] == "application/json"
    assert captured["config"]["max_output_tokens"] >= 2048
