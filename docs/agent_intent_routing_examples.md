# Agent Intent-Routing Examples (few-shot)

Drop-in block for `build_system_prompt` in `pipeline/agent.py`. This is **soft
routing**: it biases the agent toward the right tool without removing any tool from
its reach — so novel/multi-intent messages still work (nothing is hard-blocked).

**Design rules**
- These teach *which intent → which tool*. The existing "## Examples" block teaches
  *how to use* each tool — keep both, they're complementary.
- The value is in the **contrastive pairs** (two similar-looking messages, different
  tools). Don't pad with redundant same-intent examples.
- Put this block high in the prompt, right after "How to Engage."
- Routing ≠ recovery. Empty/garbage tool results still need the auto-recovery path.

---

## Intent Routing — pick the tool by what the recruiter actually wants

### 1. New search (a role/brief, no complaint about current results)
- "senior python engineers in Bengaluru" → `run_search`
- "find people who look strong for staff-level backend" → `run_search`
- "I need to fill a data role" *(vague)* → don't search yet; gather requirements, then `run_search`

### 2. Refine vs. new search (only after a search exists)
- "tighten to 7+ years" / "remote only" / "drop the salary cap" → `modify_and_search`
- "make Kubernetes optional" → `modify_and_search` (remove from required)
- "actually, let's look for data scientists instead" → `run_search` (substantially new role)
  > Rule: small tweak to the same brief → `modify_and_search`. New role/intent → `run_search`.

### 3. Diagnose vs. search (the "look off" trap)
- "these results look off" / "this seems wrong" / "not what I wanted" → `explain_poor_results`
- "why are these so bad?" / "this feels random" → `explain_poor_results`
- "find people who look good on paper for this role" → `run_search`
  > Rule: a complaint about the *current results* → diagnose. A new brief that merely
  > contains words like "look/good/strong" → search.

### 4. Lookup vs. search (named person vs. role)
- "where is Sarah Chen?" / "did Raj Patel show up?" / "is <name> in the system?" → `query_candidates_db`
- "I expected <name> to appear — why isn't she here?" → `query_candidates_db` then `get_candidate_detail`
- "where can I find Go engineers?" / "show me Go engineers" → `run_search`
  > Rule: a *named individual* → DB lookup. A *role/skill* → search.

### 5. Inspect / explain a candidate (results already on screen)
- "why is candidate X ranked #3 and not higher?" → `view_current_results` (read score_breakdown)
- "tell me more about the top match" → `get_candidate_detail`
- "compare the top two" → `compare_candidates`
- "which search was better, with Spark or without?" → `compare_iterations`

### 6. Analytics / aggregate (counting, not ranking)
- "how many python candidates do we have in each city?" → `load_candidate_pool` + `aggregate_pool`
- "what's our coverage in Berlin?" → `query_candidates_db` (COUNT)
- "who has BOTH machine learning and product experience?" → `query_candidates_db` (array `@>`)
- "who mentions 'Series B' in their resume?" → `keyword_search`
  > Rule: questions about *the database as a whole* (counts, existence, raw text) →
  > DB/keyword/pool tools, NOT `run_search` (which ranks for one query).

### 7. Job description paste
- (a long pasted JD) → `analyze_jd` to extract role + 3-5 skills + location, then `run_search`
  > Never paste the whole JD into `run_search`.

### 8. Candidate actions
- "draft an outreach email to X" → `draft_outreach`
- "interview questions for X" → `generate_interview_questions`
- "shortlist these / accept X / reject Y" → `update_shortlist`
- "send these to the main panel" → `push_to_main_panel`
- "save this search" → `save_search`; "export the shortlist" → `export_shortlist`

### 9. Memory / preferences (always confirm first)
- "always prefer candidates from startups" → ask to confirm, THEN `save_hint`
- (recruiter answers an observation question) "yes, remember that" → `confirm_observation`
  > Never `save_hint` unprompted.

### 10. Meta / chitchat (NO tool)
- "what can you do?" / "how does this work?" / "what filters can I use?" → explain in text, **no tool call**
- "thanks" / "perfect" / "great, ok" → brief acknowledgement, **no tool call**

### 11. Multi-intent (don't freeze — do the concrete ask, offer the rest)
- "the last results were weak — find me senior Go devs in Berlin instead"
  → `run_search` for the new brief now; add one line offering to diagnose the old one.
- "compare these two and draft outreach to the better one"
  → `compare_candidates` first, then `draft_outreach` for the stronger profile.
  > Rule: when a message carries two asks, execute the concrete actionable one, then
  > offer the secondary — never stall asking which one they meant.

---

## How to integrate
1. Paste the "Intent Routing" section into `build_system_prompt`, after "How to Engage."
2. Keep it as plain message→tool lines (cheap, high-contrast). Don't expand into prose.
3. Validate with a small eval set: ~3 messages per intent (including the boundary
   traps above) → assert the agent calls the expected tool. Check the
   `agent.tool.{name}` Langfuse spans to confirm.
