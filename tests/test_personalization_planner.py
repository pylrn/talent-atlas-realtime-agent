"""Planner-side tests for personalization hints (soft preferences injected
into the LLM prompt + an optional offer field on the spec)."""

import pytest

from pipeline.spec import CanonicalSearchSpec
from pipeline.validator import validate_spec


def test_spec_defaults_personalization_offer_to_none():
    spec = CanonicalSearchSpec(input_type="query", intent="candidate_search")
    assert spec.personalization_offer is None


def test_spec_to_dict_includes_personalization_offer():
    spec = CanonicalSearchSpec(
        input_type="query", intent="candidate_search",
        personalization_offer={"hint": "user likes builders", "question": "Remember?"},
    )
    d = spec.to_dict()
    assert d["personalization_offer"] == {
        "hint": "user likes builders",
        "question": "Remember?",
    }


def _raw(extra_top=None):
    base = {
        "input_type": "query",
        "intent": "candidate_search",
        "must": {"skills": [], "country": None, "city": None,
                 "min_years_exp": None, "max_years_exp": None,
                 "applied_role": None, "min_salary": None, "max_salary": None,
                 "status": ["active"]},
        "should":   {"skills": [], "themes": [], "locations": [], "roles": []},
        "must_not": {"skills": [], "status": [], "companies": []},
        "semantic_query": "x",
        "hyde_profile": None,
        "lexical_terms": [],
        "search_targets": ["candidate_profile"],
        "confidence": 0.8,
        "clarify": None,
    }
    if extra_top:
        base.update(extra_top)
    return base


def test_validator_parses_personalization_offer():
    raw = _raw({"personalization_offer": {
        "hint": "user likes startup builders",
        "question": "Remember?"}})
    spec = validate_spec(raw, original_input="python engineer")
    assert spec.personalization_offer == {
        "hint": "user likes startup builders",
        "question": "Remember?",
    }


def test_validator_handles_missing_personalization_offer():
    spec = validate_spec(_raw(), original_input="x")
    assert spec.personalization_offer is None


def test_validator_drops_malformed_personalization_offer():
    raw = _raw({"personalization_offer": "not a dict"})
    spec = validate_spec(raw, original_input="x")
    assert spec.personalization_offer is None


from pipeline.planner import _spec_from_dict


def test_spec_from_dict_restores_personalization_offer():
    d = _raw({"personalization_offer": {"hint": "h", "question": "q?"}})
    spec = _spec_from_dict(d)
    assert spec.personalization_offer == {"hint": "h", "question": "q?"}


def test_spec_from_dict_handles_missing_personalization_offer():
    spec = _spec_from_dict(_raw())
    assert spec.personalization_offer is None


from pipeline.hints import build_hints_block, hints_cache_tag


def test_build_hints_block_returns_empty_when_disabled():
    assert build_hints_block([{"text": "x"}], enabled=False) == ""


def test_build_hints_block_returns_empty_when_no_hints():
    assert build_hints_block([], enabled=True) == ""
    assert build_hints_block(None, enabled=True) == ""


def test_build_hints_block_formats_when_enabled_and_present():
    hints = [{"text": "user often wants startup experience"},
             {"text": "prefers Bangalore-based candidates"}]
    block = build_hints_block(hints, enabled=True)
    assert "ABOUT THIS USER" in block
    assert "user often wants startup experience" in block
    assert "prefers Bangalore-based candidates" in block
    assert "MUST NEVER be added to must.*" in block
    assert block.endswith("\n\n")


def test_hints_cache_tag_empty_when_disabled_or_empty():
    assert hints_cache_tag([], enabled=True) == ""
    assert hints_cache_tag([{"text": "x"}], enabled=False) == ""


def test_hints_cache_tag_stable_and_sensitive_to_text_only():
    a = [{"text": "alpha", "source": "manual",    "created_at": "t1"}]
    b = [{"text": "alpha", "source": "suggested", "created_at": "t2"}]
    c = [{"text": "beta",  "source": "manual",    "created_at": "t1"}]
    assert hints_cache_tag(a, enabled=True) == hints_cache_tag(b, enabled=True)
    assert hints_cache_tag(a, enabled=True) != hints_cache_tag(c, enabled=True)
    two_one = [{"text": "alpha"}, {"text": "beta"}]
    one_two = [{"text": "beta"},  {"text": "alpha"}]
    assert hints_cache_tag(two_one, enabled=True) == hints_cache_tag(one_two, enabled=True)
    assert hints_cache_tag(a, enabled=True).startswith("|h:")
    assert len(hints_cache_tag(a, enabled=True)) == 3 + 12


