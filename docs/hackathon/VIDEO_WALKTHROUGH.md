# Rough Video Walkthrough

This is a five-to-six-minute recording plan. It starts with the deterministic
search contract, then shows why the realtime agent harness is more than a voice
wrapper around the same endpoint.

## Before recording

1. Start the application and wait for model warm-up to finish.
2. Open these tabs in order:
   - `http://127.0.0.1:8010/docs#/default/search_search_post`
   - `http://127.0.0.1:8010/talent`
   - the repository README or terminal for the final verification shot.
3. Run `scripts/demo_search_endpoint.sh` once before recording. This warms the
   embedding model and confirms the finance records still exist.
4. Start a new Talent Atlas session and verify microphone permission.
5. Keep the **Activity** drawer closed until the evidence section of the demo.

## 0:00-0:25 - State the problem

Say:

> Recruiters search over two kinds of information at once. There are structured
> facts such as skills, location and years of experience, and unstructured
> evidence inside resumes. I transformed public recruitment datasets into a
> candidate, document and chunk schema, then built a hybrid RAG pipeline over
> PostgreSQL and pgvector. The goal is not merely to produce an answer, but to
> show exactly why every candidate was retrieved.

On screen: briefly show the corpus count from `/health` or the Talent UI's
demonstration-corpus label. Say that this is transformed public demonstration
data, not production hiring data.

## 0:25-1:20 - Show the direct search contract

Open the `POST /search` operation in Swagger and select **Try it out**. Paste the
request from `scripts/demo_search_endpoint.sh`, or run the script in a terminal.

Say:

> This JSON is the stable contract beneath the application. Required skills and
> minimum experience become hard eligibility rules. The `should` block contains
> preferences that influence ranking without silently excluding a candidate.
> `no-llm` means the request is already structured, so the backend does not pay
> for another planning call. The other presets trade latency for planning and
> reranking: `fast`, `quality`, and `agent-quality`.

Pause on these fields:

- `skills_match: "and"`: all three canonical skills are required;
- `min_years_exp: 5`: a structured SQL constraint;
- `should`: optional evidence and ranking preferences;
- `keyword_policy: "auto"`: the engine decides whether lexical retrieval is
  useful and still applies a timeout;
- `include_rank_explanation: true`: the response carries the score breakdown
  and retrieved evidence.

Execute the request. The tested response should show 15 eligible profiles, with
the displayed top eight including `Finance Candidate 4830`,
`Finance Candidate 4841`, and `Accountant Candidate 36`. Exact order can change
when equal-score candidates tie, so narrate the evidence rather than promising
one fixed ordering.

Say:

> The request is validated and normalized, then SQL eligibility, pgvector
> semantic retrieval, PostgreSQL keyword retrieval, and exact-skill retrieval
> fan out in parallel. Reciprocal Rank Fusion combines positions, an optional
> cross-encoder reviews a bounded pool, and the final response includes the
> candidates, supporting chunks, retrieval paths, timings and the explanation
> built from the score that actually sorted the list.

## 1:20-1:45 - Explain the agent boundary

Switch to `/talent`.

Say:

> Gemini Live does not replace that pipeline. It receives an allow-listed tool
> manifest: each tool has a name, description, generated JSON Schema and
> blocking behavior. The model can propose a tool and JSON arguments, but the
> application validates them, executes trusted callbacks and returns bounded
> evidence. The voice model never receives the database connection or Python
> functions.

## 1:45-2:25 - Begin a grounded voice search

Say clearly:

> Find finance candidates with accounting, auditing and financial reporting,
> with at least five years of experience.

Expected screen behavior:

- a `list_skills` call may resolve canonical vocabulary;
- `search_candidates` appears before any candidate claim;
- the agent gives a short acknowledgement while retrieval runs;
- ranked candidate cards appear from the same JSON result contract.

If the model asks an unnecessary clarification, repeat the sentence once and
add: “Those are hard requirements; search now.”

## 2:25-3:05 - Demonstrate a real interruption

While the agent is speaking about the result, interrupt it with:

> Actually, restrict that to India.

