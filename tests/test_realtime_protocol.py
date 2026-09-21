"""The two-queue protocol: timestamped inputs in, five message kinds out."""

from __future__ import annotations

import pytest

from pipeline.realtime_protocol import (
    InboundEvent,
    OutboundMessage,
    RealtimeProtocolAdapter,
)

from realtime_fakes import FakeEngine, make_session


def _adapter(**kwargs):
    session = make_session(kwargs.pop("engine", None), session_id=kwargs.pop("session_id", "proto"))
    return session, RealtimeProtocolAdapter(session)


@pytest.mark.asyncio
async def test_a_tool_call_produces_a_call_and_a_snapshot() -> None:
    session, adapter = _adapter()

    messages = await adapter.submit(InboundEvent.tool_call(
        "search_candidates", {"query": "python engineer", "top_k": 5}, call_id="call-1"
    ))

    kinds = [message.kind for message in messages]
    assert "snapshot" in kinds
    assert "acknowledgement" in kinds
    assert all(message.call_id in (None, "call-1") for message in messages)


@pytest.mark.asyncio
async def test_an_interrupt_is_a_state_change_not_a_cancellation() -> None:
    session, adapter = _adapter()

    messages = await adapter.submit(InboundEvent.interrupt(reason="barge_in"))

    assert not [message for message in messages if message.kind == "cancellation"]
    snapshots = [message for message in messages if message.kind == "snapshot"]
    assert snapshots
    assert snapshots[-1].payload["phase"] == "interrupted"


@pytest.mark.asyncio
async def test_malformed_arguments_come_back_as_a_rejection() -> None:
    session, adapter = _adapter()

    messages = await adapter.submit(InboundEvent.tool_call(
        "search_candidates",
        {"query": "python", "top_k": 500, "raw_sql": "select 1"},
        call_id="call-bad",
    ))

    rejections = [message for message in messages if message.kind == "rejection"]
    assert len(rejections) == 1
    assert rejections[0].call_id == "call-bad"
    assert rejections[0].payload["rejected"] is True
    assert session.current_plan is None


@pytest.mark.asyncio
async def test_a_retried_call_id_is_reported_as_a_duplicate() -> None:
    session, adapter = _adapter()
    event = InboundEvent.tool_call(
        "search_candidates", {"query": "python engineer", "top_k": 5}, call_id="call-dup"
    )

    await adapter.submit(event)
    second = await adapter.submit(event)

    assert adapter.stats["duplicate_calls"] == 1
    assert any(message.duplicate for message in second)


@pytest.mark.asyncio
async def test_a_transcript_starts_speculative_retrieval_and_says_so() -> None:
    session, adapter = _adapter()

    messages = await adapter.submit(InboundEvent.transcript("Find python engineers in Pune"))
    await session.settle_speculation()
    messages += adapter.drain()

    provisional = [
        message for message in messages
        if message.kind == "snapshot" and message.payload.get("authoritative") is False
    ]
    assert provisional


@pytest.mark.asyncio
async def test_a_role_image_becomes_session_context() -> None:
    session, adapter = _adapter()

    messages = await adapter.submit(InboundEvent.role_image(
        "jd-1", "Senior Backend Engineer\n5+ years of experience\nRequired: Python"
    ))

    assert "jd-1" in session.role_images
    snapshots = [message for message in messages if message.kind == "snapshot"]
    assert snapshots[-1].payload["evidence_sources"] == ["image:jd-1"]


@pytest.mark.asyncio
async def test_every_message_carries_a_timestamp_on_the_session_timeline() -> None:
    """Two clocks would make every latency measurement a guess."""
    session, adapter = _adapter()

    await adapter.submit(InboundEvent.tool_call(
        "search_candidates", {"query": "python engineer", "top_k": 5}, call_id="call-1"
    ))
    messages = adapter.drain()

    stamps = [message.timestamp_ms for message in messages]
    assert stamps == sorted(stamps)
    assert all(stamp >= 0 for stamp in stamps)
    assert [message.sequence for message in messages] == sorted(
        message.sequence for message in messages
    )


@pytest.mark.asyncio
async def test_the_final_snapshot_is_never_a_speculative_guess() -> None:
    """A turn must not close on evidence that was only ever a guess."""
    session, adapter = _adapter()

    await adapter.submit(InboundEvent.transcript("Find python engineers in Pune"))

    assert adapter.final_snapshot() is None


@pytest.mark.asyncio
async def test_the_final_snapshot_is_the_authoritative_state() -> None:
    session, adapter = _adapter()

    await adapter.submit(InboundEvent.tool_call(
        "search_candidates", {"query": "python engineer", "top_k": 5}, call_id="call-1"
    ))
    final = adapter.final_snapshot()

    assert final is not None
    assert final.kind == "snapshot"
    assert final.payload["authoritative"] is True
    assert final.payload["final"] is True
    assert final.payload["status"] == "ready"


@pytest.mark.asyncio
async def test_a_failing_retrieval_is_a_protocol_message_not_a_crash() -> None:
    engine = FakeEngine()
    session, adapter = _adapter(engine=engine)
    engine.fail = True

    messages = await adapter.submit(InboundEvent.tool_call(
        "search_candidates", {"query": "python engineer", "top_k": 5}, call_id="call-fail"
    ))

    assert [message.kind for message in messages if message.kind == "rejection"]
    assert session.state is not None
    assert session.state.status == "failed"


def test_outbound_messages_are_json_ready() -> None:
    message = OutboundMessage(kind="call", timestamp_ms=12.3456, sequence=3, payload={"a": 1})

    rendered = message.as_dict()

    assert rendered["kind"] == "call"
    assert rendered["timestamp_ms"] == 12.346
    assert set(rendered) == {
        "kind", "timestamp_ms", "sequence", "call_id", "turn_id",
        "revision_id", "duplicate", "payload",
    }
