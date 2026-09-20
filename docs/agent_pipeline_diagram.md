# Recruiter Copilot — Full Tool/Function Call Pipeline

End-to-end map of what happens from a recruiter message to the agent, every
function it calls, what those functions return, and exactly how each one talks
to PostgreSQL and how the DB answers back.

Source of truth:
- Agent + prompt + tool surface: `pipeline/agent.py`
- Tool bodies (the `do_*` helpers): `pipeline/agent_tools.py`
- Streaming run loop: `pipeline/agent_run.py`
- HTTP entry point: `api/main.py` (`POST /agent/chat`)
- Search pipeline: `pipeline/search.py` + `pipeline/retrieve_*.py` + `pipeline/planner.py`

---

## 1. Top-Level Request Flow

```mermaid
flowchart TD
    User([Recruiter types message in Talent UI<br/>/static/talent.js])
    User -->|POST /agent/chat SSE| EP["agent_chat()  api/main.py:3717"]

    subgraph SETUP["Turn setup (api/main.py)"]
        EP --> SESS["agent_session.get_or_create(recruiter_id, session_id)<br/>→ in-memory AgentSession (search_stack, pool, shortlist)"]
        EP --> MEM["load_memory(pool, recruiter_id)<br/>pipeline/memory.py"]
        MEM -->|SELECT| DBMEM[(recruiter_memory)]
        EP --> PROF["profile_recruiter_if_stale()<br/>(background task, first turn only)"]
        EP --> PObs["_pick_relevant_observation()<br/>maybe queue a confirm question"]
        EP --> BP["build_agent_prompt_for_run()  agent.py:386<br/>compiles system prompt + memory block<br/>(Langfuse managed prompt OR inline fallback)"]
        BP --> LF["_obs_get_prompt('recruiter-agent')<br/>Langfuse prompt (optional)"]
        EP --> DEPS["AgentDeps(pool, session, recruiter_id,<br/>query, filters, result_ids, hints, observations,<br/>system_prompt, lf_prompt)"]
    end

    DEPS --> SAR["stream_agent_run()  agent_run.py:378"]
    SAR --> OUT([SSE events streamed back to UI:<br/>thinking / text / tool_call_start /<br/>tool_call_result / suggested_actions])
    OUT --> User
```

**Pre-agent short-circuits (handled in `agent_chat` before the LLM runs):**
- If `session.pending_observation` is set and the message is a yes/no →
  `do_confirm_observation()` directly, reply, **return** (no agent run).
- Otherwise the relevant observation (if any) is queued and the question is
  appended *after* the agent's answer.

---

## 2. The Agent Run Loop (`stream_agent_run` → `agent.iter`)

`stream_agent_run` wraps a retry/fallback plan (`_retry_plan(model)`): same
model, then a configured fallback model. The default model is `_agent_model`
(DeepSeek `deepseek-v4-flash` per project config; the `Agent(...)` literal
`groq:llama-3.3-70b-versatile` in `agent.py:472` is overridden at call time).

```mermaid
flowchart TD
    START["stream_agent_run()"] --> RETRY{"for attempt_model in _retry_plan(model)"}
    RETRY --> ITER["_run_once(): async with agent.iter(message, model,<br/>deps, message_history=session.messages) as run"]

    ITER --> NODE{"async for node in run<br/>(PydanticAI graph node)"}

    NODE -->|is_model_request_node| MRN["MODEL REQUEST NODE<br/>LLM generates reply / decides tool calls"]
    MRN --> STREAM["node.stream(): emit PartStart / PartDelta"]
    STREAM -->|TextPart / TextPartDelta| TXT["S.text_start / text_delta → SSE"]
    STREAM -->|ThinkingPart| THK["S.thinking_start / thinking_delta → SSE"]
    STREAM -->|ToolCallPart| SWAP["close text/thinking, switch to tools"]

    NODE -->|is_call_tools_node| CTN["CALL TOOLS NODE<br/>execute the tools the model asked for"]
    CTN --> FTCE["FunctionToolCallEvent<br/>→ S.tool_call_start(id, name, args)<br/>→ _obs_start_tool() span"]
    FTCE --> EXEC["@recruiter_agent.tool wrapper in agent.py<br/>calls the matching do_* helper"]
    EXEC --> FTRE["FunctionToolResultEvent<br/>→ S.tool_call_result(id, summary, parsed)<br/>→ derive UI events (search cards, push-to-main)"]
    FTRE --> NODE

    NODE -->|End node| DONE["persist session.messages<br/>emit _derive_suggestions() chips → SSE<br/>return"]
    RETRY -->|all attempts fail| ERR["_friendly_error() → SSE error"]
```

