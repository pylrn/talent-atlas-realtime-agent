# Agent-First Search Pipeline — Orchestration Design

> How the AI Talent Copilot should route every query type, and exactly where the
> line sits between "let the LLM decide" and "hardcode it."
> Grounded in the current `pipeline/` implementation; validated against the real code.

---

## 1. The one principle that answers your question

You said: *"I don't want to leave the logic or the general pipeline completely on the agent."*
That instinct is correct, and the research backs it. The optimal split is:

> **The LLM owns language. Deterministic code owns everything with a correct answer.**

| Leave to the **LLM agent** | **Hardcode** (deterministic) |
|---|---|
| Understanding messy human intent | Ranking math (RRF, cross-encoder, 8-signal score) |
| Deciding *which* tool + natural-language args | SQL safety + filter parsing |
| Multi-turn requirement gathering | The confidence / clarify gate |
| Synthesizing & explaining results | Result enrichment + refinement chips |
| Conversation, tone, closing lines | Empty-result recovery + retry/fallback |

**Litmus test for any decision:** *"Would two reasonable engineers expect the
same output for the same input?"* If **yes → hardcode it**. If it needs judgment
about ambiguous language → **LLM**.

The agent is the **conductor**, but it should conduct a **constrained orchestra** —
not be handed 22 instruments and asked to improvise the whole symphony.

---

## 2. What you have today (grounded in the code)

Two LLM layers wrapped around a deterministic core:

- **Agent layer** — `recruiter_agent` (PydanticAI), ~22 tools, streams tool calls
  via `agent.iter`, derives UI events. The routing policy lives **only in the system
  prompt** ("specific → act, broad → gather").
  - **Runtime model = DeepSeek**, not Groq. The `Agent(model="groq:llama-3.3-70b")`
    in `pipeline/agent.py` is a constructor default that is *always overridden* at
    runtime by `_agent_model = "deepseek:deepseek-v4-flash"` (`api/main.py`), passed
    into `stream_agent_run(...)`. DeepSeek is a capable tool-caller — so the agent
    is **not** the weak link. (Groq was tried and dropped *because* it can't reliably
    tool-call: zero-arg tools break on it.)
- **Search pipeline** — `smart_search`, exposed to the agent as `run_search` /
  `modify_and_search`. It *already* has excellent **internal** deterministic routing:
  - filter-only short-circuit (`_has_filter_values` → SQL, no LLM),
  - route detect → planner with a **complexity gate** (`_is_simple_query`: ≤4 tokens,
    no NL markers → regex instead of a paid LLM call) → validate → cache,
  - confidence/clarify gate → 3-path retrieval → RRF → cross-encoder → 8-signal score → MMR.
- **Planner LLM (separate from the agent)** — inside `smart_search`, selected by
  `settings.llm_provider` (`pipeline/planner.py`, supports openai/gemini/groq/deepseek).
  This is the *only* place Groq could ever be called, and it's currently set to DeepSeek
  too. So Groq may not be in the live path at all.
- **Reliability scaffolding** — `_retry_plan`, `_friendly_error`, cross-provider
  fallback. This is the **scar from leaving Groq**, not proof the current agent is
  failing. Treat tool-call reliability as *already solved* by the DeepSeek switch
  unless Langfuse traces show otherwise.

### The gap (empirically demonstrated)

Tracing real recruiter inputs through the **real** gate functions
(`pipeline.planner._is_simple_query`, `pipeline.search._has_filter_values`):

```
bucket              | text-simple? | filter-only? | deterministic route taken
--------------------|--------------|--------------|-----------------------------------
chip-only (no text) | —            | True         | BYPASS planner -> SQL filter_only ✅
2-word exact        | True         | False        | planner: regex only (no LLM)     ✅
specific NL search  | False        | False        | planner: LLM call                ✅
broad brief         | False        | False        | planner: LLM call                ✅
these results off   | True (!)     | False        | planner: regex SEARCH  ❌ it's DIAGNOSE
where is Sarah Chen | True (!)     | False        | planner: regex SEARCH  ❌ it's a DB LOOKUP
what can you do     | False        | False        | planner: LLM call      ❌ it's CHITCHAT
```

