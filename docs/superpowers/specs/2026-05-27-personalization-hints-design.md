# Personalization Hints — Design

**Date:** 2026-05-27
**Status:** Approved for planning

## Problem

The recruiter search planner currently has no memory of an individual recruiter's taste. Two recruiters issuing the same query "senior backend engineer" get identical results, even if one consistently saves startup builders and the other consistently saves enterprise architects. The existing `recruiter_preferences` table only stores numeric ranking weights — it cannot capture qualitative preferences like "this user likes 0→1 builders".

We want the LLM planner to:

1. **Notice** when the current query implies something about the user's taste.
2. **Offer** to remember that as a soft preference for future searches.
3. **Apply** accepted preferences to future searches as *soft hints only* — never as hard filters that exclude candidates.

And we want the user to be able to **freely edit** their saved hints in a settings page.

## Non-goals

- Auto-mining preferences from impressions / outcomes history (separate feature; can be added later as a new trigger source).
- Per-search ephemeral preferences (this design covers persistent only).
- Sharing hints across recruiters.
- A/B testing whether hints improve outcomes.

## Safety contract

**Hints may only influence soft fields. They must never become hard filters.**

Concretely, a hint may add to:
- `should.skills`, `should.themes`, `should.locations`, `should.roles`
- `lexical_terms`, `semantic_query`, `hyde_profile`

A hint may **never** add to:
- `must.skills`, `must.country`, `must.city`, `must.min_years_exp`, `must.max_years_exp`, `must.applied_role`, `must.min_salary`, `must.max_salary`, `must.status`
- `must_not.*` of any kind

If the current query directly contradicts a hint, the current query wins.

This contract is enforced by two layers:

