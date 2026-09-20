# Recruiter Agent Copilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a streaming agentic side-panel chat to the recruiter admin UI that can diagnose poor search results, run and refine searches in a loop, query the DB ad-hoc, and save preferences — all with real-time tool-call visibility.

**Architecture:** PydanticAI agent runs inside FastAPI, streams AG-UI protocol events over SSE to the existing vanilla-JS admin.html side panel. Tools call existing pipeline functions directly (no HTTP round-trip). Session state (backtrack stack, role context, candidate pool) lives in-memory keyed by `recruiter_id:session_id`.

**Tech Stack:** Python 3.11+, PydanticAI, FastAPI StreamingResponse, asyncpg, AG-UI SSE protocol, vanilla JS (EventSource + fetch), PostgreSQL FTS

---

## File Structure

```
pipeline/
  agent_session.py     — in-memory session store, StackEntry, RoleContext, AgentSession
  agent_stream.py      — AG-UI SSE event formatters
  agent_tools.py       — business logic for all tool functions (testable standalone)
  agent.py             — PydanticAI Agent definition, tool registration, system prompt
db/migrations/
  010_search_history.sql  — search_history table + read-only DB role
api/
  main.py              — POST /agent/chat, GET /search/history, history background task
  static/
    agent-panel.js     — side panel: SSE consumer, chat rendering, session management
    admin.html         — add side-panel DOM, import agent-panel.js, wire search context
tests/
  test_agent_session.py
  test_agent_stream.py
  test_agent_tools.py
  test_agent_endpoint.py
  test_search_history.py
```

---

## Phase A — Backend

---

### Task 1: Install PydanticAI and update dependencies

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add pydantic-ai to pyproject.toml**

Open `pyproject.toml`. In the `dependencies` list, add after `pydantic-settings`:

```toml
    "pydantic-ai[anthropic]>=0.0.14",
```

- [ ] **Step 2: Install the new dependency**

```bash
pip install "pydantic-ai[anthropic]"
```

Expected: resolves without conflicts. PydanticAI depends on `pydantic>=2.9` which is already in deps.

- [ ] **Step 3: Verify import works**

```bash
python -c "from pydantic_ai import Agent, RunContext; print('ok')"
```

Expected output: `ok`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "feat(deps): add pydantic-ai[anthropic] for agent copilot"
```

---

### Task 2: DB migration — search_history table and read-only role

**Files:**
- Create: `db/migrations/010_search_history.sql`

- [ ] **Step 1: Create the migration file**

Create `db/migrations/010_search_history.sql`:

```sql
-- 010_search_history.sql
-- Permanent log of user-triggered searches from the main panel.
-- Agent intermediate searches are NOT logged here (in-memory only).

CREATE TABLE IF NOT EXISTS search_history (
    id           BIGSERIAL PRIMARY KEY,
    recruiter_id TEXT        NOT NULL,
    query        TEXT,
    filters_json JSONB       DEFAULT '{}'::jsonb,
    results_json JSONB       DEFAULT '[]'::jsonb,  -- top 10 {id, score}
    latency_ms   INT,
    timestamp    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS search_history_recruiter_ts
    ON search_history (recruiter_id, timestamp DESC);

-- Read-only role for the Tier-3 ad-hoc SQL tool.
-- Run as superuser. Skip if role already exists.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_readonly') THEN
        CREATE ROLE agent_readonly NOLOGIN;
    END IF;
END
$$;

GRANT CONNECT ON DATABASE postgres TO agent_readonly;
GRANT USAGE ON SCHEMA public TO agent_readonly;
GRANT SELECT ON candidates, candidate_skills, resume_chunks TO agent_readonly;

-- Create a login user that uses the role.
-- Replace 'changeme' with a real password before running in production.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_ro_user') THEN
        CREATE USER agent_ro_user PASSWORD 'changeme' IN ROLE agent_readonly;
    END IF;
END
$$;
```

- [ ] **Step 2: Apply the migration**

```bash
psql "$DATABASE_URL" -f db/migrations/010_search_history.sql
```

Expected: `CREATE TABLE`, `CREATE INDEX`, `DO` (for each DO block).

- [ ] **Step 3: Verify table exists**

```bash
psql "$DATABASE_URL" -c "\d search_history"
```

Expected: table with columns `id`, `recruiter_id`, `query`, `filters_json`, `results_json`, `latency_ms`, `timestamp`.

- [ ] **Step 4: Add read-only DSN to .env.example**

Open `.env.example` and add:

```
# Read-only DSN for agent ad-hoc SQL tool (uses agent_ro_user role)
AGENT_READONLY_DATABASE_URL=postgresql://agent_ro_user:changeme@localhost:5432/yourdb
```

Add the same line (with real password) to your local `.env`.

- [ ] **Step 5: Commit**

```bash
git add db/migrations/010_search_history.sql .env.example
git commit -m "feat(db): search_history table + agent read-only role"
```

---

### Task 3: Agent session store

**Files:**
- Create: `pipeline/agent_session.py`
- Create: `tests/test_agent_session.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_agent_session.py`:

```python
import pytest
from pipeline.agent_session import (
    AgentSession, StackEntry, RoleContext,
    get_or_create, push_to_stack, pop_from_stack, clear,
)


def test_get_or_create_returns_same_session():
    s1 = get_or_create("recruiter-1", "session-a")
    s2 = get_or_create("recruiter-1", "session-a")
    assert s1 is s2


def test_get_or_create_different_session_ids_are_isolated():
    s1 = get_or_create("recruiter-1", "session-a")
    s2 = get_or_create("recruiter-1", "session-b")
    assert s1 is not s2


def test_push_and_pop_stack():
    session = get_or_create("recruiter-2", "session-x")
    session.search_stack.clear()

    entry1 = StackEntry(query="python dev", filters={}, results_preview=[], agent_reasoning="first")
    entry2 = StackEntry(query="senior python", filters={}, results_preview=[], agent_reasoning="second")

    push_to_stack(session, entry1)
    push_to_stack(session, entry2)

    assert len(session.search_stack) == 2

    popped = pop_from_stack(session)
    assert popped is entry2
    assert len(session.search_stack) == 1


def test_pop_from_single_entry_returns_none():
    session = get_or_create("recruiter-3", "session-y")
    session.search_stack.clear()
    push_to_stack(session, StackEntry(query="q", filters={}, results_preview=[], agent_reasoning=""))
    result = pop_from_stack(session)
    assert result is None  # cannot go back further


def test_clear_removes_session():
    get_or_create("recruiter-4", "session-z")
    clear("recruiter-4", "session-z")
    # New call creates a fresh session (different object)
    s_new = get_or_create("recruiter-4", "session-z")
    assert s_new.messages == []
    assert s_new.search_stack == []
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
pytest tests/test_agent_session.py -v
```

Expected: `ModuleNotFoundError: No module named 'pipeline.agent_session'`

- [ ] **Step 3: Implement pipeline/agent_session.py**

Create `pipeline/agent_session.py`:

```python
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

_sessions: dict[str, "AgentSession"] = {}


@dataclass
class StackEntry:
    query: str
    filters: dict[str, Any]
    results_preview: list[dict]   # top 3 candidate dicts for chat display
    agent_reasoning: str
    iteration_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)


@dataclass
class RoleContext:
    soft_preferences: list[str] = field(default_factory=list)
    negative_preferences: list[str] = field(default_factory=list)
    example_good_ids: list[str] = field(default_factory=list)


@dataclass
class AgentSession:
    recruiter_id: str
    session_id: str
    messages: list[Any] = field(default_factory=list)   # PydanticAI message history
    search_stack: list[StackEntry] = field(default_factory=list)
    role_context: RoleContext = field(default_factory=RoleContext)
    candidate_pool: list[dict] = field(default_factory=list)  # Tier 2 pool
    created_at: float = field(default_factory=time.time)


def _key(recruiter_id: str, session_id: str) -> str:
    return f"{recruiter_id}:{session_id}"


def get_or_create(recruiter_id: str, session_id: str) -> AgentSession:
    k = _key(recruiter_id, session_id)
    if k not in _sessions:
        _sessions[k] = AgentSession(recruiter_id=recruiter_id, session_id=session_id)
    return _sessions[k]


def push_to_stack(session: AgentSession, entry: StackEntry) -> None:
    session.search_stack.append(entry)


def pop_from_stack(session: AgentSession) -> StackEntry | None:
    """Pop and return the top entry. Returns None if only one entry remains (can't go back)."""
    if len(session.search_stack) <= 1:
        return None
    return session.search_stack.pop()


def clear(recruiter_id: str, session_id: str) -> None:
    _sessions.pop(_key(recruiter_id, session_id), None)
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
pytest tests/test_agent_session.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add pipeline/agent_session.py tests/test_agent_session.py
git commit -m "feat(agent): in-memory session store with backtrack stack"
```

---

### Task 4: AG-UI SSE event helpers

**Files:**
- Create: `pipeline/agent_stream.py`
- Create: `tests/test_agent_stream.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_agent_stream.py`:

```python
import json
from pipeline.agent_stream import (
    sse_event, text_start, text_delta, text_end,
    tool_call_start, tool_call_end,
    search_results_event, push_to_main_event, stack_updated_event, error_event,
)