from pipeline.prompts import PLANNER_SYSTEM_PROMPT


def test_prompt_includes_personalization_offer_in_output_schema():
    assert '"personalization_offer"' in PLANNER_SYSTEM_PROMPT


def test_prompt_includes_personalization_offer_rule():
    assert "PERSONALIZATION_OFFER" in PLANNER_SYSTEM_PROMPT
    assert "roughly 1 in 5 searches" in PLANNER_SYSTEM_PROMPT


def test_call_llm_accepts_system_prompt_prefix_kwarg():
    """_call_llm and provider wrappers must accept system_prompt_prefix."""
    import inspect
    from pipeline.planner import _call_llm, _call_openai, _call_gemini, _call_groq
    for fn in (_call_llm, _call_openai, _call_gemini, _call_groq):
        params = inspect.signature(fn).parameters
        assert "system_prompt_prefix" in params, (
            f"{fn.__name__} missing system_prompt_prefix")


@pytest.mark.asyncio
async def test_plan_builds_and_passes_hints_prefix(monkeypatch):
    """plan() must read hints from cfg and pass a prefix to _call_llm."""
    import pipeline.planner as planner_mod

    captured = {}

    async def fake_call_llm(sanitized_input, provider_override=None,
                            model_override=None, thinking_level=None,
                            system_prompt_prefix="", sys_prompt_base=None):
        captured["prefix"] = system_prompt_prefix
        return _raw()

    monkeypatch.setattr(planner_mod, "_call_llm", fake_call_llm)
    cfg = {
        "use_cache": False,
        "personalization_enabled": True,
        "personalization_hints": [{"text": "user likes startup builders"}],
    }
    await planner_mod.plan("I am looking for a python engineer with experience", cfg=cfg)
    assert "ABOUT THIS USER" in captured["prefix"]
    assert "user likes startup builders" in captured["prefix"]


@pytest.mark.asyncio
async def test_plan_omits_hints_prefix_when_disabled(monkeypatch):
    import pipeline.planner as planner_mod
    captured = {}
    async def fake_call_llm(sanitized_input, provider_override=None,
                            model_override=None, thinking_level=None,
                            system_prompt_prefix="", sys_prompt_base=None):
        captured["prefix"] = system_prompt_prefix
        return _raw()
    monkeypatch.setattr(planner_mod, "_call_llm", fake_call_llm)
    cfg = {
        "use_cache": False,
        "personalization_enabled": False,
        "personalization_hints": [{"text": "anything"}],
    }
    await planner_mod.plan("I am looking for a python engineer with experience", cfg=cfg)
    assert captured["prefix"] == ""


@pytest.mark.asyncio
async def test_plan_omits_hints_prefix_when_empty(monkeypatch):
    import pipeline.planner as planner_mod
    captured = {}
    async def fake_call_llm(sanitized_input, provider_override=None,
                            model_override=None, thinking_level=None,
                            system_prompt_prefix="", sys_prompt_base=None):
        captured["prefix"] = system_prompt_prefix
        return _raw()
    monkeypatch.setattr(planner_mod, "_call_llm", fake_call_llm)
    cfg = {"use_cache": False, "personalization_enabled": True,
           "personalization_hints": []}
    await planner_mod.plan("I am looking for a python engineer with experience", cfg=cfg)
    assert captured["prefix"] == ""


