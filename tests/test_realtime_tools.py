from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from pipeline.realtime_tools import (
    DEFAULT_TOOL_SPECS,
    CancelCurrentActionArgs,
    RealtimeToolDispatcher,
    RequestClarificationArgs,
    ToolRegistry,
    ToolRejected,
    ToolSpec,
)


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
    # Searching a role the recruiter shared is retrieval, so it must not block
    # either; a blocking variant would reintroduce the dead air this removes.
    assert declarations["use_role_image"]["behavior"] == "NON_BLOCKING"
    for name in (
        "inspect_candidate",
        "compare_candidates",
        "format_current_answer",
        "cancel_current_action",
        "list_skills",
        "request_clarification",
        "add_to_shortlist",
    ):
        assert declarations[name]["behavior"] == "BLOCKING", name

    assert RealtimeToolDispatcher.non_blocking_tools() == frozenset(
        {"search_candidates", "interrupt_search", "use_role_image"}
    )


def test_the_manifest_classifies_effects_rather_than_listing_names() -> None:
    """Read-only versus state-changing travels with the tool.

    A hardcoded list of names cannot answer "is this call allowed to write?"
    for a tool the session registered a moment ago.
    """
    registry = ToolRegistry(DEFAULT_TOOL_SPECS)

    assert registry.state_modifying() == frozenset({"add_to_shortlist"})
    assert registry.get("search_candidates").effect == "read_only"
    assert registry.get("add_to_shortlist").scope == "shortlist"
    assert registry.get("add_to_shortlist").idempotent is True


def test_a_compatibility_alias_resolves_but_is_not_advertised() -> None:
    registry = ToolRegistry(DEFAULT_TOOL_SPECS)

    assert registry.resolve("revise_search") == "interrupt_search"
    assert registry.get("revise_search") is registry.get("interrupt_search")
    assert "revise_search" not in [item["name"] for item in registry.declarations()]


def test_a_session_can_declare_a_tool_it_did_not_start_with() -> None:
    registry = ToolRegistry(DEFAULT_TOOL_SPECS)
    before = registry.revision

    registry.register(ToolSpec(
        name="send_email",
        description="Send a message.",
        args_model=CancelCurrentActionArgs,
        effect="state_modifying",
        scope="email",
    ))

    assert registry.revision == before + 1
    assert "send_email" in registry.names()
    assert registry.state_modifying() == frozenset({"add_to_shortlist", "send_email"})
    assert "send_email" in [item["name"] for item in registry.declarations()]


@pytest.mark.asyncio
async def test_a_runtime_registered_tool_is_dispatchable_and_removable() -> None:
    calls: list[dict] = []

    async def send(payload: dict) -> dict:
        calls.append(payload)
        return {"sent": True}

    dispatcher = RealtimeToolDispatcher({})
    dispatcher.register_tool(
        ToolSpec(
            name="send_email",
            description="Send a message.",
            args_model=CancelCurrentActionArgs,
        ),
        send,
    )

    result = await dispatcher.dispatch("send_email", {"reason": "intro"})
    assert result["sent"] is True
    assert calls[0]["reason"] == "intro"

    assert dispatcher.unregister_tool("send_email") is True
    with pytest.raises(ToolRejected, match="Unknown realtime tool"):
        await dispatcher.dispatch("send_email", {"reason": "intro"})


@pytest.mark.asyncio
async def test_a_repeated_write_with_the_same_key_is_applied_once() -> None:
    applied: list[list[str]] = []

    async def shortlist(payload: dict) -> dict:
        applied.append(payload["candidate_ids"])
        return {"shortlist": payload["candidate_ids"]}

    dispatcher = RealtimeToolDispatcher({"add_to_shortlist": shortlist})
    args = {"candidate_ids": ["a1", "a2"], "idempotency_key": "once"}

    first = await dispatcher.dispatch("add_to_shortlist", args)
    second = await dispatcher.dispatch("add_to_shortlist", args)

    assert applied == [["a1", "a2"]]
    assert first.get("idempotent_replay") is None
    assert second["idempotent_replay"] is True

    # The audit log has to carry the proof, not just the retry: one effect
    # landed, and the second call only replayed the first result.
    outcomes = [entry["outcome"] for entry in dispatcher.effect_log]
    assert outcomes == ["applied", "replayed"]
    assert outcomes.count("applied") == 1


