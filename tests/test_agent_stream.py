import json
from types import SimpleNamespace

import pytest
from pipeline.agent_stream import (
    sse_event, text_start, text_delta, text_end,
    tool_call_start, tool_call_end,
    search_results_event, push_to_main_event, stack_updated_event, error_event,
)


def _parse(raw: str) -> tuple[str, dict]:
    """Parse raw SSE string into (event_type, data_dict)."""
    lines = raw.strip().split("\n")
    event_type = lines[0].split(": ", 1)[1]
    data = json.loads(lines[1].split(": ", 1)[1])
    return event_type, data


def test_sse_event_format():
    raw = sse_event("FOO", {"x": 1})
    assert raw == 'event: FOO\ndata: {"x": 1}\n\n'


def test_text_start_has_message_id_and_role():
    raw = text_start("msg-1")
    event_type, data = _parse(raw)
    assert event_type == "TEXT_MESSAGE_START"
    assert data["messageId"] == "msg-1"
    assert data["role"] == "assistant"


def test_text_delta_has_delta():
    raw = text_delta("msg-1", "hello")
    event_type, data = _parse(raw)
    assert event_type == "TEXT_MESSAGE_CONTENT"
    assert data["delta"] == "hello"


def test_tool_call_start_has_name():
    raw = tool_call_start("tc-1", "run_search")
    event_type, data = _parse(raw)
    assert event_type == "TOOL_CALL_START"
    assert data["toolCallName"] == "run_search"


def test_tool_call_end_has_id():
    raw = tool_call_end("tc-1", "found 8 results")
    event_type, data = _parse(raw)
    assert event_type == "TOOL_CALL_END"
    assert data["toolCallId"] == "tc-1"
    assert "found 8 results" in data["resultSummary"]


def test_search_results_event_has_results():
    results = [{"id": "c-1", "name": "Ada"}]
    raw = search_results_event(
        results,
        "iter-1",
        source="query_candidates_db",
        result_kind="selected",
        panel_title="Agent-selected candidates",
    )
    event_type, data = _parse(raw)
    assert event_type == "SEARCH_RESULTS"
    assert data["results"][0]["id"] == "c-1"
    assert data["source"] == "query_candidates_db"
    assert data["resultKind"] == "selected"
    assert data["panelTitle"] == "Agent-selected candidates"


def test_stack_updated_event_has_depth():
    raw = stack_updated_event(3)
    event_type, data = _parse(raw)
    assert event_type == "STACK_UPDATED"
    assert data["depth"] == 3


class _ObsContext:
    def __init__(self, calls, name, **kwargs):
        self.calls = calls
        self.name = name
        self.kwargs = kwargs
        self.output = None

    def __enter__(self):
        self.calls.append(("enter", self.name, self.kwargs))
        return self

    def __exit__(self, *exc):
        self.calls.append(("exit", self.name, self.output))
        return False

    def update(self, **kwargs):
        self.output = kwargs
        self.calls.append(("update", self.name, kwargs))


class _AsyncStream:
    def __init__(self, events):
        self.events = events

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for event in self.events:
            yield event


class _FakeRun:
    ctx = object()

    def __init__(self, nodes):
        self.nodes = nodes
        self.result = SimpleNamespace(all_messages=lambda: ["stored"])

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for node in self.nodes:
            yield node


class _FakeAgent:
    def __init__(self, nodes):
        self.nodes = nodes

    def iter(self, *args, **kwargs):
        return _FakeRun(self.nodes)


@pytest.mark.asyncio
async def test_agent_stream_marks_attempt_model_generation_and_tool_span(monkeypatch):
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        PartDeltaEvent,
        TextPartDelta,
        ToolCallPart,
        ToolReturnPart,
    )
    from pipeline import agent_run
    from pipeline.agent_session import AgentSession

    calls = []
    monkeypatch.setattr(
        agent_run,
        "_obs_start_span",
        lambda name, **kwargs: _ObsContext(calls, name, **kwargs),
    )
    monkeypatch.setattr(
        agent_run,
        "_obs_start_agent",
        lambda name, **kwargs: _ObsContext(calls, name, **kwargs),
    )
    monkeypatch.setattr(
        agent_run,
        "_obs_start_generation",
        lambda name, **kwargs: _ObsContext(calls, name, **kwargs),
    )
    monkeypatch.setattr(
        agent_run,
        "_obs_start_tool",
        lambda name, **kwargs: _ObsContext(calls, name, **kwargs),
    )
    monkeypatch.setattr(
        agent_run,
        "_obs_update_current_generation",
        lambda **kwargs: calls.append(("generation_update", "agent.generate_reply", kwargs)),
    )
    monkeypatch.setattr(agent_run, "_retry_plan", lambda model: [model])
    monkeypatch.setattr(agent_run, "_derive_suggestions", lambda session: [])
    monkeypatch.setattr(
        agent_run.Agent,
        "is_model_request_node",
        staticmethod(lambda node: getattr(node, "kind", "") == "model"),
    )
    monkeypatch.setattr(
        agent_run.Agent,
        "is_call_tools_node",
        staticmethod(lambda node: getattr(node, "kind", "") == "tools"),
    )

    model_node = SimpleNamespace(
        kind="model",
        stream=lambda ctx: _AsyncStream([
            PartDeltaEvent(index=0, delta=TextPartDelta("hello")),
        ]),
    )
    tool_node = SimpleNamespace(
        kind="tools",
        stream=lambda ctx: _AsyncStream([
            FunctionToolCallEvent(
                ToolCallPart("run_search", {"query": "python"}, tool_call_id="tc-1")
            ),
            FunctionToolResultEvent(
                ToolReturnPart(
                    "run_search",
                    '{"total": 2, "results": []}',
                    tool_call_id="tc-1",
                )
            ),
        ]),
    )
    session = AgentSession(recruiter_id="r-1", session_id="s-1")
    deps = SimpleNamespace(
        system_prompt="system prompt",
        lf_prompt=None,
    )

    events = [
        event
        async for event in agent_run.stream_agent_run(
            _FakeAgent([model_node, tool_node]),
            message="find python devs",
            model="groq:test-model",
            deps=deps,
            session=session,
        )
    ]

    entered = [name for kind, name, _ in calls if kind == "enter"]
    assert "agent.respond" in entered
    assert "agent.generate_reply" in entered
    assert "tool.search_candidates" in entered
    assert any("TEXT_MESSAGE_CONTENT" in event for event in events)
    assert any("TOOL_CALL_START" in event for event in events)
    assert any("TOOL_CALL_END" in event for event in events)
    assert session.messages == ["stored"]
