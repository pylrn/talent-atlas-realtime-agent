# Interruptible Realtime Talent Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add continuous Gemini Live voice conversation, revision-aware interruptible retrieval, and a collapsible full-telemetry execution graph to the existing Talent Atlas UI while running a 10,000-candidate corpus entirely from the external SSD.

**Architecture:** FastAPI owns a server-to-server Gemini Live session and exposes one mixed JSON/binary WebSocket to the browser. A typed coordinator converts transcript changes and Gemini function calls into immutable search revisions, fingerprints independent retrieval branches, reuses unchanged work, cancels stale work, and emits an append-only graph event stream. The existing hybrid search engine remains the only retrieval implementation.

**Tech Stack:** Python 3.13, FastAPI WebSockets, Pydantic v2, Google Gen AI SDK, asyncio, PostgreSQL 17 with pgvector, vanilla JavaScript AudioWorklet, HTML/CSS, pytest, Playwright.

---

## File Map

- `scripts/setup_external_runtime.sh`: creates and validates SSD runtime directories and exports cache paths.
- `docker-compose.yml`: changes PostgreSQL from a Docker named volume to the project-local SSD bind mount.
- `pipeline/realtime_events.py`: versioned event envelopes, redaction, trace nodes, and in-memory trace graph.
- `pipeline/realtime_plan.py`: typed search revisions, plan diffs, and deterministic branch fingerprints.
- `pipeline/realtime_coordinator.py`: task scopes, selective cancellation, branch reuse, retrieval orchestration, and graph emission.
- `pipeline/gemini_live.py`: Gemini Live transport abstraction and production SDK adapter.
- `pipeline/realtime_tools.py`: Pydantic-validated Gemini tool schemas and bounded dispatcher.
- `api/main.py`: realtime WebSocket endpoint and session lifecycle.
- `api/static/talent-realtime.js`: voice session, PCM transport, playback, graph state, and inspector interactions.
- `api/static/pcm-capture-worklet.js`: 16 kHz PCM microphone frames.
- `api/static/talent-realtime.css`: voice dock, collapsed Activity rail, graph, and telemetry panel.
- `api/static/talent.html`: semantic containers and script/style integration.
- `tests/test_realtime_*.py`: backend unit and endpoint contracts.
- `tests/test_talent_realtime_ui.py`: static UI and accessibility contracts.
- `scripts/evaluate_realtime_agent.py`: deterministic interruption and revision evaluation.

## Task 1: External-SSD Runtime And Reproducible Environment

**Files:**
- Create: `scripts/setup_external_runtime.sh`
- Modify: `docker-compose.yml`
- Modify: `.env.example`
- Test: `tests/test_external_runtime.py`

- [ ] **Step 1: Write the failing runtime contract test**

```python
def test_compose_uses_project_local_postgres_bind_mount():
    compose = Path("docker-compose.yml").read_text()
    assert "./.runtime/postgres:/var/lib/postgresql/data" in compose
    assert "pgdata:" not in compose

def test_setup_exports_all_heavy_caches_to_runtime():
    setup = Path("scripts/setup_external_runtime.sh").read_text()
    for name in ["HF_HOME", "TRANSFORMERS_CACHE", "TORCH_HOME", "PIP_CACHE_DIR", "TMPDIR"]:
        assert f"export {name}=" in setup
```

- [ ] **Step 2: Run the test and confirm it fails because the bind mount and script are absent**

Run: `python3 -m pytest tests/test_external_runtime.py -q`

- [ ] **Step 3: Implement the setup script and compose bind mount**

The script resolves its own project root, rejects any root not under `/Volumes/MAC/`, creates `.runtime/{postgres,cache,tmp,logs,traces,recordings}`, exports all model/package caches there, and prints resolved paths. Compose mounts `./.runtime/postgres` and sets a unique project container name.

- [ ] **Step 4: Create `.venv` on the external SSD and install the project**

Run: `source scripts/setup_external_runtime.sh && python3 -m venv .venv && .venv/bin/pip install -e '.[dev,extractors]'`

- [ ] **Step 5: Run maintained baseline tests**

Run: `.venv/bin/python -m pytest tests -q`

Expected: maintained tests pass or any pre-existing failures are recorded before feature edits.

## Task 2: Versioned Telemetry And Typed Revisions

