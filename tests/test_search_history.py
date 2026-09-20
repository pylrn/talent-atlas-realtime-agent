from fastapi.testclient import TestClient


def test_search_history_endpoint_exists():
    from api.main import app
    # raise_server_exceptions=False: DB errors surface as 500, not a raised exception
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/search/history?recruiter_id=00000000-0000-0000-0000-000000000001")
    # 200 or 500 (DB not running in test) — not 404
    assert resp.status_code != 404


def test_search_history_response_model():
    from api.main import SearchHistoryResponse
    r = SearchHistoryResponse(entries=[])
    assert r.entries == []


def test_search_history_entry_model():
    from api.main import SearchHistoryEntry
    entry = SearchHistoryEntry(
        id=1,
        query="python engineer",
        filters_json={"city": "Berlin"},
        results_json=[{"id": "c-1", "score": 0.95}],
        latency_ms=120,
        timestamp="2026-06-09T12:00:00+00:00",
    )
    assert entry.id == 1
    assert entry.query == "python engineer"
    assert entry.latency_ms == 120


def test_search_history_entry_defaults():
    from api.main import SearchHistoryEntry
    entry = SearchHistoryEntry(id=2, timestamp="2026-06-09T12:00:00+00:00")
    assert entry.query is None
    assert entry.filters_json == {}
    assert entry.results_json == []
    assert entry.latency_ms is None


def test_log_search_history_skips_without_recruiter_id():
    """_log_search_history should be a no-op when recruiter_id is None."""
    import asyncio
    from api.main import _log_search_history

    # Should complete without error and without touching DB
    asyncio.run(_log_search_history(
        recruiter_id=None,
        query="test",
        filters={},
        results=[],
        latency_ms=0,
    ))
