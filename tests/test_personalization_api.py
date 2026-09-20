"""Endpoint tests for personalization-hint CRUD + enable toggle."""

import uuid
from unittest.mock import AsyncMock, MagicMock
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from api import main as api_main


def _make_fact(text="user likes builders", source="manual", fact_id=None):
    """Return a dict-like mock that mimics an asyncpg Record for recruiter_memory rows."""
    r = MagicMock()
    r.__getitem__ = lambda self, k: {
        "id": fact_id or uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        "content": text,
        "source": source,
        "created_at": datetime(2026, 5, 27, 12, 0, 0, tzinfo=timezone.utc),
        "kind": "fact",
        "category": "other",
        "confidence": 1.0,
        "evidence_count": 1,
        "last_evidence_at": None,
    }[k]
    return r


def _install_pool(monkeypatch, *, fetchrow_return=None, fetch_return=None,
                  fetchval_return=0, execute_return="UPDATE 1"):
    """Replace app.state.pool with an AsyncMock and return it."""
    pool = AsyncMock()
    pool.fetchrow.return_value = fetchrow_return
    pool.fetch.return_value = fetch_return if fetch_return is not None else []
    pool.fetchval.return_value = fetchval_return
    pool.execute.return_value = execute_return
    api_main.app.state.pool = pool
    return pool


# ── GET /personalization ────────────────────────────────────────────────────

def test_get_personalization_returns_disabled_default_when_row_missing(monkeypatch):
    _install_pool(monkeypatch, fetchrow_return=None, fetch_return=[])
    r = TestClient(api_main.app).get(
        "/api/recruiter/11111111-1111-1111-1111-111111111111/personalization")
    assert r.status_code == 200
    assert r.json() == {"enabled": False, "hints": []}


def test_get_personalization_returns_stored_values(monkeypatch):
    fact = _make_fact("user likes builders", "manual")
    pref = MagicMock()
    pref.__getitem__ = lambda self, k: {"personalization_enabled": False}[k]
    _install_pool(monkeypatch, fetchrow_return=pref, fetch_return=[fact])
    r = TestClient(api_main.app).get(
        "/api/recruiter/22222222-2222-2222-2222-222222222222/personalization")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert len(body["hints"]) == 1
    assert body["hints"][0]["text"] == "user likes builders"
    assert body["hints"][0]["source"] == "manual"
    assert "id" in body["hints"][0]
    assert "created_at" in body["hints"][0]


# ── POST /hints ─────────────────────────────────────────────────────────────