**Files:**
- Create: `pipeline/realtime_events.py`
- Create: `pipeline/realtime_plan.py`
- Create: `tests/test_realtime_events.py`
- Create: `tests/test_realtime_plan.py`

- [ ] **Step 1: Write failing tests for redaction, event ordering, fingerprints, and plan diffs**

```python
def test_event_payload_redacts_credentials():
    event = EventEnvelope.create("tool.started", payload={"api_key": "secret", "query": "python"})
    assert event.payload == {"api_key": "[REDACTED]", "query": "python"}

def test_location_change_replaces_only_sql_branch():
    old = SearchPlanRevision.from_request(query="python postgres", city="Pune")
    new = old.patch(city="Bengaluru")
    diff = diff_revisions(old, new)
    assert diff.reused == {"vector", "bm25", "skills"}
    assert diff.replaced == {"sql"}
```

- [ ] **Step 2: Verify the tests fail with missing modules**

- [ ] **Step 3: Implement immutable Pydantic models and SHA-256 branch fingerprints**

Fingerprints include only inputs that affect that branch. Redaction recursively masks key names matching `key`, `secret`, `password`, `authorization`, `database_url`, or `dsn`.

- [ ] **Step 4: Run both test files and refactor only after green**

## Task 3: Revision-Aware Retrieval Coordinator

**Files:**
- Create: `pipeline/realtime_coordinator.py`
- Modify: `pipeline/live_rag.py`
- Create: `tests/test_realtime_coordinator.py`
- Modify: `tests/test_live_rag.py`

- [ ] **Step 1: Write failing tests with a controllable fake engine**

Tests prove that independent branches start concurrently, completed fingerprints are reused, changed SQL work is replaced, stale completion never enters fusion, presentation-only turns do not retrieve, and cancellation emits a terminal telemetry node.

- [ ] **Step 2: Verify each test fails for the intended missing behavior**

- [ ] **Step 3: Implement `RealtimeCoordinator`**

Use one `asyncio.TaskGroup`-compatible task map per revision. Retrieval branches call existing engine methods only. Every transition emits `node.created`, `node.started`, `node.completed`, `node.reused`, `node.cancelled`, or `node.failed` through `TraceGraph`.

- [ ] **Step 4: Adapt `LiveRAGSession` as a compatibility wrapper**

The existing transcript demo continues to call `process()`, but delegates revision and telemetry behavior to the new coordinator.

- [ ] **Step 5: Run coordinator and existing Live RAG tests**

Run: `.venv/bin/python -m pytest tests/test_realtime_coordinator.py tests/test_live_rag.py -q`

## Task 4: Gemini Live Bridge And Guardrailed Tools

**Files:**
- Create: `pipeline/gemini_live.py`
- Create: `pipeline/realtime_tools.py`
- Create: `tests/test_gemini_live.py`
- Create: `tests/test_realtime_tools.py`
- Modify: `pipeline/config.py`
- Modify: `.env.example`

- [ ] **Step 1: Write transport tests against an in-memory fake Gemini session**

Tests prove audio forwarding, input/output transcription events, binary output audio, interruption propagation, function-call dispatch, tool-response correlation, and clean closure.

- [ ] **Step 2: Write tool-dispatch tests**

Unknown tools, raw SQL, unknown fields, and `top_k > 20` are rejected. Search and revision calls create typed plan revisions. Formatting calls reuse current evidence.

- [ ] **Step 3: Implement an interface-first bridge**

`GeminiLiveBridge` depends on a `LiveTransport` protocol. `GoogleGenAILiveTransport` lazily imports `google.genai`, configures audio response, input/output transcription, automatic activity detection, session resumption, context compression, system instructions, and the bounded tool declarations.

- [ ] **Step 4: Run bridge and tool tests without network access**

Run: `.venv/bin/python -m pytest tests/test_gemini_live.py tests/test_realtime_tools.py -q`

## Task 5: Realtime FastAPI WebSocket

**Files:**
- Modify: `api/main.py`
- Create: `tests/test_realtime_endpoint.py`

- [ ] **Step 1: Write failing WebSocket contract tests**

Tests verify `session.start`, text fallback, binary audio acceptance, snapshot request, reset, invalid event rejection, and cleanup cancellation. The app injects a fake bridge factory so tests never contact Gemini.

- [ ] **Step 2: Implement `/talent/realtime/ws`**

