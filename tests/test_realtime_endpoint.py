from api.main import app
from pipeline.realtime_prompt import REALTIME_SYSTEM_PROMPT


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
