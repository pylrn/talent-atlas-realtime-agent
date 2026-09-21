"""Versioned, redacted telemetry primitives for the realtime agent harness."""

from __future__ import annotations

import time
import uuid
from copy import deepcopy
from typing import Any, Literal

from pydantic import BaseModel, Field


REDACTED = "[REDACTED]"
_SENSITIVE_PARTS = (
    "api_key",
    "apikey",
    "secret",
    "password",
    "authorization",
    "database_url",
    "dsn",
    "access_token",
)


def _is_sensitive(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_PARTS)


def redact(value: Any) -> Any:
    """Return a telemetry-safe deep copy of a JSON-compatible value."""
    if isinstance(value, dict):
        return {
            str(key): REDACTED if _is_sensitive(str(key)) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return [redact(item) for item in value]
    return deepcopy(value)


class EventEnvelope(BaseModel):
    schema_version: int = 1
    event_id: str = Field(default_factory=lambda: f"evt_{uuid.uuid4().hex}")
    type: str
    session_id: str
    turn_id: str | None = None
    revision_id: str | None = None
    sequence: int = 0
    timestamp_ms: float
    payload: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        event_type: str,
        *,
        session_id: str,
        payload: dict[str, Any] | None = None,
        turn_id: str | None = None,
        revision_id: str | None = None,
        sequence: int = 0,
        timestamp_ms: float | None = None,
    ) -> "EventEnvelope":
        return cls(
            type=event_type,
            session_id=session_id,
            turn_id=turn_id,
            revision_id=revision_id,
            sequence=sequence,
            timestamp_ms=round(timestamp_ms if timestamp_ms is not None else time.time() * 1000, 3),
            payload=redact(payload or {}),
        )


NodeStatus = Literal[
    "queued",
    "running",
    "completed",
    "reused",
    "replaced",
    "cancelled",
    "failed",
    "superseded",
]


class TraceNode(BaseModel):
    node_id: str
    kind: str
    status: NodeStatus
    revision_id: str
    parent_ids: list[str] = Field(default_factory=list)
    reused_from: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class TraceGraph:
    """Append-only event stream plus the latest materialized node state."""

    def __init__(self, *, session_id: str, started_at: float | None = None) -> None:
        self.session_id = session_id
        self.started_at = started_at if started_at is not None else time.perf_counter()
        self.events: list[EventEnvelope] = []
        self.nodes: dict[str, TraceNode] = {}
        self._sequence = 0

    def emit(
        self,
        event_type: str,
        *,
        payload: dict[str, Any] | None = None,
        turn_id: str | None = None,
        revision_id: str | None = None,
    ) -> EventEnvelope:
        self._sequence += 1
        event = EventEnvelope.create(
            event_type,
            session_id=self.session_id,
            turn_id=turn_id,
            revision_id=revision_id,
            sequence=self._sequence,
            timestamp_ms=(time.perf_counter() - self.started_at) * 1000,
            payload=payload,
        )
        self.events.append(event)
        return event

    def upsert_node(
        self,
        *,
        node_id: str,
        kind: str,
        status: NodeStatus,
        revision_id: str,
        parent_ids: list[str] | None = None,
        reused_from: str | None = None,
        details: dict[str, Any] | None = None,
        turn_id: str | None = None,
    ) -> EventEnvelope:
        node = TraceNode(
            node_id=node_id,
            kind=kind,
            status=status,
            revision_id=revision_id,
            parent_ids=parent_ids or [],
            reused_from=reused_from,
            details=redact(details or {}),
        )
        self.nodes[node_id] = node
        return self.emit(
            "node.updated",
            turn_id=turn_id,
            revision_id=revision_id,
            payload={"node": node.model_dump(mode="json")},
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "nodes": {
                node_id: node.model_dump(mode="json")
                for node_id, node in self.nodes.items()
            },
            "events": [event.model_dump(mode="json") for event in self.events],
        }
