import pytest


def test_agent_module_imports():
    from pipeline.agent import recruiter_agent, AgentDeps, build_system_prompt
    assert recruiter_agent is not None


def test_build_system_prompt_includes_context():
    from pipeline.agent import build_system_prompt
    prompt = build_system_prompt(
        query="python dev",
        filters={"city": "Bangalore"},
        result_count=2,
        hints=["prefers startup builders"],
    )
    assert "python dev" in prompt
    assert "Bangalore" in prompt
    assert "prefers startup builders" in prompt
    assert "candidates" in prompt.lower()  # DB schema block


def test_build_system_prompt_empty_context():
    from pipeline.agent import build_system_prompt
    prompt = build_system_prompt(query="", filters={}, result_count=0, hints=[])
    assert "recruiter" in prompt.lower()


def test_build_system_prompt_broad_request_guidance_has_concrete_example():
    from pipeline.agent import build_system_prompt

    prompt = build_system_prompt(query="", filters={}, result_count=0, hints=[])

    assert 'Example: recruiter says "I need sales people."' in prompt
    assert "Draft spec before search:" in prompt
    assert "Role: SDR / Sales Development Representative [assumed]" in prompt
    assert "Must-have: CRM, lead generation, outbound prospecting [assumed]" in prompt
    assert "Which direction should I use?" in prompt


def test_build_system_prompt_prefers_manual_no_llm_specs():
    from pipeline.agent import build_system_prompt

    prompt = build_system_prompt(query="", filters={}, result_count=0, hints=[])

    assert 'Default: `mode="no-llm"`' in prompt
    assert "Manual Search Spec Discipline" in prompt
    assert "`filters.skills`: explicit must-have skills only" in prompt
    assert "`query`: compact semantic intent" in prompt
    assert "What To Drop Instead Of Sending" in prompt
    assert 'run_search(query="linux infrastructure engineer hosting llms deployment"' in prompt


def test_build_agent_prompt_for_run_uses_langfuse_prompt(monkeypatch):
    from pipeline import agent as agent_mod

    class FakePrompt:
        name = "recruiter-agent"
        version = 4

        def compile(self, **kwargs):
            assert "memory_block" in kwargs
            assert "context_block" in kwargs
            return "compiled prompt"

    monkeypatch.setattr(agent_mod, "_obs_get_prompt", lambda name, label: FakePrompt())

    prompt, lf_prompt = agent_mod.build_agent_prompt_for_run(
        query="python dev",
        filters={"city": "Berlin"},
        result_count=3,
        hints=["prefers startup builders"],
        observations=[],
        label="staging",
    )

    assert prompt == "compiled prompt"
    assert lf_prompt.name == "recruiter-agent"


def test_build_agent_prompt_for_run_falls_back_when_langfuse_missing(monkeypatch):
    from pipeline import agent as agent_mod

    monkeypatch.setattr(agent_mod, "_obs_get_prompt", lambda name, label: None)

    prompt, lf_prompt = agent_mod.build_agent_prompt_for_run(
        query="python dev",
        filters={},
        result_count=0,
        hints=[],
        observations=[],
    )

    assert "python dev" in prompt
    assert lf_prompt is None


def test_build_agent_prompt_for_run_accepts_chat_prompt(monkeypatch):
    from pipeline import agent as agent_mod

    class FakePrompt:
        def compile(self, **kwargs):
            return [
                {"role": "system", "content": "system body"},
                {"role": "user", "content": "user body"},
            ]

    monkeypatch.setattr(agent_mod, "_obs_get_prompt", lambda name, label: FakePrompt())

    prompt, lf_prompt = agent_mod.build_agent_prompt_for_run(
        query="",
        filters={},
        result_count=0,
        hints=[],
        observations=[],
    )

    assert "system body" in prompt
    assert "user body" in prompt
    assert lf_prompt is not None


def test_agent_chat_models_importable():
    """AgentChatContext and AgentChatRequest must be importable from api.main."""
    from api.main import AgentChatContext, AgentChatRequest
    ctx = AgentChatContext(query="ml engineer", filters={"city": "Berlin"})
    req = AgentChatRequest(
        recruiter_id="r-1",
        session_id="s-1",
        message="Find me senior ML engineers",
        context=ctx,
    )
    assert req.recruiter_id == "r-1"
    assert req.context.query == "ml engineer"


def test_personalization_observation_picker_matches_relevant_context():
    from types import SimpleNamespace
    from api.main import AgentChatContext, _pick_relevant_observation

    obs = SimpleNamespace(
        id="o1",
        content="Often accepts candidates with React frontend experience",
        confidence=0.91,
    )
    ctx = AgentChatContext(query="React frontend engineers in Berlin", filters={"city": "Berlin"})

    picked = _pick_relevant_observation(
        "Find React frontend engineers in Berlin",
        ctx,
        [obs],
        set(),
    )

    assert picked is obs


def test_personalization_observation_picker_ignores_low_confidence():
    from types import SimpleNamespace
    from api.main import AgentChatContext, _pick_relevant_observation

    obs = SimpleNamespace(
        id="o1",
        content="Often accepts candidates with React frontend experience",
        confidence=0.4,
    )

    assert _pick_relevant_observation(
        "Find React frontend engineers",
        AgentChatContext(query="", filters={}),
        [obs],
        set(),
    ) is None


def test_personalization_confirmation_parser():
    from api.main import _normalise_confirmation_answer

    assert _normalise_confirmation_answer("yes") is True
    assert _normalise_confirmation_answer("Sure!") is True
    assert _normalise_confirmation_answer("nope") is False
    assert _normalise_confirmation_answer("find more people") is None


def test_personalization_observation_question_uses_natural_grammar():
    from api.main import _observation_question

    assert _observation_question(
        "Often accepts candidates with React frontend experience"
    ).startswith("I noticed you often accept candidates")


def test_agent_recruiter_id_uuid_guard():
    from api.main import _looks_like_uuid

    assert _looks_like_uuid("00000000-0000-0000-0000-000000000001") is True
    assert _looks_like_uuid("debug-user") is False
    assert _looks_like_uuid(None) is False


def test_agent_session_clear_route_registered():
    """POST /agent/session/clear and POST /agent/chat must be registered on the app."""
    from api.main import app
    routes = {r.path for r in app.routes}
    assert "/agent/chat" in routes
    assert "/agent/session/clear" in routes