The deterministic gates are good — **but they live inside `run_search`.** They only
fire *after* the agent has already decided "this is a search." The **first and most
consequential decision — "is this even a search, and which of 22 tools?" — has no
deterministic layer at all.** It rests entirely on a flaky small model. That is the
single highest-leverage place to add structure.

---

## 3. Proposed pipeline (starting at the agent)

Add two thin deterministic layers — **L0 pre-flight router** and **L3 post-flight
guardrails** — around the agent you already have. You keep ~80% of the current code.

```
                          ┌─────────────────────────────────────────┐
   recruiter message ───► │ L0  PRE-FLIGHT ROUTER   (deterministic)  │
                          │  no LLM, or one tiny classifier call     │
                          ├─────────────────────────────────────────┤
                          │ a) structured-only? (chips, empty text)  │──► /search filter_only ──┐
                          │ b) raw JD paste?    → tag jd_mode        │   (skips the agent)      │
                          │ c) classify INTENT bucket:               │                          │
                          │    search │ refine │ inspect │ diagnose  │                          │
                          │    analytics │ action │ memory │ meta    │                          │
                          │ d) SELECT TOOL SUBSET for this turn      │                          │
                          │ e) attach on-screen context (already)    │                          │
                          └───────────────────┬─────────────────────┘                          │
                                              ▼                                                  │
                          ┌─────────────────────────────────────────┐                          │
   meta/chitchat ◄────────│ L1  AGENT — the conductor       (LLM)    │                          │
   (answer, no tool)      │  sees ONLY the scoped tool subset        │                          │
                          │  • specific → act ; vague → gather reqs  │                          │
                          │  • picks ONE tool + NL args              │                          │
                          └───────────────────┬─────────────────────┘                          │
                                              ▼                                                  │
                          ┌─────────────────────────────────────────┐                          │
                          │ L2  TOOLS — deterministic execution      │ ◄────────────────────────┘
                          ├─────────────────────────────────────────┤
                          │ run_search / modify_and_search           │
                          │   └► smart_search:                       │
                          │        filter_only ─(no LLM)             │
                          │        route → planner (regex|LLM→valid→cache)
                          │        confidence/clarify gate           │
                          │        dense+bm25+skill → RRF →          │
                          │        cross-encoder → 8-signal → MMR     │
                          │ keyword_search / query_db / pool tools ─(no rank LLM)
                          │ analyze_jd / outreach / interview_qs ─(focused sub-LLM)
                          │ memory / shortlist / push ─(deterministic state)
                          └───────────────────┬─────────────────────┘
                                              ▼
                          ┌─────────────────────────────────────────┐
                          │ L3  POST-FLIGHT GUARDRAILS (deterministic)│
                          │  • empty/poor results → auto-attach       │
                          │    explain_poor_results signals + relax   │
                          │  • tool_use_failed → retry plan (exists)  │
                          │  • derive refinement chips + next actions │
                          └───────────────────┬─────────────────────┘
                                              ▼
                          ┌─────────────────────────────────────────┐
                          │ L4  RESPONSE — agent writes closing line │
                          │  UI renders result cards + chips         │
                          └─────────────────────────────────────────┘
```

**The shift:** routing intelligence moves from "all in the system prompt, executed by
a flaky model" to "coarse deterministic routing in L0 + L3, fine judgment in L1."

---

## 4. Query taxonomy → which tool, when (all use cases)

L0 maps each message to an intent bucket; the bucket scopes the tools the agent sees.
This is the "how to use which tools when" reference.

