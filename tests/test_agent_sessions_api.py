import json
from types import SimpleNamespace

import pytest


def test_session_jsonb_values_are_normalized_for_frontend_restore():
    from api.main import _coerce_session_context, _coerce_session_messages

    messages = [{"role": "user", "content": "hi"}]
    assert _coerce_session_messages(json.dumps(messages)) == messages
    assert _coerce_session_messages({"messages": messages}) == messages
    assert _coerce_session_messages("not json") == []

    context = {"candidate_summaries": [{"id": "c-1", "name": "Ada"}]}
    assert _coerce_session_context(json.dumps(context)) == context
    assert _coerce_session_context(None) == {}


def test_plain_session_messages_can_hydrate_agent_history():
    from api.main import _pydantic_history_from_session_messages

    history = _pydantic_history_from_session_messages([
        {"role": "user", "content": "Find senior Python engineers"},
        {"role": "assistant", "content": "I found Ada and Grace."},
        {"role": "system", "content": "Context added: Ada"},
        {"role": "user", "content": ""},
    ])

    assert len(history) == 2
    assert history[0].kind == "request"
    assert history[0].parts[0].part_kind == "user-prompt"
    assert history[0].parts[0].content == "Find senior Python engineers"
    assert history[1].kind == "response"
    assert history[1].parts[0].part_kind == "text"
    assert history[1].parts[0].content == "I found Ada and Grace."


@pytest.mark.asyncio
async def test_session_summary_uses_deepseek_openai_compatible_client(monkeypatch):
    from api import main as api_main

    calls = {"client": None, "create": None}

    class FakeCompletions:
        async def create(self, **kwargs):
            calls["create"] = kwargs
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content='{"title":"Python search","summary":"Looked for senior Python engineers and saved the strongest options."}'
                        )
                    )
                ]
            )

    class FakeClient:
        def __init__(self, **kwargs):
            calls["client"] = kwargs
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(api_main.settings, "deepseek_api_key", "ds-test-key", raising=False)
    monkeypatch.setattr(api_main.settings, "llm_provider", "deepseek", raising=False)
    monkeypatch.setattr(api_main.settings, "llm_model", "deepseek-chat", raising=False)
    monkeypatch.setattr("pipeline.observability.get_async_openai", lambda: FakeClient)

    summary = await api_main._llm_summarize_agent_session(
        [{"role": "user", "content": "Find senior Python engineers"}],
        {"results": [{"id": "c-1", "name": "Ada"}]},
        model="deepseek:deepseek-v4-flash",
    )

    assert calls["client"] == {
        "api_key": "ds-test-key",
        "base_url": "https://api.deepseek.com",
    }
    assert calls["create"]["model"] == "deepseek-chat"
    assert calls["create"]["response_format"] == {"type": "json_object"}
    assert summary == {
        "title": "Python search",
        "summary": "Looked for senior Python engineers and saved the strongest options.",
    }