@pytest.mark.asyncio
async def test_cache_key_changes_with_hints(monkeypatch):
    """Identical inputs but different hints → different cache keys.
       Identical inputs with empty hints → same key as today (no |h: suffix)."""
    import pipeline.planner as planner_mod
    import pipeline.cache as cache_mod

    seen_keys = []
    async def spy_get(k):
        seen_keys.append(k)
        return None
    monkeypatch.setattr(cache_mod, "get", spy_get)

    async def fake_set(k, v):
        return None

    monkeypatch.setattr(cache_mod, "set", fake_set)

    async def fake_call_llm(*a, **kw): return _raw()
    monkeypatch.setattr(planner_mod, "_call_llm", fake_call_llm)

    base_cfg = {"use_cache": True, "personalization_enabled": True}
    await planner_mod.plan("python", cfg={**base_cfg, "personalization_hints": []})
    await planner_mod.plan("python", cfg={**base_cfg, "personalization_hints": [{"text": "a"}]})
    await planner_mod.plan("python", cfg={**base_cfg, "personalization_hints": [{"text": "b"}]})
    # Baseline-with-disabled (different from "enabled but empty"? No —
    # hints_cache_tag returns "" in both, so keys[0] and keys[3] must match.)
    await planner_mod.plan("python", cfg={**base_cfg,
                                          "personalization_enabled": False,
                                          "personalization_hints": [{"text": "a"}]})

    # plan_key hashes the route, so "|h:" won't appear in the final string
    # — but distinct hints must produce distinct keys, while empty/disabled
    # must produce the same key as today (cache-hit preservation).
    assert seen_keys[0] != seen_keys[1]
    assert seen_keys[1] != seen_keys[2]
    assert seen_keys[0] == seen_keys[3]  # disabled == empty (both no suffix)


@pytest.mark.asyncio
async def test_personalization_offer_stripped_before_cache(monkeypatch):
    """Cached plan dict must NOT contain personalization_offer."""
    import pipeline.planner as planner_mod
    import pipeline.cache as cache_mod

    stored = {}
    async def fake_get(k):
        return None

    async def spy_set(k, v):
        stored[k] = v

    monkeypatch.setattr(cache_mod, "get", fake_get)
    monkeypatch.setattr(cache_mod, "set", spy_set)

    raw = _raw({"personalization_offer": {"hint": "h", "question": "q?"}})
    async def fake_call_llm(*a, **kw): return raw
    monkeypatch.setattr(planner_mod, "_call_llm", fake_call_llm)

    cfg = {"use_cache": True, "personalization_enabled": True,
           "personalization_hints": []}
    spec = await planner_mod.plan("I am looking for a python engineer with experience", cfg=cfg)

    assert spec.personalization_offer == {"hint": "h", "question": "q?"}
    [(_k, cached_dict)] = list(stored.items())
    assert cached_dict.get("personalization_offer") is None


from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_load_recruiter_hints_returns_enabled_and_hints():
    """Reads facts from recruiter_memory table."""
    from pipeline.search import HybridSearchEngine
    pool = AsyncMock()
    pool.fetchrow.return_value = {"personalization_enabled": True}
    pool.fetch.return_value = [
        {"content": "likes builders"},
        {"content": "prefers remote"},
    ]
    engine = HybridSearchEngine.__new__(HybridSearchEngine)
    engine.pool = pool
    enabled, hints = await engine._load_recruiter_hints("rid-1")
    assert enabled is True
    assert hints == [{"text": "likes builders"}, {"text": "prefers remote"}]


@pytest.mark.asyncio
async def test_load_recruiter_hints_disabled_returns_empty():
    """When personalization_enabled=False, returns (False, []) without fetching memory."""
    from pipeline.search import HybridSearchEngine
    pool = AsyncMock()
    pool.fetchrow.return_value = {"personalization_enabled": False}
    engine = HybridSearchEngine.__new__(HybridSearchEngine)
    engine.pool = pool
    enabled, hints = await engine._load_recruiter_hints("rid-2")
    assert enabled is False
    assert hints == []
    pool.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_load_recruiter_hints_defaults_disabled_on_miss():
    from pipeline.search import HybridSearchEngine
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    pool.fetch.return_value = []
    engine = HybridSearchEngine.__new__(HybridSearchEngine)
    engine.pool = pool
    enabled, hints = await engine._load_recruiter_hints("nobody")
    assert enabled is False
    assert hints == []
    pool.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_load_recruiter_hints_returns_defaults_for_none_id():
    from pipeline.search import HybridSearchEngine
    engine = HybridSearchEngine.__new__(HybridSearchEngine)
    engine.pool = AsyncMock()
    enabled, hints = await engine._load_recruiter_hints(None)
    assert enabled is False
    assert hints == []


def test_search_response_exposes_personalization_offer():
    from api.main import SearchResponse
    assert "personalization_offer" in SearchResponse.model_fields
    f = SearchResponse.model_fields["personalization_offer"]
    assert f.default is None
