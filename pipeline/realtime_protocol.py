"""The two-queue protocol adapter.

A realtime agent speaks two protocols at once and they are not the same shape.

*Inbound*, a transport hands over raw events: a partial transcript, a barge-in,
a tool call, a duplicate of a tool call, a typed message. They arrive in wall
order, with timestamps the session did not choose.

*Outbound*, a client needs five things and only five: the calls the agent is
making, the cancellations that withdrew one, the acknowledgements it may speak
before evidence exists, the clarifications it is blocked on, and the state
snapshots that say what it currently believes.

This adapter is the seam. It normalises inbound events onto one monotonic
timeline shared with the session's own event stream, so a replay can reason
about latency without trusting two different clocks; and it reduces the
session's internal events to the outbound vocabulary, so a client never has to
understand the harness's internals to render a turn.

Everything the adapter does is deterministic given the same inputs, which is
what makes it the entry point for the interruption benchmark.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from pipeline.realtime_events import EventEnvelope
from pipeline.realtime_state import StateSnapshot
from pipeline.realtime_tools import ToolRejected

InboundKind = Literal["transcript", "interrupt", "tool_call", "user_text", "role_image"]

OutboundKind = Literal[
    "call",
    "cancellation",
    "acknowledgement",
    "clarification",
    "snapshot",
    "rejection",
    "speech",
]

# Session events that mean something to a client, and what they mean.
_OUTBOUND_BY_EVENT: dict[str, OutboundKind] = {
    "tool.started": "call",
    "tool.cancelled": "cancellation",
    "tool.rejected": "rejection",
    "search.cancelled": "cancellation",
    "acknowledgement.ready": "acknowledgement",
    "clarification.requested": "clarification",
    "state.snapshot": "snapshot",
    "transcript.output": "speech",
}

# Outbound kind -> the counter it advances. Explicit rather than derived, so the
# counter names stay stable and readable in a report.
_STAT_COUNTERS: dict[str, str] = {
    "call": "calls",
    "cancellation": "cancellations",
    "acknowledgement": "acknowledgements",
    "clarification": "clarifications",
    "snapshot": "snapshots",
    "rejection": "rejections",
    "speech": "speech",
}


def _stable(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(slots=True)
class InboundEvent:
    """One normalised input, timestamped on the session's timeline."""

    kind: InboundKind
    timestamp_ms: float = 0.0
    text: str = ""
    final: bool = False
    call_id: str | None = None
    name: str = ""
    arguments: dict[str, Any] = field(default_factory=dict)
    reason: str = "barge_in"
    image_id: str = ""
    turn_id: str | None = None
    event_id: str = field(default_factory=lambda: f"in_{uuid.uuid4().hex}")

    @classmethod
    def transcript(cls, text: str, *, final: bool = False, timestamp_ms: float = 0.0) -> InboundEvent:
        return cls(kind="transcript", text=text, final=final, timestamp_ms=timestamp_ms)

    @classmethod
    def interrupt(cls, *, reason: str = "barge_in", timestamp_ms: float = 0.0) -> InboundEvent:
        return cls(kind="interrupt", reason=reason, timestamp_ms=timestamp_ms)

    @classmethod
    def role_image(
        cls,
        image_id: str,
        description: str,
        *,
        source: str = "transport",
        timestamp_ms: float = 0.0,
    ) -> InboundEvent:
        """A role the recruiter showed, already transcribed by the vision step.

        ``description`` is the extraction, not the pixels: the harness cannot
        read an image, and pretending otherwise would put an unverifiable step
        inside the part of the system this module exists to make auditable.
        """
        return cls(
            kind="role_image",
            image_id=image_id,
            text=description,
            reason=source,
            timestamp_ms=timestamp_ms,
        )

    @classmethod
    def tool_call(
        cls,
        name: str,
        arguments: dict[str, Any],
        *,
        call_id: str,
        timestamp_ms: float = 0.0,
    ) -> InboundEvent:
        return cls(
            kind="tool_call",
            name=name,
            arguments=dict(arguments),
            call_id=call_id,
            timestamp_ms=timestamp_ms,
        )


@dataclass(slots=True)
class OutboundMessage:
    """One normalised output, timestamped on the same timeline as the inputs."""

    kind: OutboundKind
    timestamp_ms: float
    sequence: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None
    turn_id: str | None = None
    revision_id: str | None = None
    duplicate: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "timestamp_ms": round(self.timestamp_ms, 3),
            "sequence": self.sequence,
            "call_id": self.call_id,
            "turn_id": self.turn_id,
            "revision_id": self.revision_id,
            "duplicate": self.duplicate,
            "payload": self.payload,
        }