JSON frames carry control and telemetry. Binary client frames are 16 kHz PCM input. Binary server frames are 24 kHz PCM output. All JSON events use `EventEnvelope`. `/live-rag/ws` remains available for the old transcript demo.

- [ ] **Step 3: Run endpoint tests and route contract tests**

Run: `.venv/bin/python -m pytest tests/test_realtime_endpoint.py tests/test_talent_ui_contracts.py -q`

## Task 6: Continuous Voice UI And Collapsed Activity Inspector

**Files:**
- Modify: `api/static/talent.html`
- Create: `api/static/talent-realtime.css`
- Create: `api/static/talent-realtime.js`
- Create: `api/static/pcm-capture-worklet.js`
- Create: `tests/test_talent_realtime_ui.py`

- [ ] **Step 1: Write failing static UI contracts**

Tests require a labelled microphone button, stable voice state, stop-call control, text fallback, collapsed Activity button, dialog-like right inspector, graph viewport, node telemetry tabs, focus management, and reduced-motion CSS.

- [ ] **Step 2: Implement microphone capture and output playback**

The AudioWorklet downsamples browser audio to mono 16-bit PCM at 16 kHz. The main script sends binary chunks only while connected. Received 24 kHz PCM is queued in `AudioContext`; interruption immediately stops and clears queued sources.

- [ ] **Step 3: Implement derived graph rendering**

Render SVG edges and HTML nodes from append-only events. Fork retrieval nodes from the plan, join at fusion, and preserve prior revision nodes. Node selection renders Overview, Input, Results, Evidence, and Raw Event tabs.

- [ ] **Step 4: Integrate candidate evidence links**

`Why this match?` opens Activity, selects the grounding node, and filters its telemetry to the candidate ID and cited chunks.

- [ ] **Step 5: Run static contracts**

Run: `.venv/bin/python -m pytest tests/test_talent_realtime_ui.py tests/test_talent_ui_contracts.py -q`

## Task 7: Local 10k Corpus

**Files:**
- Modify: `scripts/import_recruitment_dataset.py`
- Create: `scripts/verify_local_corpus.py`
- Create: `tests/test_local_corpus_manifest.py`
- Generate ignored runtime artifacts under `data/recruitment_dataset/` and `.runtime/`

- [ ] **Step 1: Add a failing manifest contract test**

The importer must write source URL, SHA-256, transform version, offset, requested/actual count, and output checksums.

- [ ] **Step 2: Implement deterministic manifest output and verification command**

- [ ] **Step 3: Start PostgreSQL with the SSD bind mount**

Run: `source scripts/setup_external_runtime.sh && docker compose up -d postgres redis`

- [ ] **Step 4: Transform and load exactly 10,000 records**

Run: `source scripts/setup_external_runtime.sh && .venv/bin/python scripts/import_recruitment_dataset.py --limit 10000 --load-db`

- [ ] **Step 5: Verify database, document, chunk, embedding, and filesystem counts**

Run: `.venv/bin/python scripts/verify_local_corpus.py --expected-candidates 10000 --require-external-root`

## Task 8: End-To-End Evaluation And Visual Verification

**Files:**
- Create: `scripts/evaluate_realtime_agent.py`
- Modify: `README.md`
- Modify: `docs/current_pipeline_status.md`
- Test: `tests/test_realtime_evaluation.py`

- [ ] **Step 1: Add deterministic transcript scenarios**

Cover initial search, mid-sentence constraint, goal replacement, presentation-only request, rapid double interruption, rejected tool arguments, stale completion, reconnect, and microphone denial.

- [ ] **Step 2: Run the full maintained test suite**

Run: `.venv/bin/python -m pytest tests -q`

- [ ] **Step 3: Start the app locally from the SSD environment**

Run: `source scripts/setup_external_runtime.sh && .venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8010`

- [ ] **Step 4: Verify `/talent` with Playwright at desktop and mobile sizes**

Check microphone permission failure, text fallback, graph closed by default, drawer focus trap, no overflow, fork-and-join graph, node tabs, candidate evidence deep-linking, console errors, and reduced motion.

- [ ] **Step 5: Run one opt-in Gemini Live smoke test**

Connect, send a short audio or text turn, verify a correlated tool call and grounded result, then close the session. Record tokens/cost if returned.

- [ ] **Step 6: Commit implementation in focused units and report residual limitations**

Do not claim production-corpus validation. State that the system is tested against a transformed public 10k dataset and local concurrency/evaluation scenarios.