1. **Prompt-level instruction** in the planner system prompt (primary).
2. **Existing hallucination-demoter** in the validator ([pipeline/validator.py:97-108](pipeline/validator.py#L97-L108)) which already moves any `must.skill` not present in the input down to `should.skills`. This naturally catches the most likely leak path (a skill the LLM extracts from a hint and puts in `must`).

`must.country`, `must.city`, and `must_not.companies` are guarded only by the prompt. We accept this risk for v1; if real-world misbehavior emerges, a baseline-diff validator pass can be added.

## Data model

Extend `recruiter_preferences` ([db/migrations/004_impressions.sql:40-47](db/migrations/004_impressions.sql#L40-L47)) with two columns:

```sql
ALTER TABLE recruiter_preferences
  ADD COLUMN personalization_enabled BOOLEAN DEFAULT TRUE,
  ADD COLUMN personalization_hints   JSONB    DEFAULT '[]'::jsonb;
```

`personalization_hints` is an ordered JSON array. Each element:

```json
{
  "text":       "user often wants candidates with startup experience",
  "source":     "suggested",
  "created_at": "2026-05-27T12:00:00Z"
}
```

- `text` (string, required): the natural-language preference, ≤ 200 chars after sanitization.
- `source` ("suggested" | "manual"): provenance — accepted from LLM banner vs typed in settings.
- `created_at` (ISO timestamp): for display + ordering.

Order is preserved. Most recently added first.

**Limits (enforced server-side at write time):**
- Per-hint text: ≤ 200 chars (POST returns 400 with a clear error if exceeded).
- Per-user hint count: ≤ 20 (POST returns 400 if at cap — user must delete one first).
- Total injected payload (sum of hint texts) is bounded by `20 × 200 = 4000` chars in the worst case, which is safe to include in every planner prompt.

## Planner JSON change

Add one new optional top-level field to `CanonicalSearchSpec` ([pipeline/spec.py:48-68](pipeline/spec.py#L48-L68)):

```python
personalization_offer: Optional[dict] = None
# Shape: {"hint": str, "question": str} or None
```

Example value emitted by the LLM:

```json
"personalization_offer": {
  "hint":     "It looks like you tend to look for 0→1 builders rather than maintainers.",
  "question": "Want me to remember this for future searches?"
}
```

- `None` (the default) most of the time.
- LLM is instructed to emit one at most ~1 in 5 searches — only when it spots something genuinely worth remembering.
- LLM should skip emitting if a sufficiently similar hint is already in the user's saved hints (the saved hints are visible to it in the prompt).

The field is added to:
- `CanonicalSearchSpec` dataclass
- `_spec_from_dict` reconstruction in [pipeline/planner.py:289-335](pipeline/planner.py#L289-L335)
- The validator's `validate_spec` parser
- The planner output JSON schema in the system prompt
- `SearchResponse` Pydantic model in `api/main.py`

## System prompt changes

Two additions to `PLANNER_SYSTEM_PROMPT` in [pipeline/prompts.py](pipeline/prompts.py):

### A. Output schema — add the new field

```
"personalization_offer": null
```

with a new rule explaining when to set it:

```
PERSONALIZATION_OFFER
  - null by default. Only set this when the CURRENT query reveals something
    notable about the user's taste that would be useful to remember for
    FUTURE searches.
  - Examples that warrant an offer: queries that repeatedly emphasize a
    company stage ("startup", "scale-up"), work style ("hands-on", "deep
    research"), location pattern, or role-shape ("0→1 builder",
    "platform-not-product").
  - Do NOT offer if the saved hints (see ABOUT THIS USER block, if present)
    already cover the observation.
  - Do NOT offer on every search. Be selective — roughly 1 in 5 searches
    should produce an offer.
  - Shape:
    {"hint": "<one-sentence observation>", "question": "<one-sentence yes/no>"}
```

### B. Hints injection block (added dynamically before the call)

When the search request includes a `recruiter_id`, the planner loads the user's preferences. If `personalization_enabled = true` and hints exist, prepend this block to the system prompt:

```
=== ABOUT THIS USER (soft hints, NOT requirements) ===
- user often wants candidates with startup experience
- prefers Bangalore-based candidates
- likes builders who shipped 0→1 products

RULES for these hints:
1. They may only influence should.skills, should.themes, should.locations,
   should.roles, lexical_terms, semantic_query, or hyde_profile.
2. They MUST NEVER be added to must.*, must_not.*, or any hard-filter field.
   A hint is a soft bias, not a requirement.
3. If the current query contradicts a hint, the current query wins —
   ignore the hint for this search.
4. Do not re-offer a hint that is already in this list.
```

When hints are empty or `personalization_enabled = false`, the block is omitted entirely — the prompt is identical to today's.

**Wiring (where the block is prepended):**

The block is built in `pipeline/planner.py` (a new helper `_build_hints_block(hints) -> str`) and threaded through to the provider wrappers. Responsibilities split:

- **Pipeline (`pipeline/search.py`)** loads hints from the DB via a new method `_load_recruiter_hints(recruiter_id) → (enabled: bool, hints: list[dict])`, parallel to the existing `_load_recruiter_prefs` ([pipeline/search.py:440-452](pipeline/search.py#L440-L452)). Returns `(True, [])` on miss. Result is injected into `cfg` as `cfg["personalization_enabled"]` and `cfg["personalization_hints"]` before calling `plan()`.
- **`plan()`** reads those two keys from `cfg` (defaulting to `True` / `[]`), calls `_build_hints_block(hints) → str` which returns either the formatted block or `""`, and passes it to `_call_llm` as a new `system_prompt_prefix: str = ""` argument. `plan()` stays DB-agnostic.
- **`_call_llm`, `_call_openai`, `_call_gemini`, `_call_groq`** each accept the new arg and prepend it (with a trailing `\n\n`) to `PLANNER_SYSTEM_PROMPT` when building the request. `PLANNER_SYSTEM_PROMPT` itself remains a constant — no template engine, no mutation.
- For Gemini, which concatenates system + user into one prompt, the prefix is prepended first.

For v1, `_load_recruiter_prefs` and `_load_recruiter_hints` are two separate queries. Merging into one SELECT is a follow-up optimization.

## API surface

**Search response gains one optional field:**

```python
class SearchResponse(BaseModel):
    ...
    personalization_offer: dict | None = None  # {hint, question} | None
```

The frontend renders a banner above results when this is non-null.

**Four new endpoints under `/api/recruiter/{recruiter_id}/`:**

```
GET    /api/recruiter/{id}/personalization
       → returns: {"enabled": bool, "hints": [{text, source, created_at}, ...]}
       Used by the settings page to render the personalization card.

POST   /api/recruiter/{id}/hints
       body: {"text": "...", "source": "manual" | "suggested"}
       → adds a hint. `source` defaults to "manual" if omitted.
         Settings page sends "manual"; the search banner's
         [Yes, remember] button sends "suggested".
       returns: updated hints array.
       errors: 400 if text empty / > 200 chars / hint count already at 20.

DELETE /api/recruiter/{id}/hints/{index}
       → removes the hint at that array index.
       returns: updated hints array.
       errors: 404 if index out of range.

PATCH  /api/recruiter/{id}/personalization
       body: {"enabled": true | false}
       → toggles personalization_enabled.
       returns: {enabled: bool}
```

All four endpoints upsert the `recruiter_preferences` row if missing. For v1, no auth check beyond what the existing recruiter routes do.

## UI changes

### Search results page

When `personalization_offer` is non-null in the response, render a small banner above the results list:

```
┌──────────────────────────────────────────────────────────────┐
│ 💡 It looks like you tend to look for 0→1 builders.          │
│    Want me to remember this for future searches?             │
│                                  [ Yes, remember ] [ Not now ]│
└──────────────────────────────────────────────────────────────┘
```

- **[Yes, remember]** → POST the hint with `source: "suggested"`, then dismiss the banner.
- **[Not now]** → just dismiss. No persistence; the same offer may surface again on a different query.

### Settings page

New "Personalization" card containing:

- **Master toggle:** "Use my personalization hints" (on/off). Calls the PATCH endpoint.
- **Hints list:** each row is `{text}  [small icon: 💡 if suggested, ✏️ if manual]  [×]`. Click × to delete.
- **Add input:** text field + "Add" button. POSTs with `source: "manual"`.

## Migration

New file `db/migrations/006_personalization_hints.sql`:

```sql
ALTER TABLE recruiter_preferences
  ADD COLUMN IF NOT EXISTS personalization_enabled BOOLEAN DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS personalization_hints   JSONB   DEFAULT '[]'::jsonb;
```

Idempotent. Safe to re-run.

## Tests

New test files:

- `tests/test_personalization_planner.py`
  - `personalization_offer` round-trips through `validate_spec` and `_spec_from_dict`
  - `_build_hints_block(hints)` returns expected prefix when hints exist; returns `""` when empty
  - `_build_hints_block` is NOT called (or returns `""`) when `personalization_enabled = false`
  - **Safety-contract test (skills leak):** feed a raw LLM dict where `must.skills = ["fintech"]` and the original input is `"python developer"`, and the saved hints include `"user likes fintech candidates"`. Assert `validate_spec` demotes `fintech` to `should.skills` (validator demoter already covers this — regression test against this exact scenario)
  - **Safety-contract test (prompt rule):** assert the rendered system prompt + hints block contains the literal phrase `"MUST NEVER be added to must.*"` — i.e. the rule the LLM is supposed to follow is actually in the prompt
  - **Cache key test:** two calls with identical inputs but different hints arrays produce different cache keys; identical inputs with empty hints produce the same key as today
  - **Offer-not-cached test:** after `plan()` with a spec containing `personalization_offer`, the cached dict (`_cache.get(ck)`) does NOT contain that field

- `tests/test_personalization_api.py`
  - POST `/hints` with `source=manual` adds an entry tagged manual
  - POST `/hints` with `source=suggested` adds an entry tagged suggested
  - POST `/hints` with text > 200 chars returns 400
  - POST `/hints` when user already has 20 hints returns 400
  - POST `/hints` strips control chars / prompt-injection patterns via `sanitize_input` (assert the stored text equals the sanitized form, not the raw input)
  - DELETE `/hints/{idx}` removes the right entry; out-of-range returns 404
  - PATCH `/personalization` toggles the flag and persists it
  - GET `/hints` returns the array in insert order (most recent first)
  - All four endpoints upsert the `recruiter_preferences` row if missing

- Extend `tests/test_dropped_items.py` to confirm hints injected into must.skills still get demoted (regression coverage for the safety net).

## Cache key

Current key ([pipeline/planner.py:64-68](pipeline/planner.py#L64-L68)) is:

```
plan_key(input_lower, filters_str, f"{route}|{provider}|{model}")
```

New rule: when (and only when) hints are non-empty AND `personalization_enabled = true`, append a hints-hash component to the route portion:

```python
hints_tag = ""
if enabled and hints:
    payload = json.dumps([h["text"] for h in hints], sort_keys=True)
    hints_tag = "|h:" + hashlib.sha1(payload.encode()).hexdigest()[:12]
route_with_llm = f"{route}|{provider}|{model}{hints_tag}"
```

Rationale:
- **No hints → key unchanged** → 100% cache-hit-rate preservation for the no-personalization majority of traffic.
- **Hints present → key partitioned by hint set** → user A's plans aren't served to user B with different hints, and toggling personalization off restores the original key.
- **Hash only the texts (sorted)** — not `created_at` or `source`, since those don't affect the prompt.

## Cached plan must not replay the offer

A cached plan dict round-tripped through `_spec_from_dict` ([pipeline/planner.py:289-335](pipeline/planner.py#L289-L335)) currently drops `dropped_items` and `skill_weights`. Same pattern applies here:

- `personalization_offer` is **stripped before writing to cache** (`spec.to_dict()` followed by `del d["personalization_offer"]`), so cached hits never re-show the same offer.
- This is intentional, not a bug — offers are first-search-only signals. Without this, the user would see the same "want to remember this?" banner every cache hit and either get spammed or sign up for the same hint twice.
- `_spec_from_dict` doesn't restore `personalization_offer` (it stays `None` on cache hit). New behavior matches existing pattern for ephemeral fields.

## Fallback path

`pipeline/planner_fallback.py` never sets `personalization_offer` — it constructs a spec from scratch with no LLM in the loop. Specifically:

- `personalization_offer` defaults to `None` on the dataclass; fallback leaves it.
- Saved hints are NOT injected into the fallback path (it has no LLM prompt). Personalization is a soft-bias feature that only meaningfully works through the LLM. Documented limitation, not a bug.

## Sanitization

Hint text is sanitized at **write time** in the POST handler before being stored:

- Run through `pipeline.sanitize.sanitize_input` to strip control chars, collapse whitespace, and neutralize obvious prompt-injection patterns (e.g. fenced blocks).
- Reject if the post-sanitization text is empty or > 200 chars.

We do NOT re-sanitize at read time — DB content is trusted because it was sanitized on the way in. (If we ever decide otherwise, a single read-time pass is cheap to add.)

## Interaction with explicit_filters

`explicit_filters` (caller-supplied chips / form values) currently override LLM output unconditionally ([pipeline/validator.py:179-216](pipeline/validator.py#L179-L216)). That behavior is unchanged: a hint-influenced `should.skills` cannot beat an explicit `must.skills` filter, and an explicit filter always wins over any soft hint. This is the right precedence — the user's *current* explicit choice trumps a *remembered* soft preference.

## Open implementation notes

- If the `recruiter_preferences` row doesn't exist for a recruiter, POST/PATCH endpoints upsert it (matches existing semantics).
- The new spec field `personalization_offer` must be added to `_spec_from_dict` reconstruction too (even though cached hits will always see `None`), so cache-disabled and test paths work uniformly.

## Out of scope (explicit)

- Auto-mining hints from impressions/outcomes (separate trigger — could be a follow-up feature)
- Per-search ephemeral preferences (only persistent in v1)
- Multi-recruiter sharing
- Frequency setting in UI (the prompt rule "~1 in 5 searches" is the only knob in v1)
- Hint editing in-place — v1 flow is delete + re-add
- "Never offer this again" button on the banner — v1 has [Yes, remember] / [Not now] only
- Applying hints in the fallback planner path (no LLM, no soft-bias mechanism)
- Auth: endpoints inherit whatever auth the existing recruiter routes have, no additional checks added
