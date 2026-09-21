# Interruptible Talent Atlas Design

**Date:** 2026-09-21

**Status:** Approved for implementation

## Objective

Turn the existing Talent Atlas application into a continuous, interruptible voice recruiting agent for Samsung PRISM Theme 5 without replacing its search engine. The user can speak naturally, interrupt the assistant, refine an in-flight search, and inspect complete evidence for every retrieval and ranking stage. The application runs locally against a 10,000-candidate transformed Kaggle subset, with project data and runtime artifacts stored on the external SSD.

## Product Boundary

Gemini Live provides bidirectional audio transport, speech understanding, speech generation, barge-in events, and function-call requests. Talent Atlas owns the agent harness:

- session and turn state;
- incremental transcript routing;
- typed search plans and revisions;
- selective cancellation and completed-work reuse;
- server-side tool authorization and validation;
- hybrid retrieval, fusion, reranking, and evidence grounding;
- full execution telemetry;
- UI graph state;
- fallback behavior and evaluation.

The project does not implement speech recognition, speech synthesis, WebRTC codecs, acoustic echo cancellation, or a foundation audio model. Those components are outside the hackathon's stated technical focus. The original text agent remains available as a fallback.

## User Experience

The primary screen remains a recruiter workspace, not a monitoring dashboard.

1. The center workspace shows the live conversation and candidate results.
2. A stable bottom voice dock shows `Connecting`, `Listening`, `Understanding`, `Searching`, `Speaking`, `Interrupted`, or `Error`.
3. Candidate cards expose `Why this match?`, shortlist, inspect, and compare actions.
4. A compact `Activity` control opens a right-side evidence inspector.
5. The inspector is collapsed by default and can be pinned open for a technical demonstration.
6. The inspector displays a fork-and-join execution graph: query plan, SQL, vector, BM25, exact skills, fusion, reranking, grounding, and answer.
7. Selecting a graph node opens its complete trace span. Selecting `Why this match?` opens the graph and highlights only the branches, scores, and document chunks that supported that candidate.

The assistant speaks a concise summary. The screen carries dense information such as complete candidate lists, score breakdowns, tool arguments, evidence excerpts, and telemetry.

## Runtime Architecture

```text
Browser microphone and text input
        |
        | PCM audio, control events, UI actions
        v
FastAPI realtime WebSocket
        |
        +-- Gemini Live bridge
        |     +-- audio input and output
        |     +-- input/output transcription
        |     +-- interruption events
        |     +-- function-call requests
        |
        +-- Conversation coordinator
        |     +-- turn and revision state
        |     +-- transcript stability gate
        |     +-- cancellation scopes
        |     +-- tool-call reconciliation
        |
        +-- Existing Talent Atlas tools
        |     +-- SQL constraints
        |     +-- pgvector retrieval
        |     +-- BM25/keyword retrieval
        |     +-- exact-skill retrieval
        |     +-- reciprocal rank fusion
        |     +-- optional reranking
        |     +-- evidence grounding
        |
        +-- Telemetry event publisher
              +-- conversation UI events
              +-- graph nodes and edges
              +-- node detail spans
              +-- external-SSD trace bundle
```

The browser connects only to FastAPI. The backend owns the long-lived Gemini Live connection. This adds one network hop but keeps API credentials, tool execution, cancellation, guardrails, and telemetry under one auditable server process.

## Session And Revision Model

Every WebSocket owns one `RealtimeAgentSession`. Its state is not shared with other sessions.

```text
RealtimeAgentSession
  session_id
  connection_state
  active_turn
  latest_revision
  completed_branches
  active_tasks
  current_candidates
  current_evidence
  current_answer
  gemini_resumption_handle
```

Each stable intent change produces an immutable `SearchPlanRevision`:

```text
SearchPlanRevision
  revision_id
  parent_revision_id
  transcript_snapshot
  semantic_query
  must_constraints
  should_preferences
  excluded_constraints
  requested_actions
  changed_fields
  branch_fingerprints
  created_at
```

Branches use semantic fingerprints derived from their normalized inputs and relevant configuration. A new revision reuses a completed branch only when its fingerprint is unchanged. This is stricter than reusing a branch because its display label looks similar.