Key properties:
- Tools are **pure**: each `@recruiter_agent.tool` just `json.dumps(do_*(...))`.
  They never push UI events; `agent_run.py` derives UI events by *observing* the
  tool name + return value.
- Every tool call is wrapped in a Langfuse span (`_obs_start_tool`), nested under
  the `agent.turn` → `agent.respond` → `agent.generate_reply` span tree.

---

## 3. The Tool Surface — 27 tools, grouped by intent

Defined in `agent.py` (`@recruiter_agent.tool`), each delegating to a `do_*` in
`agent_tools.py`. The system prompt's **Intent Routing** section tells the LLM
which to pick.

```mermaid
flowchart LR
    AGENT([recruiter_agent<br/>LLM picks tool by intent])

    subgraph SEARCH["Search (full pipeline)"]
        T1[run_search]
        T2[modify_and_search]
        T3[keyword_search]
        T4[list_skills]
    end
    subgraph INSPECT["Inspection"]
        T5[view_current_results]
        T6[get_candidate_detail]
        T7[explain_poor_results]
        T8[compare_iterations]
    end
    subgraph POOL["Pool (in-memory)"]
        T9[load_candidate_pool]
        T10[filter_from_pool]
        T11[aggregate_pool]
        T12[rerank_pool]
    end
    subgraph DB["Direct DB / history"]
        T13[query_candidates_db]
        T14[list_recent_sessions]
    end
    subgraph MEMUI["Memory & UI"]
        T15[save_hint]
        T16[add_observation]
        T17[confirm_observation]
        T18[show_candidates]
        T19[push_to_main_panel]
        T20[update_shortlist]
        T21[update_working_spec]
        T22[save_search]
        T23[export_shortlist]
    end
    subgraph INTEL["Advanced (LLM sub-calls)"]
        T24[analyze_jd]
        T25[draft_outreach]
        T26[generate_interview_questions]
        T27[compare_candidates]
    end

    AGENT --- SEARCH & INSPECT & POOL & DB & MEMUI & INTEL
```

---

## 4. `run_search` — the 3-Stage Hybrid Pipeline (the heart)

`run_search(query, filters?, weights?)` → `do_run_search()` →
`HybridSearchEngine.smart_search()`. This is the only tool that runs the full
ranking pipeline.