class RealtimeProtocolAdapter:
    """Translate between the transport's events and the session's state.

    The adapter owns dispatch, so it is the single place where a tool call, its
    rejection, its cancellation and its resulting snapshot are correlated by
    ``call_id``.
    """

    def __init__(self, session: Any) -> None:
        self.session = session
        self._sequence = 0
        # call_id -> the arguments it was first seen with, so a repeat is
        # reported as a duplicate instead of silently looking like a new call.
        self.seen_calls: dict[str, str] = {}
        self.stats: dict[str, int] = {
            "inputs": 0,
            "calls_submitted": 0,
            "duplicate_calls": 0,
            "calls": 0,
            "cancellations": 0,
            "acknowledgements": 0,
            "clarifications": 0,
            "snapshots": 0,
            "rejections": 0,
            "speech": 0,
        }

    # ── Clock ────────────────────────────────────────────────────────────────

    def now_ms(self) -> float:
        """Milliseconds since the session's event stream started.

        Sharing the session's epoch is what lets inbound inputs and outbound
        messages be compared directly. Two clocks would make every latency
        measurement a guess.
        """
        started_at = getattr(self.session.graph, "started_at", None)
        if started_at is None:
            return round(time.perf_counter() * 1000, 3)
        return round((time.perf_counter() - started_at) * 1000, 3)

    # ── Inbound ──────────────────────────────────────────────────────────────

    async def submit(self, event: InboundEvent) -> list[OutboundMessage]:
        """Handle one input and return the messages it produced."""
        self.stats["inputs"] += 1
        if not event.timestamp_ms:
            event.timestamp_ms = self.now_ms()
        if event.kind == "transcript":
            self.session.observe_transcript(event.text, final=event.final)
        elif event.kind == "interrupt":
            await self.session.tools.note_barge_in(reason=event.reason)
        elif event.kind == "role_image":
            self.session.attach_role_image(event.image_id, event.text, source=event.reason)
        elif event.kind == "tool_call":
            return await self._dispatch(event)
        elif event.kind == "user_text":
            self.session.observe_transcript(event.text, final=True)
        return self.drain()

    async def _dispatch(self, event: InboundEvent) -> list[OutboundMessage]:
        call_id = event.call_id or f"call_{uuid.uuid4().hex}"
        signature = f"{event.name}:{_stable(event.arguments)}"
        duplicate = self.seen_calls.get(call_id) == signature
        if duplicate:
            # A retried call is reported as a duplicate and still dispatched:
            # the session's idempotency ledger is what guarantees one side
            # effect, and hiding the retry would make that guarantee invisible.
            self.stats["duplicate_calls"] += 1
        else:
            self.seen_calls[call_id] = signature
        self.stats["calls_submitted"] += 1

        try:
            result = await self.session.tools.dispatch(event.name, event.arguments)
        except ToolRejected as exc:
            self.stats["rejections"] += 1
            messages = self.drain()
            messages.append(self._message(
                "rejection",
                call_id=call_id,
                turn_id=event.turn_id,
                payload={"name": event.name, "error": str(exc), "rejected": True},
            ))
            return messages
        except Exception as exc:  # noqa: BLE001 - a failed call is a protocol message
            messages = self.drain()
            messages.append(self._message(
                "rejection",
                call_id=call_id,
                turn_id=event.turn_id,
                payload={
                    "name": event.name,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                    "failed": True,
                },
            ))
            return messages

        messages = self.drain()
        if not any(message.kind == "call" and message.call_id == call_id for message in messages):
            # The session already reported this call through its own events; if
            # it did not, the adapter still owes the client a correlated result.
            messages.append(self._message(
                "call",
                call_id=call_id,
                turn_id=event.turn_id,
                payload={"name": event.name, "result": result},
                duplicate=duplicate,
            ))
        return messages

    # ── Outbound ─────────────────────────────────────────────────────────────

    def drain(self) -> list[OutboundMessage]:
        """Translate every pending session event into protocol messages."""
        messages: list[OutboundMessage] = []
        queue = getattr(self.session, "events", None)
        while queue is not None and not queue.empty():
            messages.extend(self.translate(queue.get_nowait()))
        return messages

    def translate(self, event: EventEnvelope) -> list[OutboundMessage]:
        kind = _OUTBOUND_BY_EVENT.get(event.type)
        if kind is None:
            return []
        payload = dict(event.payload)
        message = self._message(
            kind,
            call_id=payload.get("call_id"),
            turn_id=event.turn_id,
            revision_id=event.revision_id,
            payload=payload,
            timestamp_ms=event.timestamp_ms,
        )
        return [message]

    def final_snapshot(self) -> OutboundMessage | None:
        """The last authoritative snapshot, as the closing message of a turn."""
        snapshot: StateSnapshot | None = self.session.state
        if snapshot is None or not snapshot.authoritative:
            # Never close a turn on a guess. Fall back to the newest
            # authoritative snapshot in the journal.
            snapshot = next(
                (
                    item
                    for item in reversed(self.session.state_journal.snapshots)
                    if item.authoritative
                ),
                None,
            )
        if snapshot is None:
            return None
        return self._message(
            "snapshot",
            revision_id=snapshot.revision.revision_id,
            payload={**snapshot.model_dump(mode="json"), "final": True},
        )

    def _message(
        self,
        kind: OutboundKind,
        *,
        payload: dict[str, Any],
        call_id: str | None = None,
        turn_id: str | None = None,
        revision_id: str | None = None,
        timestamp_ms: float | None = None,
        duplicate: bool = False,
    ) -> OutboundMessage:
        self._sequence += 1
        counter = _STAT_COUNTERS.get(kind)
        if counter is not None:
            self.stats[counter] += 1
        return OutboundMessage(
            kind=kind,
            timestamp_ms=self.now_ms() if timestamp_ms is None else timestamp_ms,
            sequence=self._sequence,
            payload=payload,
            call_id=call_id,
            turn_id=turn_id,
            revision_id=revision_id,
            duplicate=duplicate,
        )
