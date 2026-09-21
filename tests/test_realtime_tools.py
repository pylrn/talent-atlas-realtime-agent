from __future__ import annotations

import pytest
from pydantic import ValidationError

from pipeline.realtime_tools import RealtimeToolDispatcher, ToolRejected


@pytest.mark.asyncio
async def test_dispatcher_validates_and_calls_registered_search_tool() -> None:
    received: list[dict] = []

    async def search(payload: dict) -> dict:
        received.append(payload)
        return {"revision_id": "rev-1", "candidate_count": 8}

    dispatcher = RealtimeToolDispatcher({"search_candidates": search})
    result = await dispatcher.dispatch(
        "search_candidates",
        {
            "query": "python backend engineer",
            "city": "Bengaluru",
            "must_skills": ["python"],
            "top_k": 8,
        },
    )

    assert result["candidate_count"] == 8
    assert received[0]["city"] == "Bengaluru"


@pytest.mark.asyncio
async def test_dispatcher_rejects_unknown_tools_and_fields() -> None:
    dispatcher = RealtimeToolDispatcher({})

    with pytest.raises(ToolRejected, match="Unknown realtime tool"):
        await dispatcher.dispatch("query_database", {"sql": "select * from candidates"})

    with pytest.raises(ToolRejected, match="Invalid arguments"):
        await dispatcher.dispatch(
            "search_candidates",
            {"query": "python", "raw_sql": "select * from candidates"},
        )


@pytest.mark.asyncio
async def test_search_tool_enforces_bounded_result_count() -> None:
    async def search(payload: dict) -> dict:
        return payload

    dispatcher = RealtimeToolDispatcher({"search_candidates": search})

    with pytest.raises(ToolRejected, match="Invalid arguments"):
        await dispatcher.dispatch("search_candidates", {"query": "python", "top_k": 100})


def test_tool_declarations_do_not_expose_sql_or_credentials() -> None:
    declarations = RealtimeToolDispatcher.tool_declarations()
    serialized = str(declarations).casefold()

    assert "search_candidates" in serialized
    assert "interrupt_search" in serialized
    assert "revise_search" not in serialized
    assert "list_skills" in serialized
    assert "should_themes" in serialized
    assert "raw_sql" not in serialized
    assert "api_key" not in serialized
    assert "additionalproperties" not in serialized


def test_retrieval_tools_are_non_blocking_and_lookups_stay_blocking() -> None:
    """The model must be able to speak across retrieval, and only retrieval.

    A blocking retrieval call is what produces dead air in a voice agent, so
    the slow tools run in the background. Every other tool is a bounded lookup
    whose latency is shorter than a filler sentence would be, and `list_skills`
    gates plan construction, so those still wait.
    """
    declarations = {item["name"]: item for item in RealtimeToolDispatcher.tool_declarations()}

    assert declarations["search_candidates"]["behavior"] == "NON_BLOCKING"
    assert declarations["interrupt_search"]["behavior"] == "NON_BLOCKING"
    for name in (
        "inspect_candidate",
        "compare_candidates",
        "format_current_answer",
        "cancel_current_action",
        "list_skills",
    ):
        assert declarations[name]["behavior"] == "BLOCKING", name

    assert RealtimeToolDispatcher.non_blocking_tools() == frozenset(
        {"search_candidates", "interrupt_search"}
    )
