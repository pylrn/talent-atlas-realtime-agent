# Handoff: Agent Copilot — UI Polish, Ranking Explanation Integration & Smart Suggestions

## Session Metadata
- Created: 2026-06-09 19:03:56
- Project: /Volumes/MAC/Projects_devolopment/hybrid search
- Branch: feat/personalization-hints
- Session duration: ~6 hours across multiple context windows

### Recent Commits (for context)
  - de8596b fix(ui): add push-to-main listener, _lastSearchFilters wiring, flex layout for side panel
  - d87098a feat(ui): integrate agent side panel into admin.html
  - 872b759 feat(ui): agent side panel JS — SSE consumer, chat renderer, candidate cards
  - 0b98551 feat(api): search history background logging + GET /search/history endpoint
  - 2dd24a1 fix(api): add 60s timeout to agent event queue drain loop

NOTE: All work in this session is uncommitted — large set of changes across agent pipeline, UI, and prompt files.

## Handoff Chain

- **Continues from**: None (prior context window was summarised by Claude's auto-compaction)
- **Supersedes**: None

## Current State Summary

The recruiter agent copilot (side panel in admin.html) has been significantly improved across two areas: (1) UI polish — markdown rendering via marked.js, sticky scroll, send↔stop button, quick-action chips, resizable panel, graceful tool failure display; (2) pipeline intelligence — the agent's tool results now include full ranking explanation data (score breakdown, retrieval paths, required/preferred checks, best evidence, spec quality signals). The system prompt was completely rewritten to teach the LLM how to interpret these signals. A smart server-side suggestion chip system was added (`SUGGESTED_ACTIONS` SSE event) that derives context-aware next-action chips from the live search state (e.g. "Relax city filter (remove Bangalore)", "Make Kubernetes optional (missing in 4/5)") — these appear in-chat with a "Didn't get the results you want?" header. The score bug where "View in main panel" showed 0.000 for all candidates was also fixed (`r.score` → `r.feature_score` field rename propagated through the push flow).

## Codebase Understanding

### Architecture Overview

This is a hybrid candidate-search recruitment platform with a 3-stage AI ranking pipeline:
1. **Retrieval** — Dense (embedding ANN) + BM25 (FTS) + Skill exact-match, merged via RRF
2. **Cross-encoder re-ranking** — top-25 candidates re-scored by cross-encoder
3. **Feature-weighted scoring** — 8 signals (cross_encoder, retrieval, skill_match, should_match, experience_fit, skill_recency, completeness, personalization) weighted and summed → `feature_score` (0–100)

The agent copilot is a PydanticAI agent (`pipeline/agent.py`) that runs tools against the same DB and pipeline, streamed as SSE via `pipeline/agent_run.py` using `agent.iter()` (NOT `run_stream()` — that silently drops tool calls). The agent is driven by DeepSeek (`deepseek:deepseek-chat`) by default with Groq/Gemini fallback.

Key insight: `SearchResult.explanation` is already populated by `pipeline/explanation.py`'s `build_explanation()` at the end of every search. The agent tools just needed to read `r.explanation` rather than duplicating the logic.

### Critical Files

| File | Purpose | Relevance |
|------|---------|-----------|
| `pipeline/agent.py` | Agent definition, all 13 tools, system prompt (`build_system_prompt`) | System prompt + tool wiring |
| `pipeline/agent_run.py` | SSE streaming via `agent.iter()`, retry logic, domain events, `_derive_suggestions()` | Core streaming loop |
| `pipeline/agent_tools.py` | Pure async tool helpers: `do_run_search`, `do_explain_poor_results`, etc. | All DB/pipeline logic |
| `pipeline/agent_stream.py` | SSE event constructors | All SSE event types including new `suggested_actions_event` |
| `pipeline/agent_session.py` | `AgentSession`, `StackEntry` (now has `spec_summary`, `total_scanned`) | Session state |
| `pipeline/explanation.py` | `build_explanation()` — populates `r.explanation` in every search | Ranking explanation source |
| `pipeline/search_result.py` | `SearchResult` dataclass — all score fields | Field reference |
| `api/main.py` | FastAPI app: `/agent/chat`, `/agent/push-results`, CORS, static files | API endpoints |
| `api/static/agent-panel.js` | Frontend SSE consumer, chat renderer, all UI logic | All agent UI |
| `api/static/admin.html` | Main admin UI — includes marked.js CDN, agent panel CSS | CSS + HTML structure |

### Key Patterns Discovered

- **PydanticAI streaming**: use `agent.iter()` not `run_stream()`. Node types: `is_model_request_node` (stream text/thinking), `is_call_tools_node` (FunctionToolCallEvent/FunctionToolResultEvent).
- **No-arg tool bug with Groq**: zero-param tools fail (Groq sends `arguments: "null"`). All tools that conceptually take no args have been given a required string param (`reason`, `concern`, `note`).
- **asyncpg type coercion**: `_jsonable()` helper in `agent_tools.py` converts UUID/Decimal/datetime from asyncpg rows before `json.dumps`.
- **SQL allowlist**: `_ALLOWED_TABLES = {"candidates", "candidate_skills", "candidate_documents", "document_chunks"}` — `resume_chunks` does NOT exist; `document_chunks` is the correct table.
- **Score field naming**: `feature_score` (0–100) is the final agent score. `rank_score` is the API-level alias. `similarity_score` is 0–1 (cosine). The push-results endpoint divides `feature_score` by 100 to set `similarity_score`.
- **Marked.js**: loaded from `https://cdn.jsdelivr.net/npm/marked@9/marked.min.js` before agent-panel.js. `window.marked.parse(text)` used in `appendAnswer`.
- **GEMINI_API_KEY alias**: `api/main.py` sets `os.environ["GOOGLE_API_KEY"] = GEMINI_API_KEY` at startup so pydantic_ai's `google:` provider works.

## Work Completed

### Tasks Finished

- [x] Fix "View in main panel" score showing 0.000 — `r.score` → `r.feature_score` in agent-panel.js + divide by 100 for similarity_score in push-results endpoint
- [x] Enrich agent tool results with full ranking explanation data (`_enrich_result`, `_spec_summary` helpers in agent_tools.py)
- [x] Rewrite `do_explain_poor_results` to use actual ranking signals (retrieval path distribution, required check failures, planner confidence)
- [x] Rewrite `do_compare_iterations` to include score deltas, tier shifts, path distribution changes
- [x] Extend `StackEntry` with `spec_summary` and `total_scanned` fields
- [x] Update `view_current_results` tool to return `spec_summary` and `total_scanned`
- [x] Complete system prompt rewrite — project description, scoring pipeline explanation, signal glossary, retrieval path semantics, 15 edge-case examples
- [x] Add markdown rendering (marked.js CDN + `innerHTML = marked.parse(rawText)` in appendAnswer)
- [x] Sticky scroll — `isNearBottom()` check, only auto-scroll within 80px of bottom
- [x] Send↔Stop button — `setGenerating(busy)` swaps button text/class; single onclick handler checks `state.isGenerating`
- [x] Resizable panel — draggable left-border handle via mousedown/mousemove
- [x] Remove hardcoded quick-action chips — bottom bar now hides once conversation starts
- [x] Remove feedback thumbs (👍/👎) from chat
- [x] Add `SUGGESTED_ACTIONS` SSE event — server-side `_derive_suggestions()` reads live session stack to produce context-aware chips
- [x] In-chat "Didn't get the results you want?" strip with smart refinement chips
- [x] Update system prompt to teach LLM about suggestion chips and how to write closing lines
- [x] Graceful tool failure — `closeRunningSteps(turn)` marks stuck steps as "interrupted" on JSON parse error or abort

### Files Modified

| File | Changes | Rationale |
|------|---------|-----------|
| `pipeline/agent.py` | Full system prompt rewrite; `view_current_results` tool returns `spec_summary` | Richer LLM context, explanation data |
| `pipeline/agent_run.py` | Added `_derive_suggestions()`, emit `SUGGESTED_ACTIONS` after successful run | Smart in-chat refinement chips |
| `pipeline/agent_stream.py` | Added `suggested_actions_event()` | New SSE event type |
| `pipeline/agent_tools.py` | Added `_enrich_result()`, `_spec_summary()`; rewrote `do_run_search`, `do_explain_poor_results`, `do_compare_iterations` | Full ranking explanation in tool results |
| `pipeline/agent_session.py` | Added `spec_summary: dict` and `total_scanned: int` to `StackEntry` | Persist planner data for diagnosis |
| `api/static/agent-panel.js` | Full rewrite: markdown, sticky scroll, stop button, smart chips, graceful failure, `appendSuggestedActions` | All 7 UI improvements |
| `api/static/admin.html` | marked.js CDN, resize handle, quick-actions container, CSS for all new elements | Supporting UI |
| `api/main.py` | `similarity_score = score / 100` in push-results endpoint | Fix 0.000 score display |

### Decisions Made

| Decision | Options Considered | Rationale |
|----------|-------------------|-----------|
| Server-side derived suggestions (not LLM-generated) | LLM tool call vs. deterministic derivation | No extra latency, no extra token cost; reads actual ranking signals which are more precise than LLM guesses |
| `agent.iter()` not `run_stream()` | Both are PydanticAI APIs | `run_stream()` silently drops tool calls when model returns text+tools together — confirmed bug |
| DeepSeek as default model | Groq llama, Gemini, DeepSeek | DeepSeek produces most reliable tool-call JSON; Groq has rate limits and no-arg tool bug |
| `_enrich_result` reads `r.explanation` (existing) | Rewrite explanation logic | `explanation.py`'s `build_explanation()` already runs in every search — just read it, don't duplicate |
| Suggestions appear in-chat (not bottom bar) | Fixed bottom bar vs. in-chat strip | In-chat suggestions are contextual to the specific turn; bottom bar chips were confusing when stale |

## Pending Work

## Immediate Next Steps

1. **Test the full flow end-to-end**: open admin.html, run a search (e.g. "Python engineers in Bangalore"), check that "Didn't get the results you want?" chips appear with context-aware text (not hardcoded)
2. **Test "View in main panel"**: check scores are no longer 0.000 — should show e.g. "78.4/100" in the rank chip
3. **Commit all uncommitted changes** — large set of files modified across this session, none committed
4. **Consider rerank_pool tool**: agent can filter by keyword/SQL but can't re-score a sub-pool semantically. A `rerank_pool(candidate_ids, query)` tool that runs dense embeddings over just those IDs would enable "rank these 10 candidates for fit against this JD"

### Blockers/Open Questions

- [ ] No known blockers — all changes compile and parse cleanly (verified with `python -c "import ast; ast.parse(...)"`)
- [ ] The `_derive_suggestions()` function reads `top.spec_summary` which is populated by new `_spec_summary()` helper — only works for searches run AFTER this session's changes. Old stack entries from before won't have it (harmless, `or {}` guards everywhere)

### Deferred Items

- Semantic re-ranking of a candidate sub-pool (`rerank_pool` tool) — useful but non-trivial
- Agent feedback logging to Langfuse (the `/agent/feedback` POST endpoint doesn't exist yet — the JS fires-and-forgets silently)
- The agent's narrative filler between tool calls sometimes leaks into the answer text — could tighten prompt

## Context for Resuming Agent

## Important Context

**The scoring data chain**: `search.py` calls `build_explanation(r, spec)` → stores on `r.explanation`. `do_run_search` calls `_enrich_result(r, i)` which reads `r.explanation`. The `StackEntry.results_preview` list now contains fully enriched dicts. `view_current_results` tool returns them with `spec_summary`. The LLM sees all signals: `feature_score`, `match_tier`, `retrieval_paths`, `required_checks`, `score_breakdown`, `best_evidence`.

**The suggestions chain**: After `_run_once()` completes in `stream_agent_run()`, it calls `_derive_suggestions(session)` which reads `session.search_stack[-1]` (the latest StackEntry) for filters, spec_summary, result quality, retrieval paths, and failed checks. Yields `SUGGESTED_ACTIONS` SSE event. Frontend handles it in `handleSSEEvent` → `appendSuggestedActions(turn, suggestions)`.

**DeepSeek env var**: `DEEPSEEK_API_KEY` in `.env`. Model string: `"deepseek:deepseek-chat"`. The pydantic_ai deepseek provider reads this automatically.

**Gemini env var**: pydantic_ai's `google:` provider reads `GOOGLE_API_KEY`. But the user's `.env` has `GEMINI_API_KEY`. A startup alias in `api/main.py` copies it: `os.environ.setdefault("GOOGLE_API_KEY", os.environ.get("GEMINI_API_KEY", ""))`.

**DB schema truth**: Resume text lives in `document_chunks.content` (with `content_tsv` GIN index for FTS). Candidate skills with recency are in `candidate_skills` (has `last_used_at`). There is NO `resume_chunks` table and NO `candidates.resume_text` column — these were historical bugs now fixed everywhere.

### Assumptions Made

- The `SearchResult.explanation` field is populated for all results from `smart_search()` in quality mode (cross-encoder enabled). Filter-only searches may return results without explanation.
- `marked.js` v9 from jsDelivr CDN is loaded synchronously before agent-panel.js — `window.marked` is available when `appendAnswer` runs.
- The agent model (`deepseek:deepseek-chat`) is set per-request in `stream_agent_run()` via the `model=` kwarg to `agent.iter()`, overriding the agent's default.

### Potential Gotchas

- **Groq no-arg tool bug**: Every tool that takes no logical parameters has a required string arg added (e.g. `reason: str`, `concern: str`). If you add a new zero-param tool, give it a required string arg or Groq llama will fail with `tool_use_failed`.
- **`StackEntry` backward compat**: `spec_summary` and `total_scanned` fields have `field(default_factory=dict)` and `= 0` defaults so old in-memory sessions (from before restart) don't crash.
- **Score scale mismatch**: `feature_score` is 0–100. `similarity_score` in the `SearchResponse` shape is 0–1. The `/agent/push-results` endpoint divides by 100 when setting `similarity_score`. Don't change this without updating both sides.
- **SSE format**: Events are `"event: TYPE\ndata: JSON\n\n"`. The frontend splits on `"\n\n"` and parses `event:` and `data:` lines separately. Multi-line data payloads need careful escaping.

## Environment State

### Tools/Services Used

- **FastAPI** dev server: `uvicorn api.main:app --reload` (port 8000 by default)
- **PostgreSQL**: local DB — connection via `DATABASE_URL` env var
- **DeepSeek API**: `DEEPSEEK_API_KEY` — used for agent LLM calls
- **Groq API**: `GROQ_API_KEY` — fallback model
- **Gemini API**: `GEMINI_API_KEY` — second fallback
- **Langfuse** (optional): `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` — observability, no-op if absent

### Active Processes

- FastAPI dev server was being restarted between changes during the session

### Environment Variables

- `DATABASE_URL` — PostgreSQL connection string
- `DEEPSEEK_API_KEY` — primary agent model
- `GROQ_API_KEY` — fallback
- `GEMINI_API_KEY` — fallback (aliased to `GOOGLE_API_KEY` at startup)
- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` — optional observability

## Related Resources

- `pipeline/explanation.py` — the existing explanation builder that populates `r.explanation`
- `pipeline/search_result.py` — `SearchResult` dataclass with all score fields
- `pipeline/feature_ranker.py` — 8-signal feature weighting logic
- `docs/superpowers/plans/2026-05-27-personalization-hints.md` — original plan for this branch
