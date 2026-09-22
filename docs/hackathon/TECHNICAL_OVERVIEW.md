# Talent Atlas Technical Overview

## What the prototype demonstrates

Talent Atlas applies Samsung Theme 05 to recruiting search. A recruiter can
speak naturally, interrupt the assistant, correct one constraint while keeping
the rest, inspect the tools that ran, and verify the evidence behind each
candidate. The language model orchestrates typed functions; it does not replace
the deterministic search engine or directly control the database.

## End-to-end call path

```text
Browser microphone, text, PNG/JPEG or WAV
        |
        v
WebSocket /talent/realtime/ws
        |
        +--> Gemini Live transport: streaming transcript + audio response
        |         |
        |         v
        |    typed model tool call with call_id
        |
        v
RealtimeToolDispatcher
  validate schema -> classify effect -> deduplicate -> apply scope/idempotency
        |
        v
RealtimeAgentSession + SearchPlanRevision
  patch session slots -> diff fingerprints -> preserve/cancel/start branches
        |
        +--> SQL eligibility/count branch
        +--> pgvector semantic branch
        +--> PostgreSQL full-text/BM25-style keyword branch
        +--> exact canonical-skill branch
        |
        v
RRF fusion -> bounded cross-encoder reranking -> diversity -> explanations
        |
        v
authoritative StateSnapshot + tool events + evidence
        |
        v
Talent UI result cards and collapsible Activity evidence graph
```

### 1. Input and transport

The browser opens `WS /talent/realtime/ws`. It can send:

- raw 16 kHz microphone audio as WebSocket binary frames;
- `text.input` JSON messages;
- `media.input` JSON messages containing a base64 PNG, JPEG or WAV up to 5 MB;
- `session.start`, `session.stop`, `session.reset` and `trace.snapshot` controls.

Gemini Live receives the realtime system prompt and the tool declarations. Its
24 kHz audio frames stream back over the same socket. Input/output transcripts,
tool lifecycle events, cancellations and snapshots share one ordered session
timeline.

### 2. Fast path

Partial transcripts enter `RealtimeAgentSession.observe_transcript`. A stable
prefix can launch speculative retrieval before end-of-turn. The fast path emits
an acknowledgement derived from the instruction change, not from unseen search
results. A filler audit rejects premature claims such as “I found three people”
until retrieval has actually returned evidence.

### 3. Slow path and tool calls

Gemini Live chooses a declared tool and supplies structured JSON arguments.
`RealtimeToolDispatcher` validates them with strict Pydantic models. Every call
has a `call_id`. Retries are visible; state-changing calls additionally use an
idempotency key and an effect scope, ensuring that a duplicate request cannot
apply twice and a corrected write can supersede an older one.

The model learns the tool surface from four inputs supplied on every Live
session:

1. a registry-controlled tool name;
2. a concise description of when that tool should be used;
3. a JSON Schema generated from its Pydantic argument model;
4. `BLOCKING` or `NON_BLOCKING` behavior plus the realtime system policy.

It does not receive Python functions or database handles. A model response may
only propose a registered name and JSON arguments. The dispatcher resolves the
name, rejects unknown tools, validates arguments with `extra="forbid"`, checks
for a trusted callback and only then invokes application code. Tool return JSON
is sent both to Gemini for grounded synthesis and to the UI as observable tool
and state events.

```json
{
  "name": "interrupt_search",
  "arguments": {
    "clear_location": true,
    "intent": "refine"
  },
  "call_id": "provider-issued-correlation-id"
}
```

That example does not create a fresh independent search. It produces a child
`SearchPlanRevision`, explicitly clears the location slots and retains the
unmentioned skills, experience and role intent. Branch fingerprints then decide
what can be preserved, reused, cancelled or restarted.

The default model-facing tools are:

| Tool | Purpose | Behaviour |
|---|---|---|
| `search_candidates` | Start a grounded hybrid search | Read-only, non-blocking |
| `interrupt_search` | Patch or replace the active plan and cancel stale branches | Read-only, non-blocking |
| `inspect_candidate` | Load bounded approved evidence for one candidate | Read-only, blocking |
| `compare_candidates` | Compare a bounded candidate set using retrieved evidence | Read-only, blocking |
| `format_current_answer` | Reformat existing evidence without retrieval | Read-only, blocking |
| `cancel_current_action` | Cancel cancellable read-only work | Read-only, blocking |
| `list_skills` | Resolve wording to canonical database skills | Read-only, blocking |
| `request_clarification` | Ask for genuinely missing criteria | Read-only, blocking |
| `use_role_image` | Convert grounded image text into a search plan | Read-only, non-blocking |
| `add_to_shortlist` | Persist a shortlist selection | State-modifying, idempotent |

Scenario manifests can register previously unseen tools at runtime. Their JSON
Schema is compiled into a strict Pydantic model, and each tool is classified as
read-only or state-modifying before it can execute. External callbacks, rather
than manifest text, provide the executable implementation.

The separate typed text copilot uses PydanticAI decorators to generate schemas
for a wider workflow catalogue. Those functions remain thin wrappers: each
delegates to a deterministic `do_*` helper and returns JSON. The text copilot
can choose a named search mode (`no-llm`, `fast`, `quality` or
`agent-quality`), while Gemini Live receives the smaller realtime search schema
and the application translates it into the same canonical search engine. This
keeps provider-specific conversation logic outside retrieval and ranking.