```mermaid
flowchart TD
    TOOL["@tool run_search(query, filters, weights)<br/>agent.py:498"]
    TOOL --> DRS["do_run_search()  agent_tools.py:125"]
    DRS --> SS["engine.smart_search(query, explicit_filters,<br/>mode='quality', recruiter_id, config_overrides=weights)<br/>search.py:151"]

    subgraph P0["Personalization load"]
        SS --> PH["_load_recruiter_hints() + _load_recruiter_prefs()"]
        PH -->|SELECT| DBpref[(recruiter_memory /<br/>recruiter prefs)]
        SS --> RP["load_recruiter_profile() (if ≥5 outcomes)"]
        RP -->|SELECT| DBout[(outcomes / impressions)]
    end

    subgraph P1["PHASE 1 — Query Understanding (planner.py)"]
        SS --> ROUTE["detect_route(query, jd, filters)"]
        ROUTE --> PLAN["plan() = sanitize → cache lookup →<br/>_call_llm → validate → normalize"]
        PLAN --> LLMP{"_call_llm provider<br/>planner.py:268"}
        LLMP -->|gemini| GEM[_call_gemini]
        LLMP -->|groq| GRQ[_call_groq]
        LLMP -->|deepseek| DSK[_call_deepseek]
        LLMP -->|openai| OAI[_call_openai]
        LLMP -->|fail/timeout| FB["fallback_plan() = regex<br/>planner_fallback.py<br/>(sets used_fallback=True)"]
        PLAN --> SPEC["CanonicalSearchSpec<br/>(must / should / must_not, semantic_query,<br/>lexical_terms, skills, confidence)"]
        SPEC --> GATE{"should_short_circuit()"}
        GATE -->|clarify| RET1["return empty + clarify"]
        GATE -->|lookup| LK["_lookup_by_name()"]
        GATE -->|filter_only| FO["_filter_only_search()"]
        GATE -->|proceed| FULL["_full_pipeline()"]
    end

    subgraph P2["PHASE 2 — Retrieval (3 scouts in parallel)"]
        FULL --> FSQL["build_candidate_filter(must, must_not)<br/>→ hard-filter CTE (city/country/years/skills/salary)"]
        FSQL --> GATHER["asyncio.gather(count, dense, bm25, skill)"]
        GATHER --> DENSE["retrieve_dense()  retrieve_dense.py<br/>embedder.embed([text]) → pgvector ANN"]
        GATHER --> BM25["retrieve_bm25()  retrieve_bm25.py<br/>to_tsquery OR-terms"]
        GATHER --> SKILL["retrieve_skill()  retrieve_skill.py<br/>skills array overlap, weighted"]
        DENSE -->|"SELECT … dc.embedding <=> $v::vector AS distance<br/>ORDER BY dc.embedding <=> $v LIMIT k"| DBchunks[(document_chunks<br/>HNSW vector index)]
        BM25 -->|"WHERE dc.content_tsv @@ to_tsquery('english',$q)<br/>ORDER BY ts_rank_cd(...) DESC"| DBchunks
        SKILL -->|"candidates.skills array overlap<br/>unnest must/should + weights"| DBcand[(candidates /<br/>candidate_skills)]
        DENSE --> RRF
        BM25 --> RRF
        SKILL --> RRF["rrf_fuse() — Reciprocal Rank Fusion<br/>fusion.py → fused_rrf_score + retrieval_paths"]
        RRF --> GROUP["group_by_candidate() →<br/>_enrich_candidates()"]
        GROUP -->|"SELECT profile fields + best chunk"| DBcand
    end

    subgraph P3["PHASE 3 — Ranking & Diversity"]
        GROUP --> CE["Stage 2: cross-encoder rerank top-25<br/>reranker.rerank(semantic_query, top_k)<br/>reranker.py (ms-marco-MiniLM)"]
        CE --> FR["Stage 3: score_results() feature ranker<br/>feature_ranker.py — 8 signals →<br/>feature_score 0–100 (+ recruiter_profile)"]
        FR --> MMR["mmr_select() diversity (mmr_lambda)"]
        MMR --> EXPL["build_explanation() per candidate<br/>→ checks, score_breakdown, best_evidence"]
        EXPL --> IMPR["_log_impressions() fire-and-forget<br/>assign impression_id"]
        IMPR -->|INSERT| DBimp[(impressions)]
    end

    EXPL --> RESP["SearchResponse(results, spec,<br/>total_candidates_scanned, phase_timings)"]
    RESP --> ENRICH["_enrich_result() ×10  agent_tools.py:40<br/>feature_score, match_tier, rerank_score,<br/>fused_rrf_score, retrieval_paths, required_checks,<br/>score_breakdown, best_evidence"]
    ENRICH --> PUSH["push_to_stack(session, StackEntry)<br/>spec_summary, phase_timings, results_preview"]
    PUSH --> RECOV{"weak result?<br/>(empty / avg<55 / clarify / conf<0.6)"}
    RECOV -->|yes| DIAG["do_explain_poor_results() inline<br/>→ result['recovery'] = diagnostic"]
    RECOV -->|no| OK
    DIAG --> OK["return JSON dict to agent.py tool<br/>→ json.dumps → LLM"]
```

**What `run_search` returns to the LLM** (`do_run_search` dict):
`results[]` (10 enriched candidates), `total`, `total_scanned`, `iteration_id`,
`query`, `filters`, `spec_summary` (planner confidence / used_fallback /
dropped_items / must_skills…), `clarify_notice`, `phase_timings`, and — when
weak — a `recovery` block with the full diagnostic so the LLM never has to call
`explain_poor_results` separately.

`modify_and_search(changes)` → `do_modify_and_search()` merges
`add_filters`/`remove_filters`/`query` onto the top stack entry, then calls
`do_run_search` again (same pipeline).

---

## 5. Every Other Tool — function → DB query → return