def _parse(raw: str) -> tuple[str, dict]:
    """Parse raw SSE string into (event_type, data_dict)."""
    lines = raw.strip().split("\n")
    event_type = lines[0].split(": ", 1)[1]
    data = json.loads(lines[1].split(": ", 1)[1])
    return event_type, data


def test_sse_event_format():
    raw = sse_event("FOO", {"x": 1})
    assert raw == 'event: FOO\ndata: {"x": 1}\n\n'


def test_text_start_has_message_id_and_role():
    raw = text_start("msg-1")
    event_type, data = _parse(raw)
    assert event_type == "TEXT_MESSAGE_START"
    assert data["messageId"] == "msg-1"
    assert data["role"] == "assistant"


def test_text_delta_has_delta():
    raw = text_delta("msg-1", "hello")
    event_type, data = _parse(raw)
    assert event_type == "TEXT_MESSAGE_CONTENT"
    assert data["delta"] == "hello"


def test_tool_call_start_has_name():
    raw = tool_call_start("tc-1", "run_search")
    event_type, data = _parse(raw)
    assert event_type == "TOOL_CALL_START"
    assert data["toolCallName"] == "run_search"


def test_tool_call_end_has_id():
    raw = tool_call_end("tc-1", "found 8 results")
    event_type, data = _parse(raw)
    assert event_type == "TOOL_CALL_END"
    assert data["toolCallId"] == "tc-1"
    assert "found 8 results" in data["resultSummary"]


def test_search_results_event_has_results():
    results = [{"id": "c-1", "name": "Ada"}]
    raw = search_results_event(results, "iter-1")
    event_type, data = _parse(raw)
    assert event_type == "SEARCH_RESULTS"
    assert data["results"][0]["id"] == "c-1"


def test_stack_updated_event_has_depth():
    raw = stack_updated_event(3)
    event_type, data = _parse(raw)
    assert event_type == "STACK_UPDATED"
    assert data["depth"] == 3
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
pytest tests/test_agent_stream.py -v
```

Expected: `ModuleNotFoundError: No module named 'pipeline.agent_stream'`

- [ ] **Step 3: Implement pipeline/agent_stream.py**

Create `pipeline/agent_stream.py`:

```python
from __future__ import annotations

import json
from typing import Any