def test_post_hint_adds_manual_entry(monkeypatch):
    fact = _make_fact("user likes builders", "manual")
    _install_pool(monkeypatch, fetchrow_return=None, fetch_return=[fact],
                  fetchval_return=0)
    r = TestClient(api_main.app).post(
        "/api/recruiter/33333333-3333-3333-3333-333333333333/hints",
        json={"text": "user likes builders"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["hints"]) == 1
    assert body["hints"][0]["text"] == "user likes builders"
    assert body["hints"][0]["source"] == "manual"
    assert "created_at" in body["hints"][0]


def test_post_hint_with_suggested_source(monkeypatch):
    fact = _make_fact("user likes startups", "suggested")
    _install_pool(monkeypatch, fetchrow_return=None, fetch_return=[fact],
                  fetchval_return=0)
    r = TestClient(api_main.app).post(
        "/api/recruiter/44444444-4444-4444-4444-444444444444/hints",
        json={"text": "user likes startups", "source": "suggested"},
    )
    assert r.status_code == 200
    assert r.json()["hints"][0]["source"] == "suggested"


def test_post_hint_rejects_text_over_200_chars(monkeypatch):
    _install_pool(monkeypatch, fetchrow_return=None)
    r = TestClient(api_main.app).post(
        "/api/recruiter/55555555-5555-5555-5555-555555555555/hints",
        json={"text": "x" * 201},
    )
    assert r.status_code == 400


def test_post_hint_rejects_empty_text(monkeypatch):
    _install_pool(monkeypatch, fetchrow_return=None)
    r = TestClient(api_main.app).post(
        "/api/recruiter/66666666-6666-6666-6666-666666666666/hints",
        json={"text": "   "},
    )
    assert r.status_code == 400


def test_post_hint_rejects_when_at_cap(monkeypatch):
    # fetchval returns 20 — already at cap
    _install_pool(monkeypatch, fetchrow_return=None, fetchval_return=20)
    r = TestClient(api_main.app).post(
        "/api/recruiter/77777777-7777-7777-7777-777777777777/hints",
        json={"text": "one more"},
    )
    assert r.status_code == 400


def test_post_hint_sanitizes_text(monkeypatch):
    fact = _make_fact("user likes  builders", "manual")
    _install_pool(monkeypatch, fetchrow_return=None, fetch_return=[fact],
                  fetchval_return=0)
    r = TestClient(api_main.app).post(
        "/api/recruiter/88888888-8888-8888-8888-888888888888/hints",
        json={"text": "user\x00likes\n\nbuilders"},
    )
    assert r.status_code == 200
    stored = r.json()["hints"][0]["text"]
    assert "\x00" not in stored


# ── DELETE /memory/{id} ─────────────────────────────────────────────────────

def test_delete_memory_entry_dismisses_and_returns_updated(monkeypatch):
    _install_pool(monkeypatch, fetch_return=[], execute_return="UPDATE 1")
    r = TestClient(api_main.app).delete(
        "/api/recruiter/99999999-9999-9999-9999-999999999999"
        "/memory/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    assert r.status_code == 200
    assert r.json() == {"facts": [], "observations": []}


def test_delete_memory_entry_not_found_returns_404(monkeypatch):
    _install_pool(monkeypatch, fetch_return=[], execute_return="UPDATE 0")
    r = TestClient(api_main.app).delete(
        "/api/recruiter/99999999-9999-9999-9999-999999999999"
        "/memory/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
    assert r.status_code == 404


# ── Old index-based delete route is gone ───────────────────────────────────

def test_old_delete_hint_by_index_route_is_gone(monkeypatch):
    _install_pool(monkeypatch)
    r = TestClient(api_main.app).delete(
        "/api/recruiter/99999999-9999-9999-9999-999999999999/hints/1")
    assert r.status_code == 404


# ── PATCH /personalization ──────────────────────────────────────────────────

def test_patch_personalization_toggles_enabled(monkeypatch):
    pool = _install_pool(monkeypatch, fetchrow_return=None)
    r = TestClient(api_main.app).patch(
        "/api/recruiter/cccccccc-cccc-cccc-cccc-cccccccccccc/personalization",
        json={"enabled": False},
    )
    assert r.status_code == 200
    assert r.json() == {"enabled": False}
    assert pool.execute.await_count == 1


def test_patch_personalization_requires_bool(monkeypatch):
    _install_pool(monkeypatch, fetchrow_return=None)
    r = TestClient(api_main.app).patch(
        "/api/recruiter/dddddddd-dddd-dddd-dddd-dddddddddddd/personalization",
        json={"enabled": "not a bool"},
    )
    assert r.status_code == 400


def test_confirm_observation_endpoint_promotes_or_dismisses(monkeypatch):
    async def fake_confirm(pool, recruiter_id, observation_id, accept):
        return {"promoted_to_fact": "prefers React"} if accept else {"dismissed": "prefers React"}

    _install_pool(monkeypatch)
    monkeypatch.setattr("pipeline.agent_tools.do_confirm_observation", fake_confirm)

    r = TestClient(api_main.app).post(
        "/api/recruiter/eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"
        "/observations/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/confirm",
        json={"accept": True},
    )

    assert r.status_code == 200
    assert r.json() == {"promoted_to_fact": "prefers React"}


def test_outcome_reason_endpoint_creates_unconfirmed_observation(monkeypatch):
    pool = _install_pool(monkeypatch)
    pool.fetchrow.return_value = {
        "recruiter_id": uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
        "candidate_id": uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        "full_name": "Ada React",
        "city": "Berlin",
        "country": "Germany",
        "skills": ["react", "typescript"],
        "years_exp": 6,
        "salary_min": 100000,
        "salary_max": 140000,
    }

    async def fake_infer(*, action, reason, candidate):
        assert action == "shortlisted"
        assert "React depth" in reason
        return {
            "category": "skill",
            "content": "Prefers candidates with deep React experience",
            "confidence": 0.82,
        }

    monkeypatch.setattr(api_main, "_infer_outcome_observation", fake_infer)

    r = TestClient(api_main.app).post(
        "/api/recruiter/eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee/outcome-reason",
        json={
            "candidate_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "action": "shortlisted",
            "reason": "Strong React depth and TypeScript experience",
        },
    )

    assert r.status_code == 200
    assert r.json()["observation"]["content"] == "Prefers candidates with deep React experience"
    assert pool.execute.await_count == 1


def test_outcome_reason_endpoint_requires_reason(monkeypatch):
    _install_pool(monkeypatch)
    r = TestClient(api_main.app).post(
        "/api/recruiter/eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee/outcome-reason",
        json={
            "candidate_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "action": "rejected",
            "reason": " ",
        },
    )
    assert r.status_code == 400