| Tool (agent.py) | Helper (agent_tools.py) | DB interaction | Returns |
|---|---|---|---|
| `keyword_search(query)` | `do_keyword_search` :691 | `SELECT … MAX(ts_rank(dc.content_tsv, plainto_tsquery('english',$1))) … WHERE dc.content_tsv @@ plainto_tsquery(...)` over `document_chunks` ⨝ `candidates` (words AND-ed) | `{results[≤10], total, query}` |
| `list_skills(query)` | `do_list_skills` :738 | `SELECT skill, count(*) FROM candidate_skills [WHERE skill ILIKE $1] GROUP BY skill ORDER BY n DESC` | canonical skill names + counts |
| `view_current_results(reason)` | (inline) / `do_view_main_results` :559 | If agent stack: in-memory top entry. Else `SELECT` profile + best chunk for `result_ids` (`candidates` ⨝ LATERAL `document_chunks`⨝`candidate_documents`) | enriched results list |
| `get_candidate_detail(id)` | `do_get_candidate_detail` :409 | `SELECT … FROM candidates WHERE id=$1`; `SELECT skill FROM candidate_skills WHERE candidate_id=$1`; `SELECT content FROM document_chunks WHERE candidate_id=$1 LIMIT 1` | full profile + skills + best chunk |
| `explain_poor_results(concern)` | `do_explain_poor_results` :233 | none (in-memory over last stack entry) | tier dist, retrieval-path breakdown, required-check failures, spec issues, suggested actions |
| `compare_iterations(a,b)` | `do_compare_iterations` :353 | none (compares 2 stack entries) | score deltas, gained/lost, tier shifts, spec changes |
| `query_candidates_db(sql)` | `do_query_candidates_db` :915 | **read-only** guard (`_BLOCKED_KEYWORDS` rejects UPDATE/DELETE/INSERT/DROP/…), then `asyncio.wait_for(pool.fetch(sql), timeout=5s)` | rows (JSON-coerced) |
| `load_candidate_pool(criteria,limit)` | `do_load_candidate_pool` :772 | `SELECT … FROM candidates WHERE … LIMIT ≤100` into `session.pool_cache` | `{count}` |
| `filter_from_pool(criteria)` | `do_filter_from_pool` :823 | none (in-memory) | `{results[≤20], count}` |
| `aggregate_pool(dimension)` | `do_aggregate_pool` :837 | none (in-memory group-by) | counts by field |
| `rerank_pool(query)` | `do_rerank_pool` :964 | `SELECT content FROM document_chunks … LIMIT 1` per candidate; then `get_reranker().rerank()` (cross-encoder) | reranked pool |
| `list_recent_sessions(limit)` | `do_list_recent_sessions` :849 | `SELECT session_id, title, summary, updated_at FROM agent_chat_sessions WHERE recruiter_id=$1 ORDER BY updated_at DESC` | compact session summaries |
| `save_hint(text)` | `do_save_hint` :447 | `SELECT count(*) FROM recruiter_memory` (cap); `INSERT INTO recruiter_memory` | `{saved}` |
| `add_observation(content,category)` | `do_add_observation` :477 | `INSERT INTO recruiter_memory … ON CONFLICT DO UPDATE SET evidence_count+1, confidence=LEAST(0.95, +0.05)` | observation row |
| `confirm_observation(id,accept)` | `do_confirm_observation` :508 | `SELECT … FROM recruiter_memory`; then `UPDATE … status='dismissed'` (reject) or `status='promoted'` + `INSERT` promoted fact (accept) | `{status}` |
| `update_shortlist(id,status)` | `do_update_shortlist` :951 | none (session.shortlist) | `{shortlist state}` |
| `update_working_spec(updates)` | `do_update_working_spec` :960 | none (session.working_spec) | `{spec}` |
| `save_search()` | `do_save_search` :1038 | `INSERT INTO search_history (recruiter_id, query, filters_json, results_json)` | `{saved}` |
| `export_shortlist()` | `do_export_shortlist` :1074 | reads session shortlist (+ profile fetch) | export payload |
| `analyze_jd(jd_text)` | `do_analyze_jd` :989 | none — `_get_llm_agent()` sub-LLM call | role + 3–5 skills + location |
| `draft_outreach(id,ctx)` | `do_draft_outreach` :1007 | `SELECT` candidate profile, then sub-LLM | outreach message |
| `generate_interview_questions(id,ctx)` | `do_generate_interview_questions` :1022 | `SELECT` candidate profile, then sub-LLM | questions |
| `compare_candidates(a,b)` | `do_compare_candidates` :1050 | `SELECT` both profiles, then sub-LLM | comparison |
| `show_candidates(ids)` | `do_view_main_results` :559 | `SELECT` profiles for ids (renders cards) | results + count |
| `push_to_main_panel(note)` | (inline) | none (reads top stack entry) | `{pushed, query, filters, result_ids}` |

---

## 6. How the Database Communicates Back