### 4. Revision and interruption logic

The current intent and every constraint live in a session-scoped
`SearchPlanRevision`. Each revision contains:

- a filter fingerprint for shared SQL eligibility;
- branch fingerprints for vector, keyword, skill and count/SQL work;
- a full plan fingerprint for cache and replay identity;
- changed fields and explicitly unset slots.

A spoken correction such as “remove Bangalore but keep the skills” patches only
the location slot. The revision diff decides which running branches are stale.
A query rewrite normally invalidates vector and keyword work; a hard eligibility
change invalidates all branches. A bare barge-in stops speech but does not cancel
valid retrieval merely because the user made a sound.

### 5. Hybrid retrieval and ranking

The canonical search engine owns ranking:

1. Pydantic validates the request and normalizes aliases.
2. SQL applies structured eligibility such as country, city, experience and
   exact required skills.
3. `all-MiniLM-L6-v2` embeds the semantic query into 384 dimensions.
4. pgvector HNSW returns semantically similar resume chunks.
5. PostgreSQL text search protects exact lexical terms.
6. Canonical skill retrieval protects exact technical requirements.
7. Reciprocal Rank Fusion combines independent ranked lists without pretending
   their raw scores share a scale.
8. `cross-encoder/ms-marco-MiniLM-L-6-v2` optionally reviews a bounded pool.
9. Feature ranking and diversity produce the final candidate order.
10. Explanations use the same score that sorted the results and include source
    chunks, matched requirements and retrieval paths.

### 6. State snapshots and evidence UI

The session publishes structured snapshots containing intent, slots, revision,
changed fields, branch state, status and whether the state is authoritative.
Speculative results are explicitly non-authoritative and cannot close a turn.
The Activity drawer renders the fork/join workflow and allows each node to show
its inputs, results, evidence and raw event without crowding the conversation.

## HTTP API surface

| Endpoint | Role |
|---|---|
| `GET /talent` | Recruiter-facing demo UI |
| `WS /talent/realtime/ws` | Full-duplex voice, text, media, tools and events |
| `POST /search` | Direct configurable hybrid search |
| `POST /search/similar` | Candidate-similarity search |
| `POST /agent/chat` | Typed recruiting copilot with streamed/tool-backed logic |
| `POST /agent/push-results` | Convert tool results to the common search response contract |
| `GET /talent/api/candidates/{id}` | Candidate details for result cards |
| `POST /talent/api/candidates/batch` | Resolve a bounded result set |
| `POST /ingest` | Chunk, embed and store a candidate document |
| `GET /health` | Database, corpus and embedding configuration health |

The API's OpenAPI schema is available at `/docs` while the server is running.

## Technology and SDK inventory

| Layer | Technology |
|---|---|
| Runtime | Python 3.10–3.12; verified with Python 3.11.12 |
| API/transport | FastAPI, Uvicorn, WebSocket, SSE-compatible agent events |
| Realtime model SDK | `google-genai` / Gemini Live |
| Typed agent/tool schemas | Pydantic v2 and PydanticAI |
| Optional model SDKs | OpenAI-compatible client and Groq support |
| Database | PostgreSQL 17, asyncpg, pgvector, optional PgBouncer |
| Retrieval | sentence-transformers, PostgreSQL FTS, exact skill indexes |
| Reranking | cross-encoder MiniLM |
| Cache | In-memory or Redis 7 |
| Observability | Langfuse and OpenTelemetry instrumentation |
| Packaging | Docker and Docker Compose |
| Verification | pytest, pytest-asyncio, Ruff and deterministic virtual-clock harness |

There is no Android APK and no externally distributed SDK. The executable
deliverable is the FastAPI service plus its browser UI and Docker definition.

## Guardrails

- Strict schemas reject unknown or malformed tool fields.
- The realtime voice model has no SQL tool. The typed text copilot's optional
  database helper accepts only `SELECT`, allow-lists four candidate tables,
  injects a 50-row limit and enforces a five-second timeout; writes and DDL are
  rejected before execution.
- Search and profile tools expose bounded records and approved evidence.
- Only `add_to_shortlist` modifies stored state.
- State-changing retries are idempotent and written to an effect log.
- Tool scopes prevent corrected writes from racing stale writes.
- Session state is discarded on close; cross-session personalization is not
  part of the Theme 05 realtime harness.
- Speculative retrieval is labeled non-authoritative.
- Unenforceable image requirements are reported rather than silently treated as
  active filters.
- API keys and database credentials come only from environment variables.

![Evidence Inspector showing the fork-and-join retrieval graph and per-node audit tabs](assets/evidence-inspector.png)

## Verified status

- `670` tests pass on Python 3.11.12.
- `12/12` deterministic interruption benchmark scenarios pass.
- The running database health check reports 15,352 candidates and 32,069 chunks.
- PNG role input, WAV transport, real search, constraint revision and candidate
  comparison have been exercised locally.
- Samsung's unreleased hidden evaluator is the only test surface that cannot yet
  be run or claimed.
