from api.main import app
from pipeline.realtime_prompt import REALTIME_SYSTEM_PROMPT
from pipeline.realtime_tools import RealtimeToolDispatcher


def test_talent_realtime_websocket_is_registered():
    paths = {getattr(route, "path", "") for route in app.routes}

    assert "/talent/realtime/ws" in paths


def test_realtime_prompt_requires_revision_tools_and_grounding():
    prompt = REALTIME_SYSTEM_PROMPT.casefold()

    assert "interrupt_search" in prompt
    assert "search_candidates" in prompt
    assert "interrupt" in prompt
    assert "only" in prompt and "evidence" in prompt
    assert "protected" in prompt


def test_every_declared_tool_is_documented_in_the_prompt():
    """A declared tool with no policy attached is a capability, not a rule.

    This is the invariant that keeps the manifest and the instruction from
    drifting apart: adding a tool without writing down when to call it fails
    here rather than shipping as an undocumented escape hatch.
    """
    prompt = REALTIME_SYSTEM_PROMPT.casefold()

    undocumented = [
        declaration["name"]
        for declaration in RealtimeToolDispatcher.tool_declarations()
        if declaration["name"] not in prompt
    ]

    assert undocumented == []


def test_the_prompt_states_the_rules_the_new_tools_depend_on():
    prompt = REALTIME_SYSTEM_PROMPT.casefold()

    # A correction must replace rather than accumulate.
    assert "replace_existing" in prompt
    # A retry must be safe.
    assert "idempotency_key" in prompt
    # A requirement the database cannot enforce must never be described as a
    # filter that was applied.
    assert "degree" in prompt
    assert "cannot be enforced" in prompt