All DB access goes through a single shared **`asyncpg.Pool`** held on
`app.state.pool` and passed into `AgentDeps.pool`. Communication pattern:

```mermaid
flowchart LR
    do["do_* helper / SearchEngine"]
    do -->|"pool.fetch / fetchrow / fetchval / execute"| PG[(PostgreSQL 17 + pgvector)]
    PG -->|"asyncpg Record rows"| COERCE["_jsonable() / _jsonable_row()<br/>UUID→str, Decimal→float,<br/>datetime→isoformat, bytes→utf-8"]
    COERCE -->|"plain dicts/lists"| JSON["json.dumps in @tool wrapper"]
    JSON -->|"tool result string"| LLM([LLM context])

    subgraph TABLES["Tables touched"]
        T_c[candidates]
        T_cs[candidate_skills]
        T_cd[candidate_documents]
        T_dc["document_chunks<br/>(embedding vector + content_tsv)"]
        T_rm[recruiter_memory]
        T_acs[agent_chat_sessions]
        T_sh[search_history]
        T_imp[impressions]
    end
    PG --- TABLES
```

Notes:
- **Vector search** uses pgvector's `<=>` cosine operator on
  `document_chunks.embedding` against an HNSW index (`hnsw_ef_search=40` from
  `/health`); the embedding is computed by `pipeline/embedder.py`
  (`all-MiniLM-L6-v2`, 384-dim, cached).
- **Keyword search** uses Postgres FTS: `content_tsv @@ to_tsquery` /
  `plainto_tsquery` with `ts_rank_cd` / `ts_rank` ranking.
- **Skill search** matches the `candidates.skills` text[] array directly with
  per-skill weights via `unnest`.
- All three retrievers share the same **hard-filter CTE**
  (`build_candidate_filter`) so structured filters apply uniformly.
- `query_candidates_db` is the only tool that runs LLM-authored SQL — gated to
  SELECT-only and a 5-second timeout.
- Writes are minimal and intentional: `recruiter_memory` (hints/observations),
  `search_history` (save_search), `impressions` (fire-and-forget logging).

---

## 7. The System Prompt as the Routing Brain (`agent.py:93`)

The prompt (`build_system_prompt`) is what makes the LLM choose correctly. Its
sections map directly onto the machinery above:

- **What This Platform Is / How Ranking Works** → teaches the LLM to read the
  `_enrich_result` fields (`feature_score`, `rerank_score`, `fused_rrf_score`,
  `retrieval_paths`, `score_breakdown`, `best_evidence`) produced by §4 Phase 3.
- **Current Context** (dynamic) → injects active query/filters/result_count and
  the **Recruiter Memory block** (hints + observations from `recruiter_memory`).
- **How to Engage** → SPECIFIC vs BROAD: act immediately vs gather requirements
  before `run_search`.
- **Intent Routing** → the message→tool decision table (§3).
- **Handling Weak Results / the `recovery` block** → tells the LLM that
  `do_run_search` already attaches the diagnostic, so it reads
  `recovery.diagnostic` instead of re-calling `explain_poor_results`.
- **Tool Reference** → the contract for all 27 tools.
- **Operating Rules + Examples (15)** → diagnose-before-fix, smallest-change-
  first, always-confirm-before-`save_hint`, read-the-evidence.

At runtime `build_agent_prompt_for_run` can swap the inline prompt for a
**Langfuse managed prompt** (`recruiter-agent` + label), compiling in
`context_block`, `memory_block`, `current_date`, and `db_schema`; the inline
prompt remains the source-of-truth fallback.

---

## 8. One-Glance Summary

```
Recruiter → POST /agent/chat
  → load memory + build system prompt (+ pending-observation gate)
  → stream_agent_run → agent.iter (DeepSeek default, fallback on error)
      ├─ MODEL REQUEST node → stream thinking/text, decide tool calls
      └─ CALL TOOLS node → run do_* helper → asyncpg → Postgres → JSON → LLM
          └─ run_search ⇒ smart_search:
               Phase 1 planner (LLM→spec, regex fallback)
               Phase 2 retrieve_dense ∥ retrieve_bm25 ∥ retrieve_skill → RRF fuse
               Phase 3 cross-encoder rerank → 8-signal feature score → MMR → explain
               (+ auto-recovery diagnostic when weak)
  → SSE: thinking / text / tool_call_start / tool_call_result / suggested chips
  → (optional) surface observation-confirmation question
```