| # | Query type | Example | Intent bucket | Tool(s) the agent may use | LLM or deterministic |
|---|---|---|---|---|---|
| 1 | Specific search | "senior python eng, Bengaluru" | `search` | `run_search` | planner LLM (or regex if simple) |
| 2 | Broad / vague brief | "I need a data role" | `search` | *(none yet)* gather reqs → `run_search` | LLM (requirement loop) |
| 3 | JD paste | (pasted JD) | `search` (jd_mode) | `analyze_jd` → `run_search` | focused sub-LLM + planner |
| 4 | Chip / filter-only | chips, empty text | `search` | **bypass agent** → `/search` filter_only | deterministic (no LLM) |
| 5 | Refine / tweak | "tighten to 7+ yrs" | `refine` | `modify_and_search` | deterministic merge + planner |
| 6 | Inspect candidate | "tell me about X" / "why #3?" | `inspect` | `view_current_results`, `get_candidate_detail` | deterministic fetch |
| 7 | Diagnose | "these look off" | `diagnose` | `explain_poor_results` | deterministic signals |
| 8 | Compare | "X vs Y" / "search A vs B" | `inspect` | `compare_candidates`, `compare_iterations` | sub-LLM / deterministic |
| 9 | Analytics / aggregate | "python candidates per city" | `analytics` | `load_candidate_pool`+`aggregate_pool` OR `query_candidates_db` | deterministic |
| 10 | Existence / lookup | "where is Sarah Chen?" | `analytics` | `query_candidates_db` | deterministic SQL |
| 11 | Resume keyword | "who mentions 'Series B'" | `analytics` | `keyword_search` | deterministic FTS |
| 12 | Multi-constraint exact | "ML **and** product" | `analytics`/`search` | `query_candidates_db` (array `@>`) | deterministic |
| 13 | Candidate actions | outreach / interview Qs / shortlist / export / push | `action` | `draft_outreach`, `generate_interview_questions`, `update_shortlist`, `export_shortlist`, `push_to_main_panel`, `save_search` | sub-LLM / deterministic state |
| 14 | Memory / prefs | "always prefer startups" | `memory` | `save_hint`, `confirm_observation`, `add_observation` | deterministic (confirm first) |
| 15 | Meta / chitchat | "what can you do?", "thanks" | `meta` | **no tool** — answer directly | LLM, no tool |

**Tool-surface scoping (the reliability win):** instead of exposing all 22 tools
every turn, L0 hands the agent ~3–6 relevant ones. `search` bucket → `{run_search,
modify_and_search, analyze_jd}`; `analytics` → `{query_candidates_db, keyword_search,
load_candidate_pool, aggregate_pool}`; `meta` → `{}`. Fewer choices = far fewer
`tool_use_failed` errors on small models.

---

## 5. Why this works (the theory)

1. **Reliability (secondary, now that the agent is DeepSeek).** Research notes that
   with many tools "relying solely on the LLM can be brittle." This was the dominant
   concern under Groq; with a capable DeepSeek tool-caller it is **no longer the main
   driver** — do not adopt the full classifier on this argument alone. Narrowing the
   tool surface still helps at the margin, but **measure mis-routing in Langfuse
   before building it** (a classifier can itself misclassify and cascade errors).
2. **Recovery is where failures hide.** ~60% of agentic hallucinations come from
   *unhandled execution errors* — empty vector results, failed SQL, schema
   mismatches that propagate silently. L3 turns those into structured recovery
   (auto-diagnose + relax suggestions) instead of letting the agent narrate fiction.
3. **Reproducibility & cost.** Deterministic gates already save paid LLM calls
   (`_is_simple_query`, filter-only short-circuit). Pushing the same philosophy up to
   L0 means chip-only and chitchat never spin up the full agent. Enterprises
   "value reproducible execution over maximum accuracy" — deterministic routing is a
   production requirement, not a nicety.