@pytest.mark.asyncio
async def test_a_newer_write_in_the_same_scope_cancels_the_older_one() -> None:
    """Two selections must not both land."""
    gate = asyncio.Event()
    applied: list[list[str]] = []

    async def shortlist(payload: dict) -> dict:
        await gate.wait()
        applied.append(payload["candidate_ids"])
        return {"shortlist": payload["candidate_ids"]}

    dispatcher = RealtimeToolDispatcher({"add_to_shortlist": shortlist})
    stale = asyncio.create_task(dispatcher.dispatch(
        "add_to_shortlist", {"candidate_ids": ["a1", "a2", "a3"]}
    ))
    await asyncio.sleep(0)
    correction = asyncio.create_task(dispatcher.dispatch(
        "add_to_shortlist", {"candidate_ids": ["a1"], "replace_existing": True}
    ))
    await asyncio.sleep(0)
    gate.set()
    outcomes = await asyncio.gather(stale, correction, return_exceptions=True)

    assert isinstance(outcomes[0], asyncio.CancelledError)
    assert applied == [["a1"]]

    # The stale selection is withdrawn before the correction lands, and the
    # withdrawn one is never recorded as an effect.
    effects = [entry["outcome"] for entry in dispatcher.effect_log]
    assert effects == ["cancelled", "applied"]


@pytest.mark.asyncio
async def test_a_read_only_tool_is_never_superseded() -> None:
    """Scope supersession applies to writes, not to lookups."""
    gate = asyncio.Event()

    async def search(payload: dict) -> dict:
        await gate.wait()
        return {"count": 1}

    dispatcher = RealtimeToolDispatcher({"search_candidates": search})
    first = asyncio.create_task(dispatcher.dispatch(
        "search_candidates", {"query": "python engineer"}
    ))
    await asyncio.sleep(0)
    second = asyncio.create_task(dispatcher.dispatch(
        "search_candidates", {"query": "python engineer"}
    ))
    await asyncio.sleep(0)
    gate.set()
    outcomes = await asyncio.gather(first, second, return_exceptions=True)

    assert all(not isinstance(item, BaseException) for item in outcomes)


def test_a_clarification_can_only_name_real_slots() -> None:
    with pytest.raises(ValidationError, match="Unknown slot names"):
        RequestClarificationArgs(
            question="What is your favourite colour?",
            slots_needed=["colour"],
        )


def test_a_clarification_naming_a_real_slot_is_accepted() -> None:
    args = RequestClarificationArgs(question="Which city?", slots_needed=["city"])

    assert args.slots_needed == ["city"]


@pytest.mark.asyncio
async def test_a_failed_write_is_recorded_as_failed_not_applied() -> None:
    """A write that raised must not leave an ``applied`` entry behind."""

    async def shortlist(payload: dict) -> dict:
        raise RuntimeError("shortlist store unavailable")

    dispatcher = RealtimeToolDispatcher({"add_to_shortlist": shortlist})

    with pytest.raises(RuntimeError, match="shortlist store unavailable"):
        await dispatcher.dispatch("add_to_shortlist", {"candidate_ids": ["a1"]})

    assert [entry["outcome"] for entry in dispatcher.effect_log] == ["failed"]


@pytest.mark.asyncio
async def test_a_rejected_write_never_reaches_the_audit_log() -> None:
    """A call that never executed is not an effect, so it is not logged."""
    applied: list[list[str]] = []

    async def shortlist(payload: dict) -> dict:
        applied.append(payload["candidate_ids"])
        return {"shortlist": payload["candidate_ids"]}

    dispatcher = RealtimeToolDispatcher({"add_to_shortlist": shortlist})

    with pytest.raises(ToolRejected, match="Invalid arguments"):
        await dispatcher.dispatch("add_to_shortlist", {"candidate_ids": "not-a-list"})

    assert applied == []
    assert dispatcher.effect_log == []


@pytest.mark.asyncio
async def test_a_withdrawn_write_never_reports_a_confirmation() -> None:
    """A superseded call raises, so the caller cannot read a success out of it."""
    gate = asyncio.Event()

    async def shortlist(payload: dict) -> dict:
        await gate.wait()
        return {"shortlist": payload["candidate_ids"], "confirmed": True}

    dispatcher = RealtimeToolDispatcher({"add_to_shortlist": shortlist})
    stale = asyncio.create_task(
        dispatcher.dispatch("add_to_shortlist", {"candidate_ids": ["a1", "a2", "a3"]})
    )
    await asyncio.sleep(0)
    correction = asyncio.create_task(
        dispatcher.dispatch(
            "add_to_shortlist", {"candidate_ids": ["a1"], "replace_existing": True}
        )
    )
    await asyncio.sleep(0)
    gate.set()
    stale_result, correction_result = await asyncio.gather(
        stale, correction, return_exceptions=True
    )

    assert isinstance(stale_result, asyncio.CancelledError)
    # A withdrawal surfaces as an exception, so the model never receives a dict
    # it could read back to the user as a confirmation.
    assert not isinstance(stale_result, dict)
    assert isinstance(correction_result, dict)
    assert correction_result["confirmed"] is True
    assert correction_result["superseded_calls"] == ["shortlist"]
