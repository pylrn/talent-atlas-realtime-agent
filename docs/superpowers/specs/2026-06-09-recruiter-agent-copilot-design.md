# Recruiter Agent Copilot — Design

## Problem

The current search pipeline is deterministic but opaque. When results are poor, the recruiter has no way to understand why, refine intent interactively, or build up persistent preferences. Hardcoded skill boosting and soft-filter weights can actively hurt results without context about what the recruiter actually wanted.

## Goal

An agentic side-panel chat that helps recruiters refine search intent in real time — running searches, diagnosing poor results, modifying constraints, and saving preferences — with full visibility into what the agent is doing.

## Non-goals

- Passive/implicit personalization (auto-learning from selections without asking) — stays off by default
- LangGraph workflows — scaffolded for later, not in scope for v1
- React frontend migration — vanilla JS admin.html unchanged
- Auth/multi-tenant access control beyond existing recruiter_id pattern

## Architecture

```
admin.html (vanilla JS, existing)
  │
  ├── POST /search              → existing FastAPI + pipeline (unchanged)
  │      └── logs to search_history table (new, source='user')
  │
  ├── POST /agent/chat          → new FastAPI endpoint
  │      └── PydanticAI agent  → streams AG-UI SSE events back
  │             └── tools call back into existing pipeline functions directly
  │
  └── POST /agent/workflows/:id → future LangGraph entry point (not in scope)
```

### Why PydanticAI

- Native AG-UI protocol and Vercel AI Data Stream Protocol support via UI adapters
- Provider-agnostic: Anthropic, OpenAI, Groq, Gemini, OpenRouter via model config
- First-class FastAPI / Starlette streaming integration
- Tools are typed Python functions — call existing pipeline code directly, no HTTP round-trip
- Clean agent loop with streaming tool use out of the box

### Why not Node.js / Vercel AI SDK

The ops cost of a second server is unjustified. PydanticAI's native AG-UI support gives the same streaming event protocol (TEXT_MESSAGE_*, TOOL_CALL_*, STATE_SNAPSHOT) without leaving Python. All existing pipeline functions, DB connections, and Langfuse observability are directly available.

### Two-layer agent architecture

**Layer 1 — PydanticAI (interactive, v1 scope)**
- Real-time recruiter chat assistant
- Search refinement loop
- Intent diagnosis
- Preference capture

**Layer 2 — LangGraph (predefined workflows, future)**
- Weekly candidate digests
- Bulk role-matching
- Multi-step shortlist generation with human approval gates
- Triggered from chat ("run shortlist workflow") but executes as a separate graph

## Agent Tools

All tools are Python functions called directly within the PydanticAI agent — no internal HTTP calls.

### Tier 1 — Pipeline tools

| Tool | Signature | What it does |
|---|---|---|
| `run_search` | `(query, filters, weights)` | Calls `pipeline.search(...)`, pushes iteration onto session stack |
| `modify_and_search` | `(changes)` | Diffs current stack top, applies changes, reruns, pushes new entry |
| `explain_poor_results` | `(result_set)` | Uses `pipeline.ai_insights` / ranking explanation to diagnose |
| `compare_iterations` | `(iter_a, iter_b)` | Delta between two stack entries — what changed and why |
| `get_candidate_detail` | `(candidate_id)` | Fetches full candidate profile for in-chat expansion |
| `save_hint` | `(text)` | Writes to `recruiter_preferences.personalization_hints` |
| `push_to_main_panel` | `(search_state)` | Fires `PUSH_TO_MAIN` SSE event — updates main search panel |

### Tier 2 — Lightweight DB/pool tools

| Tool | Signature | What it does |
|---|---|---|
| `keyword_search_candidates` | `(query: str)` | BM25/FTS directly against candidates table — bypasses the full pipeline |
| `load_candidate_pool` | `(criteria: dict, limit: int)` | Loads up to N candidates matching criteria into session pool for fast iteration |
| `filter_from_pool` | `(criteria: dict)` | Filters the current session pool in-memory without re-hitting the pipeline |
| `aggregate_pool` | `(dimension: str)` | Count/group stats on current pool — "how many are in Bangalore?" |

`load_candidate_pool` is useful for backtrack-heavy sessions: load 100 candidates once, let the agent slice and dice without burning full pipeline latency each iteration. The pool lives in session state.

### Tier 3 — Ad-hoc SQL tool

| Tool | Signature | What it does |
|---|---|---|
| `query_candidates_db` | `(sql: str)` | Agent writes raw SELECT — executed against a read-only DB role |

Instead of pre-defining a tool for every possible query pattern, the agent writes the SQL itself. Guardrails make it safe:

- **Read-only Postgres role** — agent cannot run UPDATE/DELETE/DROP even if it tries
- **Auto-inject `LIMIT 50`** — no accidental full-table scans
- **5-second query timeout** — stops runaway queries
- **DML keyword block** — belt-and-suspenders: reject queries containing `UPDATE`, `DELETE`, `INSERT`, `DROP`, `TRUNCATE`, `ALTER`
- **Table allowlist** — only `candidates`, `candidate_skills`, `resume_chunks` accessible

Allowed tables and their key columns are injected into the agent's system prompt as a mini-schema, so the agent knows what it can query without guessing.

The agent's system prompt is injected at call time with: current query, current filters, current result set (top 10 IDs + scores), recruiter's saved hints from `recruiter_preferences`, and the DB mini-schema for Tier 3 queries. The agent has full context before the recruiter types a word.

## Session State & Backtracking

In-memory, per `recruiter_id + session_uuid`. No persistence — session dies on tab close or "New Search."