## Conversation Flow

1. The browser captures mono PCM audio and sends small frames over the application WebSocket.
2. The Gemini bridge forwards frames to Gemini Live and publishes input-transcription deltas.
3. The transcript stability gate classifies each delta as `WAIT`, `SPECULATE`, `COMMIT`, or `SUPPRESS`.
4. `SPECULATE` may begin read-only retrieval from a stable prefix before end-of-speech.
5. A Gemini function call or stable final transcript is validated into a typed search-plan revision.
6. The coordinator diffs the revision against its parent and determines reused, new, replaced, and cancelled branches.
7. Retrieval branches run concurrently and emit graph updates as they start, return results, or fail.
8. Fusion emits provisional candidate updates whenever a branch completes.
9. The committed revision performs final fusion, optional reranking, and evidence grounding.
10. A compact grounded tool result is returned to Gemini. Full candidates and telemetry go directly to the browser.
11. Gemini speaks a short response while the browser renders candidate cards and citations.

## Gemini Tool Boundary

Gemini receives a small, explicit tool registry rather than database access:

- `search_candidates`: create a typed search revision from a new recruiting request;
- `revise_search`: patch the current typed revision without silently losing prior constraints;
- `inspect_candidate`: retrieve approved profile and document evidence for a selected candidate;
- `compare_candidates`: compare an explicit bounded candidate set using retrieved evidence;
- `format_current_answer`: change presentation without rerunning retrieval;
- `cancel_current_action`: request cancellation of cancellable work.

Every call passes through Pydantic validation and a server registry. Unknown tools, unknown fields, oversized result counts, raw SQL, cross-session identifiers, and unsupported side effects are rejected. Gemini receives compact JSON containing candidate IDs, display summaries, evidence IDs, and execution status. It does not receive credentials, unrestricted database rows, or the full telemetry bundle.

## Interruption And Cancellation

An interruption is a first-class event, not a new chat message glued onto the end.

When the user speaks during assistant output:

1. browser playback stops and buffered output audio is discarded;
2. the current spoken response is marked `superseded`;
3. completed retrieval branches remain available;
4. speculative or obsolete read-only tasks receive cooperative cancellation;
5. the new transcript becomes a child revision;
6. the coordinator computes the minimal branch delta;
7. reused nodes stay visible and replaced nodes retain lineage;
8. fusion, reranking, grounding, and the answer rerun for the committed revision.

Read-only searches are cancellable. A completed upload, shortlist action, or other side effect is never rolled back implicitly. Side-effecting tools require explicit confirmation and idempotency keys.

## Execution Graph

The graph is evidence on demand. It is not visible by default.

Node classes:

- transcript and intent gate;
- plan revision and diff;
- Gemini tool request and validated server call;
- SQL, vector, BM25, and exact-skill branches;
- cache lookup;
- rank fusion;
- cross-encoder reranking;
- evidence grounding;
- answer generation;
- cancellation, retry, or fallback.

Node statuses:

- `queued`, `running`, `completed`, `reused`, `replaced`, `cancelled`, `failed`, `superseded`.

Every node detail contains:

- trace, span, session, turn, and revision identifiers;
- parent/child edges and reuse lineage;
- start, end, duration, and time-to-first-result;
- validated and redacted inputs;
- model, index, prompt, and configuration versions;
- cache status;
- complete bounded outputs and score contributions;
- downstream consumers;
- evidence and document/chunk identifiers;
- retry, fallback, exception, and cancellation data;
- a raw event representation.

Passwords, API keys, connection strings, authorization headers, and unrestricted environment values are always redacted.

## Event Protocol

Server-to-browser events use a versioned envelope:

```json
{
  "schema_version": 1,
  "event_id": "evt_...",
  "type": "node.completed",
  "session_id": "session_...",
  "turn_id": "turn_...",
  "revision_id": "rev_...",
  "timestamp_ms": 486.2,
  "payload": {}
}
```

Event families are `session.*`, `audio.*`, `transcript.*`, `turn.*`, `plan.*`, `tool.*`, `node.*`, `fusion.*`, `candidate.*`, `evidence.*`, `answer.*`, `interruption.*`, `metrics.*`, and `error.*`.