Do not wait for it to finish. After its acknowledgement, interrupt again:

> Wait, remove the entire location restriction, but keep the same skills and
> minimum experience.

What to point out:

- speech stops immediately, but a bare barge-in does not discard valid work;
- `interrupt_search`, not `cancel_current_action`, handles the correction;
- the second instruction clears location while preserving the other slots;
- each change becomes a child revision in the same conversation;
- candidate cards update only after the authoritative result arrives.

Say:

> This is the part I built around Gemini Live. The harness compares immutable
> search revisions. It preserves unaffected state and cancels or restarts only
> retrieval branches whose fingerprints changed, instead of throwing the whole
> task away.

## 3:05-3:40 - Force the slow evidence path

Say:

> Compare the top two candidates using their retrieved evidence. Tell me which
> required skills each one has, what preference is missing, and recommend one.

Expected behavior: `compare_candidates` receives the real IDs from the active
ranked list. A tool card must appear before the recommendation. The answer may
name resume evidence, but must not invent qualifications or use placeholder IDs.

Follow with:

> Put that comparison into three short bullets.

Expected behavior: `format_current_answer` reuses existing evidence and does not
perform another retrieval.

## 3:40-4:15 - Show a safe state-changing tool

Say:

> Shortlist the stronger candidate.

Then correct it immediately:

> Actually, replace that selection with the other candidate.

Say:

> Search and comparison tools are read-only. Shortlisting is the only stored
> write in the realtime toolset. It has an idempotency key, an effect scope and
> an audit log, so retries cannot add the same person twice and a correction can
> supersede an older in-flight write.

## 4:15-5:05 - Open the Activity evidence graph

Open **Activity** and select the latest revision. Click these nodes in order:

1. **Validated query**: show the retained skills/experience and cleared
   location.
2. **SQL eligibility**: show the hard-filter count.
3. **Vector meaning**, **BM25 keywords**, and **Exact skills**: show the three
   independent retrieval paths and their bounded results.
4. **Reciprocal rank fusion**: show the joined candidate IDs.
5. **Cross-encoder review** and **Evidence grounding**: show the final bounded
   review and candidate evidence.

Switch between **Input**, **Results**, **Evidence**, and **Raw event** for one
node.

Say:

> I deliberately do not expose private chain-of-thought. What I expose is more
> useful for auditing: the model's selected tool, validated arguments, current
> plan, changed fields, branch decisions, timings, returned evidence and final
> grounded answer. This lets a reviewer verify what actually happened rather
> than trusting a generated explanation of hidden reasoning.

## 5:05-5:30 - Multimodal and reliability close

If time allows, upload a screenshot of a job description and say:

> Use this role image, but ignore the degree requirement. Treat explicitly
> required skills as mandatory and everything else as preferred.

Point out that `use_role_image` reports requirements the database cannot enforce
instead of pretending they were applied.

Finish with:

> The result is a full-duplex recruiting agent with session-scoped memory,
> selective interruption recovery, typed and guarded tools, hybrid RAG,
> multimodal input, grounded output and an evidence graph. The control harness
> is covered by 670 tests, a 12-scenario interruption benchmark and a separate
> pass/fail agent evaluation.

## What not to claim

- Do not call the Activity graph “the model's chain-of-thought.” Call it the
  auditable execution or decision trace.
- Do not describe transformed public records as production data.
- Do not promise a fixed candidate order when candidates have similar scores.
- Do not say an India-only result is an exact match if the UI reports automatic
  relaxation or broader alternatives.
- Do not claim the hidden Samsung evaluator has been run.

## Backup shots if voice behaves unpredictably

- Run `scripts/demo_search_endpoint.sh` to prove the retrieval contract.
- Open `reports/realtime_state_cards_preview.html` to show interruption state
  cards generated from real handlers.
- Run `python scripts/benchmark_realtime_interruptions.py` and show `12/12`.
- Run `python scripts/evaluate_realtime_agent.py` and show the pass/fail gate.
- Use text input in the same Talent UI; it exercises the same session, tools,
  revisions and evidence graph even if the microphone transport is noisy.