```python
session = {
    "messages": [...],            # full chat history passed to LLM each turn
    "search_stack": [             # UI backtrack stack
        {                         # oldest iteration
            "query": "...",
            "filters": {...},
            "results_preview": [...],  # top 3 for chat display
            "agent_reasoning": "..."
        },
        { ... }                   # current (top of stack)
    ],
    "role_context": {             # built up through conversation
        "soft_preferences": [...],
        "negative_preferences": [...],
        "example_good_ids": [...]
    }
}
```

### Backtracking

UI-level only — like Android back button. No LangGraph checkpoints.

- Back button calls `stack.pop()`, restores previous entry to main panel
- Button is disabled when stack depth ≤ 1
- Stack state mirrored client-side in JS so the button can reflect depth without an extra API call
- Each `run_search` / `modify_and_search` tool call pushes a new entry

Sessions are keyed `recruiter_id:session_uuid`. `session_uuid` generated when the side panel opens or when the recruiter clicks "New Session" (clears stack + role_context + messages). Sessions are also lost on server restart — this is acceptable; the recruiter simply starts a new session.

## Search History Logging

Permanent log of **user-triggered** main panel searches only. Agent intermediate searches are not logged here — they live only in the session backtrack stack.

```sql
CREATE TABLE search_history (
    id           BIGSERIAL PRIMARY KEY,
    recruiter_id TEXT NOT NULL,
    query        TEXT,
    filters_json JSONB,
    results_json JSONB,        -- top 10 candidate IDs + scores
    latency_ms   INT,
    timestamp    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX search_history_recruiter_ts ON search_history (recruiter_id, timestamp DESC);
```

New endpoint: `GET /search/history?recruiter_id=X&limit=50` returns the log for the history panel.

Logging happens in the existing `/search` handler via a `BackgroundTask` — same pattern as outcome logging already in the codebase. Zero latency impact on search.

## Streaming Protocol (AG-UI over SSE)

The `/agent/chat` endpoint streams AG-UI events as SSE. Vanilla JS consumes via `fetch` + `ReadableStream`.

Events emitted:

```
TEXT_MESSAGE_START      agent starts a reasoning chunk
TEXT_MESSAGE_CONTENT    streamed text delta
TEXT_MESSAGE_END        reasoning chunk done

TOOL_CALL_START         agent decided to call a tool (name visible in UI)
TOOL_CALL_ARGS          streaming tool arguments
TOOL_CALL_END           tool done, result attached

SEARCH_RESULTS          custom — top 3 candidate cards for inline display
PUSH_TO_MAIN            custom — instructs JS to update main search panel
STACK_UPDATED           custom — current stack depth (enables/disables back button)
STATE_SNAPSHOT          role_context sync to client
```

## UI Layout

Side panel added to existing `admin.html` — does not modify the main search panel layout.

```
┌─────────────────────────┬─────────────────────────┐
│  Main search panel      │  Agent side panel       │
│  (existing, unchanged)  │                         │
│                         │  [chat messages]        │
│  Query: [___________]   │  ┌──────────────────┐  │
│  Filters: [_________]   │  │ 🔍 Searching with │  │  ← TOOL_CALL_START
│                         │  │  Python + 8yrs...│  │
│  Results:               │  └──────────────────┘  │
│  1. Candidate A         │                         │
│  2. Candidate B         │  Results (top 3):       │
│  3. Candidate C         │  ┌──────────────────┐  │  ← SEARCH_RESULTS
│  ...                    │  │ Priya S.      ▸  │  │
│                         │  │ Rahul M.      ▸  │  │  ← expand in chat
│                         │  │ Amit K.       ▸  │  │
│                         │  └──────────────────┘  │
│                         │  [View in main ↗]       │  ← PUSH_TO_MAIN
│                         │  [← Back]  [✓ Save]     │  ← stack pop / save_hint
│                         │                         │
│                         │  [Type a message ____]  │
└─────────────────────────┴─────────────────────────┘
```

Candidate cards in chat: name, current role, top 3 matching skills, score. "▸ Expand" fetches full profile via `get_candidate_detail` and renders inline.

## Migration

New table: `search_history` (migration `010_search_history.sql`).

No changes to `recruiter_preferences` schema — `personalization_hints` column already exists from migration `006_personalization_hints.sql`. The `save_hint` tool writes directly to that column.

## API request shape

```
POST /agent/chat
Content-Type: application/json
{
  "recruiter_id": "string",
  "session_id":   "string",       // UUID, generated client-side on panel open
  "message":      "string",       // recruiter's chat message
  "context": {                    // current main panel state at time of send
    "query":   "string | null",
    "filters": { ... } | null,
    "result_ids": ["id1", ...]    // top 10 IDs currently shown in main panel
  }
}

Response: text/event-stream (AG-UI SSE)
```

## New files

```
pipeline/agent.py              PydanticAI agent definition + tools
pipeline/agent_session.py      In-memory session store (search stack, role context)
pipeline/agent_stream.py       AG-UI SSE event emitter helpers
db/migrations/010_search_history.sql   -- number to be confirmed against latest migration
api/static/agent-panel.js      Side panel JS (loaded by admin.html)
```

## Modified files

```
api/main.py                    Add POST /agent/chat, GET /search/history
api/static/admin.html          Import agent-panel.js, add side-panel DOM
```

## Out of scope (explicit)

- LangGraph workflows (Layer 2) — scaffolded as a stub, implemented separately
- Passive personalization / auto-learning from selections
- React migration
- Agent memory across sessions (role_context resets on new session)
- Multi-agent delegation / subagents
- Auth beyond existing recruiter_id