The UI stores events in an append-only client timeline and derives graph state from them. Reconnecting does not mutate old events; the server sends a snapshot followed by new deltas.

## External-SSD Runtime Contract

All intentional project runtime state lives below:

```text
/Volumes/MAC/Projects_devolopment/Samsum_rag/.runtime/
  postgres/
  cache/huggingface/
  cache/transformers/
  cache/torch/
  cache/pip/
  cache/uv/
  tmp/
  logs/
  traces/
  recordings/
```

The Python environment lives at `Samsum_rag/.venv`. Docker Compose uses an SSD bind mount for PostgreSQL instead of a Docker named volume. Environment setup exports `HF_HOME`, `TRANSFORMERS_CACHE`, `SENTENCE_TRANSFORMERS_HOME`, `TORCH_HOME`, `PIP_CACHE_DIR`, `UV_CACHE_DIR`, `XDG_CACHE_HOME`, and `TMPDIR` into `.runtime`.

Docker Desktop may retain its engine image metadata in its configured virtual-disk location. The project will not intentionally store database files, models, datasets, logs, traces, recordings, or Python dependencies on the internal drive. A setup check reports every resolved runtime path before launch.

## Local Dataset

The local database contains exactly 10,000 transformed rows from the existing public Kaggle recruitment dataset. Import remains deterministic and records:

- source dataset URL and checksum;
- transformation version;
- selected offset and row count;
- inserted and deduplicated candidates;
- document and chunk counts;
- embedding model and dimensions;
- import duration and failures.

This is a real public dataset transformed into the Talent Atlas schema, not a production recruiting corpus. The UI and documentation state that limitation.

## Failure And Fallback Behavior

- Gemini unavailable: switch to the existing typed text agent and keep the graph.
- Microphone denied: text input remains fully functional.
- Audio output fails: continue with transcript output.
- Transcript instability: wait rather than launching noisy searches.
- Tool validation fails: emit a visible rejected-tool node and ask for clarification.
- One retrieval branch fails: fuse remaining branches and label degraded evidence.
- Database unavailable: stop retrieval, retain the conversation, and surface a retry action.
- Socket reconnect: restore a session snapshot where possible; otherwise begin a visibly new session.
- Stale result arrives after cancellation: retain it only as cancelled telemetry and never merge it into the current revision.

## Observability And Evaluation

The harness records both user-visible graph events and Langfuse/OpenTelemetry spans. Required metrics include:

- time to first transcript;
- time to first speculative retrieval;
- time to first candidate;
- time to grounded answer;
- interruption-to-silence latency;
- interruption-to-new-plan latency;
- branch cancellation and reuse counts;
- wasted work after interruption;
- tool-call validation failures;
- evidence coverage;
- retrieval and ranking latency;
- Gemini token and estimated cost data where available.

Evaluation covers normal search, mid-sentence refinement, goal replacement, presentation-only turns, rapid double interruption, failed tool arguments, stale result arrival, reconnect, and microphone denial.

## Implementation Slices

1. External-SSD runtime and reproducible 10k corpus.
2. Versioned events, typed revisions, branch fingerprints, and telemetry tests.
3. Selective cancellation and retrieval reuse in the existing controller.
4. Gemini Live server bridge and server-side tool registry.
5. Continuous browser audio capture/playback with interruption handling.
6. Talent Atlas conversational workspace and collapsed Activity inspector.
7. Interactive fork-and-join graph with complete node telemetry.
8. End-to-end evaluation, load testing, screenshots, and demo script.

Each slice must preserve the text workflow and have deterministic tests before the next slice begins.

## Acceptance Criteria

- A user can hold a continuous voice conversation and interrupt assistant speech.
- Retrieval starts from stable intent before the utterance ends.
- A refinement changes only affected branches and visibly reuses unaffected work.
- Gemini can call only registered, validated server-side tools.
- Candidate claims link to actual retrieved chunks.
- Every graph node exposes complete redacted telemetry.
- The graph is collapsed by default and does not obstruct normal recruiting work.
- The system runs locally against 10,000 candidates.
- All intentional application data and runtime artifacts resolve to the external SSD.
- Existing text search, Talent UI, and deterministic search tests continue to pass.