4. **It's incremental, not a rewrite.** You already have L2 (excellent) and most of
   L3 (chips, retry). The work is L0 (intent classifier + tool scoping) and making L3
   recovery automatic rather than prompt-dependent. Low risk, high leverage.

---

## 6. Validation (tested against the real system)

- ✅ **Deterministic foundation passes:** `tests/test_filter_only_shortcircuit.py` → **9/9 passed**.
- ✅ **Gate behavior traced on real functions** (§2 table) — confirms filter-only,
  regex, and LLM routes fire correctly for *search* inputs.
- ✅ **Gap proven empirically:** the same trace shows `diagnose`, `lookup`, and
  `meta` inputs are mis-handled *because no L0 router exists* — they only get a
  deterministic decision once (wrongly) inside `run_search`. This is the precise
  failure L0 removes.

---

## 7. Recommendation (recalibrated for the DeepSeek agent)

Since the agent is a capable tool-caller, this is **not** a rescue mission. Do the
cheap, no-downside wins now; *measure* before adding anything that can misclassify.

### ✅ Do now — safe wins, no new failure modes
1. **`meta` short-circuit.** Detect pure chitchat/thanks/"what can you do" with a
   tiny rule set → reply from a template, never invoke the agent. Pure cost/latency win.
2. **Confirm the chip-only bypass is wired at the UI level**, so structured-only
   searches hit `/search` filter_only and never spin up the agent (the pipeline
   already supports it via `_has_filter_values`).
3. **L3 auto-recovery.** When a search tool returns empty/low-score, fold
   `explain_poor_results` signals + relax suggestions into the agent's context
   *before* it answers — don't depend on the prompt remembering to. This kills the
   biggest hallucination source (unhandled empty results) regardless of model.
4. **Keep L2 as-is.** The planner, retrieval, scoring, and confidence gate are the
   reproducible heart — the agent should never reimplement any of it.

### 🔬 Measure first — only build if the data justifies it
5. **Instrument mis-routing in Langfuse.** You already emit `agent.tool.{name}` spans.
   Sample real sessions and label: did the agent pick the right tool for the message?
   If wrong-tool rate is low (say <5%), **stop here** — a classifier would add risk
   for little gain. If it's high on specific buckets (diagnose/lookup/meta), proceed.
6. **Targeted tool-scoping (not a full router).** If step 5 shows a specific bucket
   mis-routes, scope *only that bucket's* tools via PydanticAI `prepare_tools` —
   e.g. when an active search exists and the message reads as a complaint, hide
   `run_search` so the agent can't re-search instead of diagnosing. Surgical, not global.

### 🤔 Probably skip unless scaling forces it
7. A full upstream intent classifier over all 22 tools. With a capable agent + the
   existing 15-example prompt, the classifier's own misclassification risk likely
   outweighs the benefit. Revisit only if you downgrade to a cheaper/weaker agent
   model to cut cost, which *re-introduces* the original Groq-era brittleness.

---

## Sources

- [Agentic RAG: Developer Guide to Smarter Retrieval (2026) — Future AGI](https://futureagi.com/blog/agentic-rag-systems-2025/)
- [Building Hierarchical Agentic RAG Systems — InfoQ](https://www.infoq.com/articles/building-hierarchical-agentic-rag-systems/)
- [Enhancing Intent Classification and Error Handling in Agentic LLM Applications — A. Murga](https://medium.com/@mr.murga/enhancing-intent-classification-and-error-handling-in-agentic-llm-applications-df2917d0a3cc)
- [LLM Function-Calling Pitfalls Nobody Mentions — Codastra](https://medium.com/@2nick2patel2/llm-function-calling-pitfalls-nobody-mentions-a0a0575888b1)
- [Query Routing for Homogeneous Tools (RAG) — arXiv 2406.12429](https://arxiv.org/pdf/2406.12429)
- [Route Before Retrieve — OpenReview](https://openreview.net/forum?id=N1E7rFZJGH)