def sse_event(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def text_start(message_id: str) -> str:
    return sse_event("TEXT_MESSAGE_START", {"messageId": message_id, "role": "assistant"})


def text_delta(message_id: str, delta: str) -> str:
    return sse_event("TEXT_MESSAGE_CONTENT", {"messageId": message_id, "delta": delta})


def text_end(message_id: str) -> str:
    return sse_event("TEXT_MESSAGE_END", {"messageId": message_id})


def tool_call_start(tool_call_id: str, tool_name: str) -> str:
    return sse_event("TOOL_CALL_START", {
        "toolCallId": tool_call_id,
        "toolCallName": tool_name,
    })


def tool_call_end(tool_call_id: str, result_summary: str = "") -> str:
    return sse_event("TOOL_CALL_END", {
        "toolCallId": tool_call_id,
        "resultSummary": result_summary,
    })


def search_results_event(results: list[dict], iteration_id: str) -> str:
    return sse_event("SEARCH_RESULTS", {
        "results": results,
        "iterationId": iteration_id,
    })


def push_to_main_event(query: str, filters: dict, result_ids: list[str]) -> str:
    return sse_event("PUSH_TO_MAIN", {
        "query": query,
        "filters": filters,
        "resultIds": result_ids,
    })


def stack_updated_event(depth: int) -> str:
    return sse_event("STACK_UPDATED", {"depth": depth})


def error_event(message: str) -> str:
    return sse_event("ERROR", {"message": message})
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
pytest tests/test_agent_stream.py -v
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add pipeline/agent_stream.py tests/test_agent_stream.py
git commit -m "feat(agent): AG-UI SSE event formatters"
```

---

### Task 5: Agent tools — Tier 1 (pipeline tools)

**Files:**
- Create: `pipeline/agent_tools.py`
- Create: `tests/test_agent_tools.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_agent_tools.py`:

```python
import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from pipeline.agent_tools import (
    do_run_search,
    do_explain_poor_results,
    do_compare_iterations,
    do_get_candidate_detail,
    do_save_hint,
)
from pipeline.agent_session import AgentSession, StackEntry, RoleContext


def _make_session() -> AgentSession:
    return AgentSession(recruiter_id="r-1", session_id="s-1")


def _make_entry(query="python dev", n_results=3) -> StackEntry:
    return StackEntry(
        query=query,
        filters={},
        results_preview=[{"id": f"c-{i}", "name": f"Cand {i}", "score": 0.9 - i * 0.1}
                         for i in range(n_results)],
        agent_reasoning="initial search",
    )


@pytest.mark.asyncio
async def test_do_run_search_pushes_entry_to_stack(monkeypatch):
    session = _make_session()
    mock_pool = AsyncMock()

    # Patch the SearchEngine to avoid real DB
    mock_resp = MagicMock()
    mock_resp.results = [
        MagicMock(candidate_id=f"c-{i}", feature_score=0.9 - i * 0.05, full_name=f"Cand {i}",
                  city="Bangalore", years_exp=5, skills=["python"])
        for i in range(5)
    ]
    mock_resp.spec = MagicMock(query_text="python dev", must_filters=MagicMock())

    with patch("pipeline.agent_tools.SearchEngine") as MockEngine:
        instance = MockEngine.return_value
        instance.smart_search = AsyncMock(return_value=mock_resp)

        result = await do_run_search(
            pool=mock_pool,
            session=session,
            recruiter_id="r-1",
            query="python dev",
            filters={},
            weights={},
        )

    assert len(session.search_stack) == 1
    assert session.search_stack[0].query == "python dev"
    assert "results" in result
    assert len(result["results"]) <= 3  # preview is top 3


@pytest.mark.asyncio
async def test_do_compare_iterations_shows_diff():
    entry_a = _make_entry("python dev", n_results=3)
    entry_b = _make_entry("senior python dev", n_results=3)
    entry_b.results_preview[0]["id"] = "c-new"

    result = await do_compare_iterations(entry_a, entry_b)

    assert "query" in result
    assert result["query"]["before"] == "python dev"
    assert result["query"]["after"] == "senior python dev"


@pytest.mark.asyncio
async def test_do_get_candidate_detail_queries_db():
    mock_pool = AsyncMock()
    mock_pool.fetchrow = AsyncMock(return_value={
        "id": "c-1",
        "full_name": "Ada Lovelace",
        "city": "Bangalore",
        "country": "India",
        "years_exp": 8,
        "salary_min": 100000,
        "salary_max": 140000,
    })

    result = await do_get_candidate_detail(mock_pool, "c-1")

    assert result["full_name"] == "Ada Lovelace"
    mock_pool.fetchrow.assert_called_once()


@pytest.mark.asyncio
async def test_do_save_hint_writes_to_recruiter_preferences():
    mock_pool = AsyncMock()
    mock_pool.execute = AsyncMock()

    await do_save_hint(mock_pool, "r-1", "prefers startup builders")

    mock_pool.execute.assert_called_once()
    call_args = mock_pool.execute.call_args[0]
    assert "recruiter_preferences" in call_args[0]
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
pytest tests/test_agent_tools.py -v
```

Expected: `ModuleNotFoundError: No module named 'pipeline.agent_tools'`

- [ ] **Step 3: Implement Tier 1 business logic in pipeline/agent_tools.py**

Create `pipeline/agent_tools.py`:

```python
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import asyncpg

from pipeline.agent_session import AgentSession, StackEntry, push_to_stack
from pipeline.search import SearchEngine

logger = logging.getLogger(__name__)

# ── Tier 1: Pipeline tools ────────────────────────────────────────────────────

async def do_run_search(
    pool: asyncpg.Pool,
    session: AgentSession,
    recruiter_id: str,
    query: str,
    filters: dict[str, Any],
    weights: dict[str, Any],
) -> dict[str, Any]:
    """Run a full hybrid search and push the iteration onto the session stack."""
    engine = SearchEngine(pool)
    resp = await engine.smart_search(
        query=query,
        explicit_filters=filters or None,
        mode="quality",
        recruiter_id=recruiter_id,
        config_overrides=weights or {},
    )

    preview = [
        {
            "id": r.candidate_id,
            "name": getattr(r, "full_name", ""),
            "city": getattr(r, "city", ""),
            "years_exp": getattr(r, "years_exp", None),
            "skills": list(getattr(r, "skills", []))[:5],
            "score": round(r.feature_score, 3),
        }
        for r in resp.results[:3]
    ]

    entry = StackEntry(
        query=query,
        filters=filters or {},
        results_preview=preview,
        agent_reasoning="",
    )
    push_to_stack(session, entry)

    return {
        "results": preview,
        "total": len(resp.results),
        "iteration_id": entry.iteration_id,
        "spec_summary": {
            "query_text": getattr(getattr(resp, "spec", None), "query_text", query),
        },
    }


async def do_modify_and_search(
    pool: asyncpg.Pool,
    session: AgentSession,
    recruiter_id: str,
    changes: dict[str, Any],
) -> dict[str, Any]:
    """Apply changes to the current stack top and run a new search."""
    base = session.search_stack[-1] if session.search_stack else None
    base_query = changes.get("query") or (base.query if base else "")
    base_filters = {**(base.filters if base else {}), **(changes.get("add_filters") or {})}
    for key in (changes.get("remove_filters") or []):
        base_filters.pop(key, None)

    return await do_run_search(pool, session, recruiter_id, base_query, base_filters, {})


async def do_explain_poor_results(
    pool: asyncpg.Pool,
    session: AgentSession,
) -> dict[str, Any]:
    """Return a diagnostic summary of why current results may be poor."""
    if not session.search_stack:
        return {"explanation": "No search has been run yet in this session."}

    top = session.search_stack[-1]
    results = top.results_preview
    if not results:
        return {"explanation": "The last search returned no results."}

    scores = [r["score"] for r in results]
    avg_score = sum(scores) / len(scores) if scores else 0

    issues = []
    if avg_score < 0.5:
        issues.append(f"Low average relevance score ({avg_score:.2f}). Candidates may not match the query well.")
    if len(results) < 3:
        issues.append("Very few results returned — filters may be too restrictive.")
    if top.filters:
        issues.append(f"Active hard filters: {list(top.filters.keys())}. Try relaxing some.")

    return {
        "query": top.query,
        "filters": top.filters,
        "result_count": len(results),
        "avg_score": round(avg_score, 3),
        "potential_issues": issues or ["Scores look reasonable — the query may need rephrasing."],
    }


async def do_compare_iterations(
    entry_a: StackEntry,
    entry_b: StackEntry,
) -> dict[str, Any]:
    """Compare two search iterations and return a delta summary."""
    ids_a = {r["id"] for r in entry_a.results_preview}
    ids_b = {r["id"] for r in entry_b.results_preview}

    return {
        "query": {"before": entry_a.query, "after": entry_b.query},
        "filters": {"before": entry_a.filters, "after": entry_b.filters},
        "results_gained": list(ids_b - ids_a),
        "results_lost": list(ids_a - ids_b),
        "results_kept": list(ids_a & ids_b),
    }


async def do_get_candidate_detail(
    pool: asyncpg.Pool,
    candidate_id: str,
) -> dict[str, Any]:
    """Fetch full candidate profile from the DB."""
    row = await pool.fetchrow(
        """SELECT id, full_name, city, country, years_exp, salary_min, salary_max
           FROM candidates WHERE id = $1::uuid""",
        candidate_id,
    )
    if not row:
        return {"error": f"Candidate {candidate_id} not found"}

    skills_rows = await pool.fetch(
        "SELECT skill FROM candidate_skills WHERE candidate_id = $1::uuid ORDER BY skill",
        candidate_id,
    )
    skills = [r["skill"] for r in skills_rows]

    chunk = await pool.fetchrow(
        """SELECT content FROM resume_chunks
           WHERE candidate_id = $1::uuid ORDER BY created_at LIMIT 1""",
        candidate_id,
    )

    return {
        "id": str(row["id"]),
        "full_name": row["full_name"],
        "city": row["city"],
        "country": row["country"],
        "years_exp": row["years_exp"],
        "salary_min": row["salary_min"],
        "salary_max": row["salary_max"],
        "skills": skills,
        "best_chunk": chunk["content"][:400] if chunk else "",
    }


async def do_save_hint(
    pool: asyncpg.Pool,
    recruiter_id: str,
    hint_text: str,
) -> dict[str, Any]:
    """Append a personalization hint to the recruiter's preferences."""
    hint_text = hint_text.strip()[:200]
    if not hint_text:
        return {"error": "Hint text is empty"}

    await pool.execute(
        """INSERT INTO recruiter_preferences (recruiter_id, personalization_hints)
           VALUES ($1::uuid, $2::jsonb)
           ON CONFLICT (recruiter_id) DO UPDATE
           SET personalization_hints = (
               SELECT jsonb_agg(h) FROM (
                   SELECT jsonb_array_elements(
                       COALESCE(recruiter_preferences.personalization_hints, '[]'::jsonb)
                   ) AS h
                   UNION ALL
                   SELECT $2::jsonb->0
               ) sub
               LIMIT 20
           )""",
        recruiter_id,
        json.dumps([{"text": hint_text, "source": "agent", "created_at": str(asyncio.get_event_loop().time())}]),
    )
    return {"saved": hint_text}
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
pytest tests/test_agent_tools.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add pipeline/agent_tools.py tests/test_agent_tools.py
git commit -m "feat(agent): Tier 1 tool business logic (pipeline, hints, profile)"
```

---

### Task 6: Agent tools — Tier 2 (DB / pool tools)

**Files:**
- Modify: `pipeline/agent_tools.py`
- Modify: `tests/test_agent_tools.py`

- [ ] **Step 1: Add failing tests to tests/test_agent_tools.py**

Append to `tests/test_agent_tools.py`:

```python
from pipeline.agent_tools import (
    do_keyword_search,
    do_load_candidate_pool,
    do_filter_from_pool,
    do_aggregate_pool,
)


@pytest.mark.asyncio
async def test_do_keyword_search_returns_candidates():
    mock_pool = AsyncMock()
    mock_pool.fetch = AsyncMock(return_value=[
        {"id": "c-1", "full_name": "Ada", "city": "Bangalore", "years_exp": 6, "score": 0.8},
    ])
    results = await do_keyword_search(mock_pool, "machine learning")
    assert len(results) == 1
    assert results[0]["full_name"] == "Ada"


@pytest.mark.asyncio
async def test_do_load_candidate_pool_populates_session():
    session = _make_session()
    mock_pool = AsyncMock()
    mock_pool.fetch = AsyncMock(return_value=[
        {"id": "c-1", "full_name": "Ada", "city": "Bangalore", "country": "India",
         "years_exp": 6, "salary_min": 100000, "salary_max": 140000},
        {"id": "c-2", "full_name": "Grace", "city": "Mumbai", "country": "India",
         "years_exp": 4, "salary_min": 80000, "salary_max": 110000},
    ])
    result = await do_load_candidate_pool(mock_pool, session, criteria={}, limit=50)
    assert session.candidate_pool == result["pool"]
    assert len(result["pool"]) == 2


def test_do_filter_from_pool_by_city():
    session = _make_session()
    session.candidate_pool = [
        {"id": "c-1", "city": "Bangalore", "years_exp": 6},
        {"id": "c-2", "city": "Mumbai", "years_exp": 4},
    ]
    result = do_filter_from_pool(session, {"city": "Bangalore"})
    assert len(result) == 1
    assert result[0]["id"] == "c-1"


def test_do_aggregate_pool_counts_by_city():
    session = _make_session()
    session.candidate_pool = [
        {"id": "c-1", "city": "Bangalore"},
        {"id": "c-2", "city": "Bangalore"},
        {"id": "c-3", "city": "Mumbai"},
    ]
    result = do_aggregate_pool(session, "city")
    assert result["Bangalore"] == 2
    assert result["Mumbai"] == 1
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_agent_tools.py -v -k "keyword or pool or filter or aggregate"
```

Expected: `ImportError` — functions not yet defined.

- [ ] **Step 3: Add Tier 2 functions to pipeline/agent_tools.py**

Append to `pipeline/agent_tools.py`:

```python
# ── Tier 2: Lightweight DB / pool tools ───────────────────────────────────────

async def do_keyword_search(
    pool: asyncpg.Pool,
    query: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """BM25-style FTS keyword search directly against the candidates table."""
    rows = await pool.fetch(
        """SELECT id, full_name, city, country, years_exp,
                  ts_rank(to_tsvector('english', COALESCE(resume_text, '')),
                          plainto_tsquery('english', $1)) AS score
           FROM candidates
           WHERE status = 'active'
             AND (
               to_tsvector('english', COALESCE(resume_text, '')) @@ plainto_tsquery('english', $1)
               OR full_name ILIKE '%' || $1 || '%'
             )
           ORDER BY score DESC
           LIMIT $2""",
        query, limit,
    )
    return [
        {
            "id": str(r["id"]),
            "full_name": r["full_name"],
            "city": r["city"],
            "country": r["country"],
            "years_exp": r["years_exp"],
            "score": float(r["score"]),
        }
        for r in rows
    ]


async def do_load_candidate_pool(
    pool: asyncpg.Pool,
    session: AgentSession,
    criteria: dict[str, Any],
    limit: int = 100,
) -> dict[str, Any]:
    """Load up to `limit` candidates matching simple criteria into the session pool."""
    clauses = ["c.status = 'active'"]
    params: list[Any] = []
    idx = 1

    if criteria.get("city"):
        clauses.append(f"c.city ILIKE '%' || ${idx} || '%'")
        params.append(criteria["city"])
        idx += 1
    if criteria.get("min_years_exp"):
        clauses.append(f"c.years_exp >= ${idx}")
        params.append(criteria["min_years_exp"])
        idx += 1
    if criteria.get("max_salary"):
        clauses.append(f"c.salary_max <= ${idx}")
        params.append(criteria["max_salary"])
        idx += 1

    params.append(limit)
    where = " AND ".join(clauses)

    rows = await pool.fetch(
        f"""SELECT c.id, c.full_name, c.city, c.country, c.years_exp,
                   c.salary_min, c.salary_max
            FROM candidates c
            WHERE {where}
            LIMIT ${idx}""",
        *params,
    )
    pool_data = [
        {
            "id": str(r["id"]),
            "full_name": r["full_name"],
            "city": r["city"],
            "country": r["country"],
            "years_exp": r["years_exp"],
            "salary_min": r["salary_min"],
            "salary_max": r["salary_max"],
        }
        for r in rows
    ]
    session.candidate_pool = pool_data
    return {"pool": pool_data, "count": len(pool_data)}


def do_filter_from_pool(
    session: AgentSession,
    criteria: dict[str, Any],
) -> list[dict[str, Any]]:
    """Filter the session's candidate pool in-memory by simple key=value criteria."""
    results = session.candidate_pool
    for key, value in criteria.items():
        if isinstance(value, str):
            results = [c for c in results if str(c.get(key, "")).lower() == value.lower()]
        elif isinstance(value, (int, float)):
            results = [c for c in results if c.get(key) == value]
    return results


def do_aggregate_pool(
    session: AgentSession,
    dimension: str,
) -> dict[str, int]:
    """Count pool candidates grouped by a dimension field (e.g. 'city', 'country')."""
    counts: dict[str, int] = {}
    for c in session.candidate_pool:
        val = str(c.get(dimension, "unknown"))
        counts[val] = counts.get(val, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))
```

- [ ] **Step 4: Run all agent tool tests**

```bash
pytest tests/test_agent_tools.py -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pipeline/agent_tools.py tests/test_agent_tools.py
git commit -m "feat(agent): Tier 2 tool business logic (keyword search, candidate pool)"
```

---

### Task 7: Agent tools — Tier 3 (ad-hoc SQL)

**Files:**
- Modify: `pipeline/agent_tools.py`
- Modify: `tests/test_agent_tools.py`

- [ ] **Step 1: Add read-only DSN to pipeline settings**

Open `pipeline/config.py` (or wherever `settings` is defined — check `pipeline/settings.py` or `pipeline/config.py`). Add:

```python
agent_readonly_database_url: str = Field(
    default="",
    validation_alias=AliasChoices("AGENT_READONLY_DATABASE_URL", "DATABASE_URL"),
)
```

If the settings file uses `pydantic-settings` with `model_config`, add it to the model. If it uses a simple `os.getenv` pattern, add:

```python
agent_readonly_database_url: str = os.getenv(
    "AGENT_READONLY_DATABASE_URL",
    os.getenv("DATABASE_URL", ""),
)
```

- [ ] **Step 2: Add failing tests**

Append to `tests/test_agent_tools.py`:

```python
from pipeline.agent_tools import do_query_candidates_db


@pytest.mark.asyncio
async def test_do_query_candidates_db_blocks_dml():
    mock_pool = AsyncMock()
    for bad_sql in [
        "DELETE FROM candidates",
        "UPDATE candidates SET status='inactive'",
        "DROP TABLE candidates",
        "INSERT INTO candidates VALUES (1)",
        "TRUNCATE candidates",
        "ALTER TABLE candidates ADD COLUMN x int",
    ]:
        result = await do_query_candidates_db(mock_pool, bad_sql)
        assert "error" in result, f"Expected error for: {bad_sql}"
        assert "not allowed" in result["error"].lower()
    mock_pool.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_do_query_candidates_db_injects_limit():
    mock_pool = AsyncMock()
    mock_pool.fetch = AsyncMock(return_value=[
        {"id": "c-1", "full_name": "Ada"},
    ])
    result = await do_query_candidates_db(mock_pool, "SELECT id, full_name FROM candidates")
    assert result["rows"][0]["full_name"] == "Ada"
    # Verify LIMIT was injected into the actual SQL sent to DB
    call_sql = mock_pool.fetch.call_args[0][0]
    assert "LIMIT 50" in call_sql.upper()


@pytest.mark.asyncio
async def test_do_query_candidates_db_blocks_disallowed_tables():
    mock_pool = AsyncMock()
    result = await do_query_candidates_db(
        mock_pool, "SELECT * FROM recruiter_preferences"
    )
    assert "error" in result
    assert "not allowed" in result["error"].lower()
```

- [ ] **Step 3: Run to confirm failure**

```bash
pytest tests/test_agent_tools.py -v -k "query_candidates"
```

Expected: `ImportError`.

- [ ] **Step 4: Add Tier 3 function to pipeline/agent_tools.py**

Append to `pipeline/agent_tools.py`:

```python
# ── Tier 3: Ad-hoc read-only SQL ─────────────────────────────────────────────

_BLOCKED_KEYWORDS = {"UPDATE", "DELETE", "INSERT", "DROP", "TRUNCATE", "ALTER", "CREATE", "GRANT", "REVOKE"}
_ALLOWED_TABLES = {"candidates", "candidate_skills", "resume_chunks"}


def _check_sql_safety(sql: str) -> str | None:
    """Return an error message if SQL is not safe, else None."""
    upper = sql.upper()
    for kw in _BLOCKED_KEYWORDS:
        if kw in upper.split():
            return f"Query contains '{kw}' which is not allowed. Only SELECT is permitted."
    # Basic table allowlist check — heuristic, not a substitute for the read-only role
    import re
    tables_in_query = {t.lower() for t in re.findall(r"FROM\s+(\w+)", upper)}
    tables_in_query |= {t.lower() for t in re.findall(r"JOIN\s+(\w+)", upper)}
    disallowed = tables_in_query - _ALLOWED_TABLES
    if disallowed:
        return f"Tables not allowed: {disallowed}. Allowed: {_ALLOWED_TABLES}"
    return None


def _inject_limit(sql: str, limit: int = 50) -> str:
    """Append LIMIT if not already present."""
    if "LIMIT" not in sql.upper():
        return sql.rstrip("; \n") + f" LIMIT {limit}"
    return sql


async def do_query_candidates_db(
    pool: asyncpg.Pool,
    sql: str,
) -> dict[str, Any]:
    """Execute an ad-hoc read-only SELECT against the candidates schema."""
    error = _check_sql_safety(sql)
    if error:
        return {"error": error}

    safe_sql = _inject_limit(sql)

    try:
        rows = await asyncio.wait_for(pool.fetch(safe_sql), timeout=5.0)
        return {"rows": [dict(r) for r in rows], "count": len(rows)}
    except asyncio.TimeoutError:
        return {"error": "Query timed out after 5 seconds"}
    except Exception as exc:
        logger.warning("Agent SQL query failed: %s | sql=%s", exc, safe_sql[:200])
        return {"error": f"Query failed: {exc}"}
```

- [ ] **Step 5: Run all agent tool tests**

```bash
pytest tests/test_agent_tools.py -v
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add pipeline/agent_tools.py tests/test_agent_tools.py pipeline/config.py
git commit -m "feat(agent): Tier 3 ad-hoc SQL tool with DML block and LIMIT injection"
```

---

### Task 8: PydanticAI agent definition and system prompt

**Files:**
- Create: `pipeline/agent.py`
- Create: `tests/test_agent_endpoint.py` (skeleton, full tests in Task 9)

- [ ] **Step 1: Write a failing import test**

Create `tests/test_agent_endpoint.py`:

```python
import pytest


def test_agent_module_imports():
    from pipeline.agent import recruiter_agent, AgentDeps, build_system_prompt
    assert recruiter_agent is not None


def test_build_system_prompt_includes_context():
    from pipeline.agent import build_system_prompt
    prompt = build_system_prompt(
        query="python dev",
        filters={"city": "Bangalore"},
        result_ids=["c-1", "c-2"],
        hints=["prefers startup builders"],
    )
    assert "python dev" in prompt
    assert "Bangalore" in prompt
    assert "prefers startup builders" in prompt
    assert "candidates" in prompt.lower()  # DB schema block


def test_build_system_prompt_empty_context():
    from pipeline.agent import build_system_prompt
    prompt = build_system_prompt(query="", filters={}, result_ids=[], hints=[])
    assert "recruiter" in prompt.lower()
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_agent_endpoint.py -v -k "import or prompt"
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement pipeline/agent.py**

Create `pipeline/agent.py`:

```python
from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

import asyncpg
from pydantic_ai import Agent, RunContext

from pipeline.agent_session import AgentSession, StackEntry
from pipeline.agent_tools import (
    do_run_search, do_modify_and_search, do_explain_poor_results,
    do_compare_iterations, do_get_candidate_detail, do_save_hint,
    do_keyword_search, do_load_candidate_pool, do_filter_from_pool,
    do_aggregate_pool, do_query_candidates_db,
)

_DB_SCHEMA = """
candidates:        id (uuid), full_name (text), city (text), country (text),
                   years_exp (int), salary_min (numeric), salary_max (numeric),
                   status (text — filter to 'active'), resume_text (text)
candidate_skills:  candidate_id (uuid), skill (text)
resume_chunks:     candidate_id (uuid), content (text), chunk_type (text)
"""


def build_system_prompt(
    query: str,
    filters: dict[str, Any],
    result_ids: list[str],
    hints: list[str],
) -> str:
    hints_block = (
        "\n".join(f"- {h}" for h in hints)
        if hints else "No saved preferences yet."
    )
    filters_str = json.dumps(filters, indent=2) if filters else "none"
    results_str = ", ".join(result_ids[:10]) if result_ids else "none"

    return f"""You are an expert recruiter assistant helping refine candidate searches.

## Current Search Context
Query: {query or "(none)"}
Active Filters: {filters_str}
Current Result IDs (top 10): {results_str}

## About This Recruiter
{hints_block}

## Tool Guide
- run_search: Full hybrid pipeline search. Use for major query rethinks.
- modify_and_search: Apply incremental changes to the current search (add filter, relax constraint). Prefer this over run_search for small tweaks.
- explain_poor_results: Diagnose why current results may be poor. Call this BEFORE suggesting fixes.
- compare_iterations: Show what changed between two search iterations.
- get_candidate_detail: Fetch full profile for a specific candidate ID.
- save_hint: Save a confirmed preference to the recruiter's profile. Always ask before saving.
- push_to_main_panel: Push current search state to the main UI panel.
- keyword_search_candidates: Fast FTS search — use when the full pipeline is overkill.
- load_candidate_pool: Load a set of candidates into session for fast in-memory filtering.
- filter_from_pool: Filter the loaded pool in-memory (no DB call).
- aggregate_pool: Count/group pool by a field (city, country, etc.).
- query_candidates_db: Ad-hoc SELECT. Available tables and columns:
{_DB_SCHEMA}

## Behaviour Rules
1. Diagnose before acting — call explain_poor_results before running a new search.
2. Prefer modify_and_search over run_search for incremental changes.
3. Always ask before saving hints ("Want me to remember this for future searches?").
4. Show reasoning — be explicit about why you're making a change.
5. Be concise. Recruiters are time-constrained.
"""


@dataclass
class AgentDeps:
    pool: asyncpg.Pool
    session: AgentSession
    recruiter_id: str
    event_queue: asyncio.Queue  # SSE events pushed here from tools


recruiter_agent: Agent[AgentDeps, str] = Agent(
    model="claude-sonnet-4-6",
    deps_type=AgentDeps,
)


# ── Tool registrations ────────────────────────────────────────────────────────

@recruiter_agent.tool
async def run_search(
    ctx: RunContext[AgentDeps],
    query: str,
    filters: dict[str, Any] | None = None,
    weights: dict[str, Any] | None = None,
) -> str:
    tc_id = str(uuid.uuid4())
    await ctx.deps.event_queue.put({"type": "TOOL_CALL_START",
                                     "data": {"toolCallId": tc_id, "toolCallName": "run_search"}})
    result = await do_run_search(ctx.deps.pool, ctx.deps.session, ctx.deps.recruiter_id,
                                  query, filters or {}, weights or {})
    await ctx.deps.event_queue.put({"type": "SEARCH_RESULTS",
                                     "data": {"results": result["results"],
                                              "iterationId": result["iteration_id"]}})
    await ctx.deps.event_queue.put({"type": "STACK_UPDATED",
                                     "data": {"depth": len(ctx.deps.session.search_stack)}})
    await ctx.deps.event_queue.put({"type": "TOOL_CALL_END",
                                     "data": {"toolCallId": tc_id,
                                              "resultSummary": f"{result['total']} results"}})
    return json.dumps(result)


@recruiter_agent.tool
async def modify_and_search(
    ctx: RunContext[AgentDeps],
    changes: dict[str, Any],
) -> str:
    tc_id = str(uuid.uuid4())
    await ctx.deps.event_queue.put({"type": "TOOL_CALL_START",
                                     "data": {"toolCallId": tc_id, "toolCallName": "modify_and_search"}})
    result = await do_modify_and_search(ctx.deps.pool, ctx.deps.session,
                                         ctx.deps.recruiter_id, changes)
    await ctx.deps.event_queue.put({"type": "SEARCH_RESULTS",
                                     "data": {"results": result["results"],
                                              "iterationId": result["iteration_id"]}})
    await ctx.deps.event_queue.put({"type": "STACK_UPDATED",
                                     "data": {"depth": len(ctx.deps.session.search_stack)}})
    await ctx.deps.event_queue.put({"type": "TOOL_CALL_END",
                                     "data": {"toolCallId": tc_id,
                                              "resultSummary": f"{result['total']} results"}})
    return json.dumps(result)


@recruiter_agent.tool
async def explain_poor_results(ctx: RunContext[AgentDeps]) -> str:
    tc_id = str(uuid.uuid4())
    await ctx.deps.event_queue.put({"type": "TOOL_CALL_START",
                                     "data": {"toolCallId": tc_id, "toolCallName": "explain_poor_results"}})
    result = await do_explain_poor_results(ctx.deps.pool, ctx.deps.session)
    await ctx.deps.event_queue.put({"type": "TOOL_CALL_END",
                                     "data": {"toolCallId": tc_id, "resultSummary": "diagnosis ready"}})
    return json.dumps(result)


@recruiter_agent.tool
async def get_candidate_detail(ctx: RunContext[AgentDeps], candidate_id: str) -> str:
    tc_id = str(uuid.uuid4())
    await ctx.deps.event_queue.put({"type": "TOOL_CALL_START",
                                     "data": {"toolCallId": tc_id, "toolCallName": "get_candidate_detail"}})
    result = await do_get_candidate_detail(ctx.deps.pool, candidate_id)
    await ctx.deps.event_queue.put({"type": "TOOL_CALL_END",
                                     "data": {"toolCallId": tc_id,
                                              "resultSummary": result.get("full_name", candidate_id)}})
    return json.dumps(result)


@recruiter_agent.tool
async def save_hint(ctx: RunContext[AgentDeps], hint_text: str) -> str:
    tc_id = str(uuid.uuid4())
    await ctx.deps.event_queue.put({"type": "TOOL_CALL_START",
                                     "data": {"toolCallId": tc_id, "toolCallName": "save_hint"}})
    result = await do_save_hint(ctx.deps.pool, ctx.deps.recruiter_id, hint_text)
    await ctx.deps.event_queue.put({"type": "TOOL_CALL_END",
                                     "data": {"toolCallId": tc_id, "resultSummary": "hint saved"}})
    return json.dumps(result)


@recruiter_agent.tool
async def push_to_main_panel(ctx: RunContext[AgentDeps]) -> str:
    if not ctx.deps.session.search_stack:
        return json.dumps({"error": "No search to push"})
    top = ctx.deps.session.search_stack[-1]
    await ctx.deps.event_queue.put({"type": "PUSH_TO_MAIN",
                                     "data": {"query": top.query,
                                              "filters": top.filters,
                                              "resultIds": [r["id"] for r in top.results_preview]}})
    return json.dumps({"pushed": True, "query": top.query})


@recruiter_agent.tool
async def keyword_search(ctx: RunContext[AgentDeps], query: str) -> str:
    tc_id = str(uuid.uuid4())
    await ctx.deps.event_queue.put({"type": "TOOL_CALL_START",
                                     "data": {"toolCallId": tc_id, "toolCallName": "keyword_search"}})
    results = await do_keyword_search(ctx.deps.pool, query)
    await ctx.deps.event_queue.put({"type": "TOOL_CALL_END",
                                     "data": {"toolCallId": tc_id, "resultSummary": f"{len(results)} matches"}})
    return json.dumps({"results": results[:10], "total": len(results)})


@recruiter_agent.tool
async def load_candidate_pool(
    ctx: RunContext[AgentDeps],
    criteria: dict[str, Any] | None = None,
    limit: int = 100,
) -> str:
    tc_id = str(uuid.uuid4())
    await ctx.deps.event_queue.put({"type": "TOOL_CALL_START",
                                     "data": {"toolCallId": tc_id, "toolCallName": "load_candidate_pool"}})
    result = await do_load_candidate_pool(ctx.deps.pool, ctx.deps.session, criteria or {}, limit)
    await ctx.deps.event_queue.put({"type": "TOOL_CALL_END",
                                     "data": {"toolCallId": tc_id,
                                              "resultSummary": f"pool of {result['count']} loaded"}})
    return json.dumps({"count": result["count"]})


@recruiter_agent.tool
async def filter_from_pool(ctx: RunContext[AgentDeps], criteria: dict[str, Any]) -> str:
    results = do_filter_from_pool(ctx.deps.session, criteria)
    return json.dumps({"results": results[:20], "count": len(results)})


@recruiter_agent.tool
async def aggregate_pool(ctx: RunContext[AgentDeps], dimension: str) -> str:
    counts = do_aggregate_pool(ctx.deps.session, dimension)
    return json.dumps(counts)


@recruiter_agent.tool
async def query_candidates_db(ctx: RunContext[AgentDeps], sql: str) -> str:
    tc_id = str(uuid.uuid4())
    await ctx.deps.event_queue.put({"type": "TOOL_CALL_START",
                                     "data": {"toolCallId": tc_id, "toolCallName": "query_candidates_db"}})
    result = await do_query_candidates_db(ctx.deps.pool, sql)
    summary = f"{result.get('count', 0)} rows" if "rows" in result else result.get("error", "done")
    await ctx.deps.event_queue.put({"type": "TOOL_CALL_END",
                                     "data": {"toolCallId": tc_id, "resultSummary": summary}})
    return json.dumps(result)
```

- [ ] **Step 4: Run tests**

```bash
pytest tests/test_agent_endpoint.py -v -k "import or prompt"
```

Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add pipeline/agent.py tests/test_agent_endpoint.py
git commit -m "feat(agent): PydanticAI agent definition with all tools registered"
```

---

### Task 9: POST /agent/chat streaming endpoint

**Files:**
- Modify: `api/main.py`
- Modify: `tests/test_agent_endpoint.py`

- [ ] **Step 1: Add failing endpoint test**

Append to `tests/test_agent_endpoint.py`:

```python
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch, MagicMock


def test_agent_chat_endpoint_exists():
    from api.main import app
    client = TestClient(app)
    # POST with missing fields should return 422, not 404
    resp = client.post("/agent/chat", json={})
    assert resp.status_code == 422


def test_agent_chat_request_model_validates():
    from api.main import AgentChatRequest
    req = AgentChatRequest(
        recruiter_id="r-1",
        session_id="s-1",
        message="find python devs",
        context={"query": "python", "filters": {}, "result_ids": []},
    )
    assert req.recruiter_id == "r-1"
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_agent_endpoint.py::test_agent_chat_endpoint_exists -v
```

Expected: `AssertionError` (404, endpoint not found).

- [ ] **Step 3: Add AgentChatRequest model and /agent/chat endpoint to api/main.py**

In `api/main.py`, find the imports block at the top and add:

```python
import asyncio
import uuid as _uuid
from pipeline.agent import recruiter_agent, AgentDeps, build_system_prompt
from pipeline.agent_session import get_or_create as get_or_create_session
```

Find the Pydantic model definitions section (near the other `class *Request(BaseModel)` definitions) and add:

```python
class AgentChatContext(BaseModel):
    query: str = ""
    filters: dict = {}
    result_ids: list[str] = []


class AgentChatRequest(BaseModel):
    recruiter_id: str
    session_id: str
    message: str
    context: AgentChatContext = AgentChatContext()
```

Then, before the final `if __name__ == "__main__":` block (or near the other route definitions), add:

```python
@app.post("/agent/chat")
async def agent_chat(req: AgentChatRequest):
    from fastapi.responses import StreamingResponse

    session = get_or_create_session(req.recruiter_id, req.session_id)
    pool = await get_pool()

    # Fetch recruiter hints for system prompt
    hints: list[str] = []
    try:
        row = await pool.fetchrow(
            "SELECT personalization_hints FROM recruiter_preferences WHERE recruiter_id = $1::uuid",
            req.recruiter_id,
        )
        if row and row["personalization_hints"]:
            hints = [h.get("text", "") for h in (row["personalization_hints"] or [])]
    except Exception:
        pass

    system_prompt = build_system_prompt(
        query=req.context.query,
        filters=req.context.filters,
        result_ids=req.context.result_ids,
        hints=hints,
    )

    event_queue: asyncio.Queue = asyncio.Queue()
    deps = AgentDeps(
        pool=pool,
        session=session,
        recruiter_id=req.recruiter_id,
        event_queue=event_queue,
    )

    msg_id = str(_uuid.uuid4())

    async def run_agent():
        try:
            async with recruiter_agent.run_stream(
                req.message,
                deps=deps,
                message_history=session.messages,
                system_prompt=system_prompt,
            ) as result:
                await event_queue.put({"type": "TEXT_MESSAGE_START",
                                       "data": {"messageId": msg_id, "role": "assistant"}})
                async for delta in result.stream_text(delta=True):
                    await event_queue.put({"type": "TEXT_MESSAGE_CONTENT",
                                           "data": {"messageId": msg_id, "delta": delta}})
                await event_queue.put({"type": "TEXT_MESSAGE_END",
                                       "data": {"messageId": msg_id}})
                session.messages = result.all_messages()
        except Exception as exc:
            await event_queue.put({"type": "ERROR", "data": {"message": str(exc)}})
        finally:
            await event_queue.put(None)  # sentinel

    async def stream_events():
        import json
        asyncio.create_task(run_agent())
        while True:
            try:
                event = await asyncio.wait_for(event_queue.get(), timeout=60.0)
            except asyncio.TimeoutError:
                yield "event: PING\ndata: {}\n\n"
                continue
            if event is None:
                break
            yield f"event: {event['type']}\ndata: {json.dumps(event['data'])}\n\n"

    return StreamingResponse(
        stream_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/agent/session/clear", status_code=204)
async def clear_agent_session(recruiter_id: str, session_id: str):
    from pipeline.agent_session import clear as clear_session
    clear_session(recruiter_id, session_id)
```

- [ ] **Step 4: Run endpoint tests**

```bash
pytest tests/test_agent_endpoint.py -v
```

Expected: all pass.

- [ ] **Step 5: Smoke test with curl**

Start the server: `uvicorn api.main:app --reload`

```bash
curl -N -X POST http://localhost:8000/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"recruiter_id":"00000000-0000-0000-0000-000000000001","session_id":"test-1","message":"hello","context":{"query":"","filters":{},"result_ids":[]}}' \
  --no-buffer
```

Expected: streaming SSE events including `TEXT_MESSAGE_START`, `TEXT_MESSAGE_CONTENT`, `TEXT_MESSAGE_END`.

- [ ] **Step 6: Commit**

```bash
git add api/main.py tests/test_agent_endpoint.py
git commit -m "feat(api): POST /agent/chat streaming endpoint with PydanticAI"
```

---

### Task 10: Search history logging and GET /search/history

**Files:**
- Modify: `api/main.py`
- Create: `tests/test_search_history.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_search_history.py`:

```python
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch


def test_search_history_endpoint_exists():
    from api.main import app
    client = TestClient(app)
    resp = client.get("/search/history?recruiter_id=00000000-0000-0000-0000-000000000001")
    # 200 or 500 (DB not running in test) — not 404
    assert resp.status_code != 404


def test_search_history_request_model():
    from api.main import SearchHistoryResponse
    r = SearchHistoryResponse(entries=[])
    assert r.entries == []
```

- [ ] **Step 2: Run to confirm failure**

```bash
pytest tests/test_search_history.py -v
```

Expected: `AssertionError` (404) or `ImportError`.

- [ ] **Step 3: Add history logging to existing /search handler**

In `api/main.py`, find the `/search` handler. Near the end where the `SearchResponse` is built and returned (around the `return SearchResponse(...)` call), find the existing `BackgroundTasks` usage or add it. First ensure `background_tasks: BackgroundTasks` is in the function signature.

Add this helper function near the other private helpers:

```python
async def _log_search_history(
    recruiter_id: str | None,
    query: str,
    filters: dict,
    results: list,
    latency_ms: int,
) -> None:
    """Fire-and-forget: log a user-triggered search to search_history."""
    import json as _json
    if not recruiter_id:
        return
    try:
        pool = await get_pool()
        top10 = [{"id": r.candidate_id, "score": round(r.feature_score, 4)}
                 for r in results[:10]]
        await pool.execute(
            """INSERT INTO search_history (recruiter_id, query, filters_json, results_json, latency_ms)
               VALUES ($1::uuid, $2, $3::jsonb, $4::jsonb, $5)""",
            recruiter_id,
            query or "",
            _json.dumps(filters or {}),
            _json.dumps(top10),
            latency_ms,
        )
    except Exception as exc:
        logger.debug("search_history logging skipped: %s", exc)
```

Then in the `/search` handler, after building the `SearchResponse` object (before `return`), add:

```python
    background_tasks.add_task(
        _log_search_history,
        recruiter_id=req.recruiter_id,
        query=req.query or "",
        filters=req.explicit_filters or {},
        results=search_resp.results,
        latency_ms=int(sum(search_resp.phase_timings.values())),
    )
```

(If `background_tasks` is not already in the signature, add `background_tasks: BackgroundTasks` after `req: SearchRequest`.)

- [ ] **Step 4: Add SearchHistoryResponse model and GET /search/history endpoint**

In `api/main.py`, add near the other response models:

```python
class SearchHistoryEntry(BaseModel):
    id: int
    query: str | None
    filters_json: dict
    results_json: list
    latency_ms: int | None
    timestamp: str


class SearchHistoryResponse(BaseModel):
    entries: list[SearchHistoryEntry]
```

Add the endpoint:

```python
@app.get("/search/history", response_model=SearchHistoryResponse)
async def get_search_history(recruiter_id: str, limit: int = 50):
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT id, query, filters_json, results_json, latency_ms,
                  timestamp::text AS timestamp
           FROM search_history
           WHERE recruiter_id = $1::uuid
           ORDER BY timestamp DESC
           LIMIT $2""",
        recruiter_id, min(limit, 200),
    )
    return SearchHistoryResponse(entries=[dict(r) for r in rows])
```

- [ ] **Step 5: Run tests**

```bash
pytest tests/test_search_history.py -v
```

Expected: 2 passed.

- [ ] **Step 6: Smoke test**

With the server running and after a search from the UI:

```bash
curl "http://localhost:8000/search/history?recruiter_id=00000000-0000-0000-0000-000000000001&limit=5"
```

Expected: JSON with `entries` array.

- [ ] **Step 7: Commit**

```bash
git add api/main.py tests/test_search_history.py
git commit -m "feat(api): search history logging + GET /search/history endpoint"
```

---

## Phase B — Frontend

---

### Task 11: Side panel JS — SSE consumer, chat rendering, candidate cards

**Files:**
- Create: `api/static/agent-panel.js`

- [ ] **Step 1: Create api/static/agent-panel.js with SSE consumer and chat renderer**

Create `api/static/agent-panel.js`:

```javascript
// Agent side panel — SSE consumer, chat renderer, session manager
// Consumed by admin.html via <script src="/static/agent-panel.js">

(function () {
  "use strict";

  // ── State ──────────────────────────────────────────────────────────────────
  const state = {
    sessionId: crypto.randomUUID(),
    recruiterId: null,
    stackDepth: 0,
    activeMessageId: null,
    currentAbortController: null,
  };

  // ── DOM refs (populated by init()) ────────────────────────────────────────
  let panelEl, chatEl, inputEl, sendBtn, backBtn, newSessionBtn, clearBtn;

  // ── Init ──────────────────────────────────────────────────────────────────
  function init(recruiterId) {
    state.recruiterId = recruiterId;
    panelEl     = document.getElementById("agentPanel");
    chatEl      = document.getElementById("agentChat");
    inputEl     = document.getElementById("agentInput");
    sendBtn     = document.getElementById("agentSendBtn");
    backBtn     = document.getElementById("agentBackBtn");
    newSessionBtn = document.getElementById("agentNewSessionBtn");

    sendBtn.addEventListener("click", sendMessage);
    inputEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
    });
    backBtn.addEventListener("click", handleBack);
    newSessionBtn.addEventListener("click", newSession);
    updateBackBtn();
  }

  // ── Send message ──────────────────────────────────────────────────────────
  async function sendMessage() {
    const text = inputEl.value.trim();
    if (!text || !state.recruiterId) return;

    inputEl.value = "";
    appendUserMessage(text);
    sendBtn.disabled = true;

    const context = getSearchContext();

    if (state.currentAbortController) state.currentAbortController.abort();
    state.currentAbortController = new AbortController();

    const assistantEl = appendAssistantMessage();

    try {
      const resp = await fetch("/agent/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          recruiter_id: state.recruiterId,
          session_id: state.sessionId,
          message: text,
          context,
        }),
        signal: state.currentAbortController.signal,
      });

      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const events = buf.split("\n\n");
        buf = events.pop(); // incomplete last chunk
        for (const raw of events) {
          if (raw.trim()) handleSSEEvent(raw, assistantEl);
        }
      }
    } catch (err) {
      if (err.name !== "AbortError") {
        appendErrorMessage(err.message);
      }
    } finally {
      sendBtn.disabled = false;
    }
  }

  // ── SSE event handler ─────────────────────────────────────────────────────
  function handleSSEEvent(raw, assistantEl) {
    const lines = raw.split("\n");
    let eventType = "", dataStr = "";
    for (const line of lines) {
      if (line.startsWith("event: ")) eventType = line.slice(7);
      if (line.startsWith("data: ")) dataStr = line.slice(6);
    }
    if (!eventType || !dataStr) return;

    let data;
    try { data = JSON.parse(dataStr); } catch { return; }

    switch (eventType) {
      case "TEXT_MESSAGE_START":
        state.activeMessageId = data.messageId;
        break;

      case "TEXT_MESSAGE_CONTENT":
        appendTextDelta(assistantEl, data.delta || "");
        break;

      case "TEXT_MESSAGE_END":
        state.activeMessageId = null;
        break;

      case "TOOL_CALL_START":
        appendToolCall(assistantEl, data.toolCallName, "running");
        break;

      case "TOOL_CALL_END":
        finalizeToolCall(assistantEl, data.toolCallId, data.resultSummary || "done");
        break;

      case "SEARCH_RESULTS":
        appendSearchResults(assistantEl, data.results || [], data.iterationId);
        break;

      case "PUSH_TO_MAIN":
        applyToMainPanel(data);
        break;

      case "STACK_UPDATED":
        state.stackDepth = data.depth || 0;
        updateBackBtn();
        break;

      case "ERROR":
        appendErrorMessage(data.message || "Unknown error");
        break;
    }
    chatEl.scrollTop = chatEl.scrollHeight;
  }

  // ── Chat renderers ────────────────────────────────────────────────────────
  function appendUserMessage(text) {
    const el = document.createElement("div");
    el.className = "agent-msg agent-msg--user";
    el.textContent = text;
    chatEl.appendChild(el);
    chatEl.scrollTop = chatEl.scrollHeight;
  }

  function appendAssistantMessage() {
    const el = document.createElement("div");
    el.className = "agent-msg agent-msg--assistant";
    chatEl.appendChild(el);
    return el;
  }

  function appendTextDelta(containerEl, delta) {
    let textNode = containerEl.querySelector(".agent-text");
    if (!textNode) {
      textNode = document.createElement("span");
      textNode.className = "agent-text";
      containerEl.appendChild(textNode);
    }
    textNode.textContent += delta;
  }

  function appendToolCall(containerEl, toolName, status) {
    const el = document.createElement("div");
    el.className = "agent-tool-call";
    el.dataset.status = status;
    el.innerHTML = `<span class="agent-tool-icon">⚙</span>
                    <span class="agent-tool-name">${escHtml(toolName)}</span>
                    <span class="agent-tool-status">${escHtml(status)}</span>`;
    containerEl.appendChild(el);
    return el;
  }

  function finalizeToolCall(containerEl, toolCallId, summary) {
    // Update the last in-progress tool call in this container
    const calls = containerEl.querySelectorAll(".agent-tool-call[data-status='running']");
    const last = calls[calls.length - 1];
    if (last) {
      last.dataset.status = "done";
      last.querySelector(".agent-tool-status").textContent = summary;
    }
  }

  function appendSearchResults(containerEl, results, iterationId) {
    if (!results.length) return;
    const wrapper = document.createElement("div");
    wrapper.className = "agent-results";
    wrapper.innerHTML = `<div class="agent-results-label">Top results</div>`;

    results.slice(0, 3).forEach((r) => {
      const card = document.createElement("div");
      card.className = "agent-candidate-card";
      card.innerHTML = `
        <div class="agent-card-name">${escHtml(r.name || r.full_name || r.id)}</div>
        <div class="agent-card-meta">${escHtml(r.city || "")} · ${r.years_exp ?? "?"}y exp</div>
        <div class="agent-card-skills">${(r.skills || []).slice(0, 3).map(s => `<span class="chip">${escHtml(s)}</span>`).join("")}</div>
        <div class="agent-card-score">Score: ${r.score ?? ""}</div>
        <button class="agent-expand-btn" data-candidate-id="${escHtml(r.id)}">▸ Expand</button>
      `;
      card.querySelector(".agent-expand-btn").addEventListener("click", () => expandCandidate(r.id, card));
      wrapper.appendChild(card);
    });

    const viewBtn = document.createElement("button");
    viewBtn.className = "agent-view-main-btn";
    viewBtn.textContent = "View in main panel ↗";
    viewBtn.addEventListener("click", () => {
      fetch("/agent/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          recruiter_id: state.recruiterId,
          session_id: state.sessionId,
          message: "push_to_main",
          context: getSearchContext(),
        }),
      });
    });
    wrapper.appendChild(viewBtn);

    containerEl.appendChild(wrapper);
  }

  async function expandCandidate(candidateId, cardEl) {
    const existing = cardEl.querySelector(".agent-card-detail");
    if (existing) { existing.remove(); return; }

    const detail = document.createElement("div");
    detail.className = "agent-card-detail";
    detail.textContent = "Loading…";
    cardEl.appendChild(detail);

    try {
      const resp = await fetch(`/candidates/${candidateId}`);
      const data = await resp.json();
      detail.innerHTML = `
        <p><strong>Skills:</strong> ${(data.skills || []).join(", ") || "—"}</p>
        <p><strong>Salary:</strong> ${data.salary_min ?? "?"} – ${data.salary_max ?? "?"}</p>
        <p><strong>Summary:</strong> ${escHtml((data.best_chunk || "").slice(0, 300))}</p>
      `;
    } catch (e) {
      detail.textContent = "Failed to load";
    }
  }

  function applyToMainPanel(data) {
    // Dispatch a custom event that admin.html can listen to
    document.dispatchEvent(new CustomEvent("agent:push-to-main", { detail: data }));
    appendSystemMessage("↗ Results pushed to main panel");
  }

  function appendErrorMessage(msg) {
    const el = document.createElement("div");
    el.className = "agent-msg agent-msg--error";
    el.textContent = `Error: ${msg}`;
    chatEl.appendChild(el);
  }

  function appendSystemMessage(msg) {
    const el = document.createElement("div");
    el.className = "agent-msg agent-msg--system";
    el.textContent = msg;
    chatEl.appendChild(el);
  }

  // ── Session management ────────────────────────────────────────────────────
  function handleBack() {
    if (state.stackDepth <= 1) return;
    // Ask the server to pop the stack (or handle client-side)
    fetch("/agent/session/clear", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    }).catch(() => {});
    document.dispatchEvent(new CustomEvent("agent:back"));
    state.stackDepth = Math.max(0, state.stackDepth - 1);
    updateBackBtn();
    appendSystemMessage("← Went back to previous search");
  }

  function newSession() {
    if (state.currentAbortController) state.currentAbortController.abort();
    fetch(`/agent/session/clear?recruiter_id=${encodeURIComponent(state.recruiterId)}&session_id=${encodeURIComponent(state.sessionId)}`, {
      method: "POST",
    }).catch(() => {});
    state.sessionId = crypto.randomUUID();
    state.stackDepth = 0;
    chatEl.innerHTML = "";
    updateBackBtn();
    appendSystemMessage("New session started");
  }

  function updateBackBtn() {
    if (backBtn) backBtn.disabled = state.stackDepth <= 1;
  }

  // ── Helpers ───────────────────────────────────────────────────────────────
  function getSearchContext() {
    // Read current main panel state — admin.html must expose these
    return {
      query: (document.getElementById("searchQuery") || {}).value || "",
      filters: window._getActiveFilters ? window._getActiveFilters() : {},
      result_ids: window._getCurrentResultIds ? window._getCurrentResultIds() : [],
    };
  }

  function escHtml(str) {
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ── Public API ────────────────────────────────────────────────────────────
  window.AgentPanel = { init };
})();
```

- [ ] **Step 2: Verify file was created**

```bash
ls api/static/agent-panel.js
```

Expected: file exists.

- [ ] **Step 3: Commit**

```bash
git add api/static/agent-panel.js
git commit -m "feat(ui): agent side panel JS — SSE consumer, chat renderer, candidate cards"
```

---

### Task 12: Admin HTML integration

**Files:**
- Modify: `api/static/admin.html`

- [ ] **Step 1: Add side-panel CSS**

Open `api/static/admin.html`. Find the closing `</style>` tag in the `<head>` section and insert before it:

```css
    /* ── Agent side panel ──────────────────────────────────────────────── */
    .agent-panel {
      width: 380px;
      min-width: 280px;
      max-width: 480px;
      border-left: 1px solid var(--border, #e0e0e0);
      display: flex;
      flex-direction: column;
      height: 100%;
      background: var(--surface, #fff);
    }
    .agent-panel-header {
      padding: 10px 12px;
      border-bottom: 1px solid var(--border, #e0e0e0);
      display: flex;
      align-items: center;
      gap: 8px;
      font-weight: 600;
      font-size: 13px;
    }
    .agent-panel-header button { font-size: 11px; }
    #agentChat {
      flex: 1;
      overflow-y: auto;
      padding: 10px;
      display: flex;
      flex-direction: column;
      gap: 8px;
      font-size: 13px;
    }
    .agent-msg { padding: 8px 10px; border-radius: 8px; max-width: 95%; }
    .agent-msg--user { background: var(--accent-light, #e8f0fe); align-self: flex-end; }
    .agent-msg--assistant { background: var(--surface-2, #f5f5f5); align-self: flex-start; }
    .agent-msg--error { background: #ffeaea; color: #c00; align-self: flex-start; }
    .agent-msg--system { color: #888; font-size: 11px; align-self: center; font-style: italic; }
    .agent-tool-call {
      font-size: 11px; color: #555;
      background: #f0f4ff; border-radius: 4px;
      padding: 3px 7px; margin: 2px 0;
      display: flex; gap: 6px; align-items: center;
    }
    .agent-tool-call[data-status="done"] { color: #2a7; }
    .agent-results { border: 1px solid var(--border, #e0e0e0); border-radius: 8px; padding: 8px; }
    .agent-results-label { font-size: 11px; font-weight: 600; color: #666; margin-bottom: 6px; }
    .agent-candidate-card {
      border: 1px solid #e0e0e0; border-radius: 6px;
      padding: 7px 9px; margin-bottom: 6px; font-size: 12px;
    }
    .agent-card-name { font-weight: 600; margin-bottom: 2px; }
    .agent-card-meta { color: #666; font-size: 11px; }
    .agent-card-skills { margin: 3px 0; display: flex; flex-wrap: wrap; gap: 3px; }
    .agent-card-detail { margin-top: 6px; font-size: 11px; color: #444; border-top: 1px solid #eee; padding-top: 5px; }
    .agent-expand-btn { font-size: 11px; background: none; border: none; color: #4a6cf7; cursor: pointer; padding: 2px 0; }
    .agent-view-main-btn { font-size: 11px; margin-top: 4px; }
    .agent-panel-input-row {
      padding: 8px;
      border-top: 1px solid var(--border, #e0e0e0);
      display: flex; gap: 6px;
    }
    #agentInput {
      flex: 1; font-size: 13px;
      padding: 6px 8px; border-radius: 6px;
      border: 1px solid var(--border, #e0e0e0);
      resize: none; min-height: 34px; max-height: 100px;
    }
```

- [ ] **Step 2: Add side-panel DOM**

In `api/static/admin.html`, find the `<main class="workspace">` element. It currently wraps the main search panel. Wrap both the existing panel and the new agent panel in a flex row:

Find:
```html
    <main class="workspace">
```

Replace with:
```html
    <main class="workspace" style="display:flex;flex-direction:row;height:calc(100vh - 56px);overflow:hidden;">
```

Then find the closing `</main>` tag and, just before it, add the agent panel DOM:

```html
      <!-- Agent side panel -->
      <aside class="agent-panel" id="agentPanel">
        <div class="agent-panel-header">
          <span>Agent</span>
          <button id="agentBackBtn" disabled title="Go back to previous search">← Back</button>
          <button id="agentNewSessionBtn" title="Start a new session">New session</button>
        </div>
        <div id="agentChat"></div>
        <div class="agent-panel-input-row">
          <textarea id="agentInput" placeholder="Ask the agent…" rows="1"></textarea>
          <button id="agentSendBtn" class="primary">Send</button>
        </div>
      </aside>
```

- [ ] **Step 3: Import agent-panel.js and initialise**

Find the closing `</body>` tag in `admin.html`. Just before it, add:

```html
    <script src="/static/agent-panel.js"></script>
    <script>
      // Initialise agent panel once recruiter ID is known
      (function () {
        function tryInit() {
          const rid = document.getElementById("recruiterId");
          if (rid && rid.value) {
            AgentPanel.init(rid.value);
          } else if (rid) {
            rid.addEventListener("change", () => AgentPanel.init(rid.value), { once: true });
          }
        }
        if (document.readyState === "loading") {
          document.addEventListener("DOMContentLoaded", tryInit);
        } else {
          tryInit();
        }

        // Expose helper for agent context (called by agent-panel.js)
        window._getCurrentResultIds = function () {
          // Read candidate IDs from current search results in the DOM
          return Array.from(document.querySelectorAll("[data-candidate-id]"))
                      .map(el => el.dataset.candidateId)
                      .filter(Boolean)
                      .slice(0, 10);
        };

        window._getActiveFilters = function () {
          // Return current explicit_filters from the search form
          // Adapt to however admin.html builds its filter payload
          return window._lastSearchFilters || {};
        };

        // When agent pushes search state to main panel, apply it
        document.addEventListener("agent:push-to-main", (e) => {
          const { query, filters, resultIds } = e.detail;
          const queryEl = document.getElementById("searchQuery");
          if (queryEl && query) queryEl.value = query;
          // Trigger a search with the agent's params
          if (window._triggerSearch) window._triggerSearch(query, filters);
        });
      })();
    </script>
```

- [ ] **Step 4: Expose _lastSearchFilters and _triggerSearch from existing search code**

Find the existing search function in `admin.html` (where it calls `POST /search`). Add two lines:
1. Before the fetch call: `window._lastSearchFilters = explicit_filters;` (where `explicit_filters` is the filter object being sent)
2. Wrap the search trigger in a named function if not already: `window._triggerSearch = function(query, filters) { ... }`

The exact location depends on the existing code structure — search for the `fetch("/search"` call and add the assignment immediately before it.

- [ ] **Step 5: Open in browser and verify**

Start the server: `uvicorn api.main:app --reload`

Open `http://localhost:8000/ui`. Verify:
- [ ] Side panel appears to the right of the main search panel
- [ ] Typing a message in the agent input and pressing Enter sends a request
- [ ] Agent response streams in with text appearing incrementally
- [ ] Tool call events show as grey chips that turn green when done
- [ ] "← Back" button is disabled initially, enabled after second search
- [ ] "New session" clears the chat

- [ ] **Step 6: Commit**

```bash
git add api/static/admin.html api/static/agent-panel.js
git commit -m "feat(ui): integrate agent side panel into admin.html"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|---|---|
| PydanticAI agent + AG-UI SSE | Tasks 4, 8, 9 |
| Provider-agnostic (model string) | Task 8 (model param in agent.py) |
| Tier 1 pipeline tools | Task 5 |
| Tier 2 DB/pool tools | Task 6 |
| Tier 3 ad-hoc SQL with guardrails | Task 7 |
| Session store + backtrack stack | Task 3 |
| Search history table | Task 2 |
| Search history logging on /search | Task 10 |
| GET /search/history | Task 10 |
| POST /agent/chat streaming | Task 9 |
| POST /agent/session/clear | Task 9 |
| System prompt with context injection | Task 8 |
| AG-UI events: TEXT, TOOL, SEARCH_RESULTS, PUSH_TO_MAIN, STACK_UPDATED | Tasks 4, 8 |
| Side panel JS: SSE consumer | Task 11 |
| Side panel JS: chat + tool call rendering | Task 11 |
| Side panel JS: candidate card expand | Task 11 |
| Side panel JS: push-to-main | Task 11 |
| Side panel JS: back button | Task 11 |
| Side panel JS: new session | Task 11 |
| Admin HTML: side panel DOM | Task 12 |
| Admin HTML: search context wiring | Task 12 |
| LangGraph Layer 2 stub | Not implemented — explicitly out of scope for v1 |

**No gaps found.** LangGraph workflows are explicitly out of scope per the spec.
