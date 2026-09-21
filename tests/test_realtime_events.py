from pipeline.realtime_events import EventEnvelope, TraceGraph, redact


def test_redact_masks_nested_credentials_without_touching_query() -> None:
    payload = {
        "query": "python engineer",
        "api_key": "secret",
        "nested": {"database_url": "postgres://secret", "count": 4},
        "items": [{"authorization": "Bearer abc", "score": 0.8}],
    }

    assert redact(payload) == {
        "query": "python engineer",
        "api_key": "[REDACTED]",
        "nested": {"database_url": "[REDACTED]", "count": 4},
        "items": [{"authorization": "[REDACTED]", "score": 0.8}],
    }


def test_event_envelope_assigns_monotonic_sequence_and_redacts_payload() -> None:
    graph = TraceGraph(session_id="session-1")

    first = graph.emit("node.started", payload={"dsn": "private"}, revision_id="rev-1")
    second = graph.emit("node.completed", payload={"hits": 12}, revision_id="rev-1")

    assert first.sequence == 1
    assert second.sequence == 2
    assert first.payload["dsn"] == "[REDACTED]"
    assert second.session_id == "session-1"
    assert second.schema_version == 1


def test_trace_graph_snapshot_preserves_node_lineage_and_raw_events() -> None:
    graph = TraceGraph(session_id="session-1")
    graph.upsert_node(
        node_id="vector-r1",
        kind="vector",
        status="completed",
        revision_id="rev-1",
        parent_ids=["plan-r1"],
        details={"hits": 40},
    )
    graph.upsert_node(
        node_id="vector-r2",
        kind="vector",
        status="reused",
        revision_id="rev-2",
        parent_ids=["plan-r2"],
        reused_from="vector-r1",
        details={"hits": 40},
    )

    snapshot = graph.snapshot()

    assert snapshot["nodes"]["vector-r2"]["reused_from"] == "vector-r1"
    assert snapshot["nodes"]["vector-r2"]["details"]["hits"] == 40
    assert [event["type"] for event in snapshot["events"]] == ["node.updated", "node.updated"]


def test_event_create_can_be_used_without_graph() -> None:
    event = EventEnvelope.create(
        "tool.rejected",
        session_id="session-2",
        payload={"password": "nope", "reason": "unknown tool"},
    )

    assert event.type == "tool.rejected"
    assert event.payload == {"password": "[REDACTED]", "reason": "unknown tool"}
