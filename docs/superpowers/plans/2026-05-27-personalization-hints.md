# Personalization Hints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the LLM planner emit an occasional "want me to remember this for future searches?" offer; persist accepted hints per-recruiter; inject them as soft hints into future planner prompts; never let them become hard filters.

**Architecture:** Spec gains an optional `personalization_offer` field the LLM may emit. A new pair of columns on `recruiter_preferences` stores accepted hints. Hints are loaded by the pipeline, threaded into `cfg`, formatted into an ABOUT-THIS-USER block by a small helper, and prepended to the planner system prompt. Validator demoter already prevents the most likely leak (must.skills). Cache key gets a hints-hash component only when hints exist.

**Tech Stack:** Python 3.11+, FastAPI, asyncpg, Pydantic v2, pytest + pytest-asyncio + FastAPI TestClient.

**Spec:** [docs/superpowers/specs/2026-05-27-personalization-hints-design.md](../specs/2026-05-27-personalization-hints-design.md)

---

## File Structure

**Create:**
- `db/migrations/006_personalization_hints.sql` — DDL adding 2 columns to `recruiter_preferences`
- `pipeline/hints.py` — `build_hints_block(hints) -> str` + `hints_cache_tag(hints, enabled) -> str`
- `tests/test_personalization_planner.py` — planner-side tests
- `tests/test_personalization_api.py` — endpoint tests

**Modify:**
- `pipeline/spec.py` — add `personalization_offer: Optional[dict]` field
- `pipeline/validator.py` — parse `personalization_offer` from raw dict
- `pipeline/planner.py` — `system_prompt_prefix` arg, cfg-driven hints, cache key change, strip offer before caching, restore offer in `_spec_from_dict`
- `pipeline/prompts.py` — add `personalization_offer` to output schema + new rule
- `pipeline/search.py` — `_load_recruiter_hints` method, inject into cfg
- `api/main.py` — `SearchResponse.personalization_offer`, 4 new recruiter endpoints
- `api/static/admin.html` — offer banner + accept/dismiss buttons
- `api/static/settings.html` — Personalization card (toggle, list, add form)
- `tests/test_dropped_items.py` — regression: hints-derived must.skill still demoted

---

## Task 1: Migration — add columns to `recruiter_preferences`

**Files:**
- Create: `db/migrations/006_personalization_hints.sql`

- [ ] **Step 1: Create the migration file**

```sql
-- 006_personalization_hints.sql
-- Per-recruiter personalization hints: free-form natural-language preferences
-- the planner injects as soft hints into future searches.

ALTER TABLE recruiter_preferences
  ADD COLUMN IF NOT EXISTS personalization_enabled BOOLEAN DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS personalization_hints   JSONB   DEFAULT '[]'::jsonb;
```

- [ ] **Step 2: Apply the migration to the local DB**

Run:
```bash
psql "$DATABASE_URL" -f db/migrations/006_personalization_hints.sql
```
Expected: `ALTER TABLE` printed twice (or once with both columns). Re-running is safe.

- [ ] **Step 3: Verify columns exist**

Run:
```bash
psql "$DATABASE_URL" -c "\d recruiter_preferences" | grep personalization
```
Expected: two lines listing `personalization_enabled` (boolean, default true) and `personalization_hints` (jsonb, default '[]'::jsonb).

- [ ] **Step 4: Commit**

```bash
git add db/migrations/006_personalization_hints.sql
git commit -m "feat(db): add personalization columns to recruiter_preferences"
```

---

## Task 2: Add `personalization_offer` field to `CanonicalSearchSpec`

**Files:**
- Modify: `pipeline/spec.py`
- Test: `tests/test_personalization_planner.py`

- [ ] **Step 1: Create the test file with the failing test**

Create `tests/test_personalization_planner.py`:
```python
"""Planner-side tests for personalization hints (soft preferences injected
into the LLM prompt + an optional offer field on the spec)."""

from pipeline.spec import CanonicalSearchSpec


def test_spec_defaults_personalization_offer_to_none():
    spec = CanonicalSearchSpec(input_type="query", intent="candidate_search")
    assert spec.personalization_offer is None


def test_spec_to_dict_includes_personalization_offer():
    spec = CanonicalSearchSpec(
        input_type="query", intent="candidate_search",
        personalization_offer={"hint": "user likes builders", "question": "Remember?"},
    )
    d = spec.to_dict()
    assert d["personalization_offer"] == {
        "hint": "user likes builders",
        "question": "Remember?",
    }
```

- [ ] **Step 2: Run the test to confirm it fails**

Run: `pytest tests/test_personalization_planner.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'personalization_offer'` (or TypeError on the kwarg).

- [ ] **Step 3: Add the field**

In `pipeline/spec.py`, inside the `CanonicalSearchSpec` dataclass, after `dropped_items: list[dict]`:
```python
    # Optional "want me to remember this?" offer the LLM may emit. Shape:
    # {"hint": "<observation>", "question": "<yes/no question>"} or None.
    personalization_offer: Optional[dict] = None
```

- [ ] **Step 4: Run the test to confirm it passes**

Run: `pytest tests/test_personalization_planner.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add pipeline/spec.py tests/test_personalization_planner.py
git commit -m "feat(spec): add personalization_offer field to CanonicalSearchSpec"
```

---

## Task 3: Validator parses `personalization_offer`

**Files:**
- Modify: `pipeline/validator.py`
- Test: `tests/test_personalization_planner.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_personalization_planner.py`:
```python
from pipeline.validator import validate_spec


def _raw(extra_top=None):
    base = {
        "input_type": "query",
        "intent": "candidate_search",
        "must": {"skills": [], "country": None, "city": None,
                 "min_years_exp": None, "max_years_exp": None,
                 "applied_role": None, "min_salary": None, "max_salary": None,
                 "status": ["active"]},
        "should":   {"skills": [], "themes": [], "locations": [], "roles": []},
        "must_not": {"skills": [], "status": [], "companies": []},
        "semantic_query": "x",
        "hyde_profile": None,
        "lexical_terms": [],
        "search_targets": ["candidate_profile"],
        "confidence": 0.8,
        "clarify": None,
    }
    if extra_top:
        base.update(extra_top)
    return base


def test_validator_parses_personalization_offer():
    raw = _raw({"personalization_offer": {
        "hint": "user likes startup builders",
        "question": "Remember?"}})
    spec = validate_spec(raw, original_input="python engineer")
    assert spec.personalization_offer == {
        "hint": "user likes startup builders",
        "question": "Remember?",
    }


def test_validator_handles_missing_personalization_offer():
    spec = validate_spec(_raw(), original_input="x")
    assert spec.personalization_offer is None


def test_validator_drops_malformed_personalization_offer():
    raw = _raw({"personalization_offer": "not a dict"})
    spec = validate_spec(raw, original_input="x")
    assert spec.personalization_offer is None
```

- [ ] **Step 2: Run — confirm failures**

Run: `pytest tests/test_personalization_planner.py -v`
Expected: 3 new tests FAIL (offer attribute not set by validator).

- [ ] **Step 3: Parse the field in the validator**

In `pipeline/validator.py`, inside `validate_spec`, after the `clarify = _clean_str(...)` line and before the `# Skill hallucination check` comment, add:
```python
    personalization_offer = _coerce_personalization_offer(raw.get("personalization_offer"))
```

Then pass it into the spec constructor — change:
```python
    spec = CanonicalSearchSpec(
        ...
        dropped_items=dropped,
    )
```
to:
```python
    spec = CanonicalSearchSpec(
        ...
        dropped_items=dropped,
        personalization_offer=personalization_offer,
    )
```

Then add the helper near the other `_coerce_*` helpers at the bottom of the file:
```python
def _coerce_personalization_offer(v: Any) -> dict | None:
    """Accept {hint: str, question: str}; reject anything else."""
    if not isinstance(v, dict):
        return None
    hint     = _clean_str(v.get("hint"))
    question = _clean_str(v.get("question"))
    if not hint or not question:
        return None
    return {"hint": hint[:300], "question": question[:200]}
```

- [ ] **Step 4: Run — confirm passes**

Run: `pytest tests/test_personalization_planner.py -v`
Expected: all 5 tests pass.

- [ ] **Step 5: Commit**

```bash
git add pipeline/validator.py tests/test_personalization_planner.py
git commit -m "feat(validator): parse personalization_offer from planner output"
```

---

## Task 4: Restore `personalization_offer` in `_spec_from_dict`

**Files:**
- Modify: `pipeline/planner.py`
- Test: `tests/test_personalization_planner.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_personalization_planner.py`:
```python
from pipeline.planner import _spec_from_dict


def test_spec_from_dict_restores_personalization_offer():
    d = _raw({"personalization_offer": {"hint": "h", "question": "q?"}})
    spec = _spec_from_dict(d)
    assert spec.personalization_offer == {"hint": "h", "question": "q?"}


def test_spec_from_dict_handles_missing_personalization_offer():
    spec = _spec_from_dict(_raw())
    assert spec.personalization_offer is None
```

- [ ] **Step 2: Run — confirm failures**

Run: `pytest tests/test_personalization_planner.py::test_spec_from_dict_restores_personalization_offer tests/test_personalization_planner.py::test_spec_from_dict_handles_missing_personalization_offer -v`
Expected: FAIL (field not restored).

- [ ] **Step 3: Add reconstruction**

In `pipeline/planner.py` `_spec_from_dict`, in the `CanonicalSearchSpec(...)` constructor at the bottom, before the closing paren, after `planner_error  = d.get("planner_error"),`, add:
```python
        personalization_offer = d.get("personalization_offer"),
```

- [ ] **Step 4: Run — confirm passes**

Run: `pytest tests/test_personalization_planner.py -v`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add pipeline/planner.py tests/test_personalization_planner.py
git commit -m "feat(planner): restore personalization_offer in _spec_from_dict"
```

---

## Task 5: Create `pipeline/hints.py` helper module

**Files:**
- Create: `pipeline/hints.py`
- Test: `tests/test_personalization_planner.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_personalization_planner.py`:
```python
from pipeline.hints import build_hints_block, hints_cache_tag


def test_build_hints_block_returns_empty_when_disabled():
    assert build_hints_block([{"text": "x"}], enabled=False) == ""


def test_build_hints_block_returns_empty_when_no_hints():
    assert build_hints_block([], enabled=True) == ""
    assert build_hints_block(None, enabled=True) == ""


def test_build_hints_block_formats_when_enabled_and_present():
    hints = [{"text": "user often wants startup experience"},
             {"text": "prefers Bangalore-based candidates"}]
    block = build_hints_block(hints, enabled=True)
    assert "ABOUT THIS USER" in block
    assert "user often wants startup experience" in block
    assert "prefers Bangalore-based candidates" in block
    # Safety rule must be present verbatim — the LLM is supposed to obey it.
    assert "MUST NEVER be added to must.*" in block
    # Block ends with a blank line so it concatenates cleanly to the
    # system prompt.
    assert block.endswith("\n\n")


def test_hints_cache_tag_empty_when_disabled_or_empty():
    assert hints_cache_tag([], enabled=True) == ""
    assert hints_cache_tag([{"text": "x"}], enabled=False) == ""


def test_hints_cache_tag_stable_and_sensitive_to_text_only():
    a = [{"text": "alpha", "source": "manual", "created_at": "t1"}]
    b = [{"text": "alpha", "source": "suggested", "created_at": "t2"}]
    c = [{"text": "beta", "source": "manual", "created_at": "t1"}]
    # Different metadata, same text → same tag.
    assert hints_cache_tag(a, enabled=True) == hints_cache_tag(b, enabled=True)
    # Different text → different tag.
    assert hints_cache_tag(a, enabled=True) != hints_cache_tag(c, enabled=True)
    # Order-independent.
    two_one  = [{"text": "alpha"}, {"text": "beta"}]
    one_two  = [{"text": "beta"},  {"text": "alpha"}]
    assert hints_cache_tag(two_one, enabled=True) == hints_cache_tag(one_two, enabled=True)
    # Format is "|h:<12 hex chars>"
    assert hints_cache_tag(a, enabled=True).startswith("|h:")
    assert len(hints_cache_tag(a, enabled=True)) == 3 + 12
```

- [ ] **Step 2: Run — confirm failures**

Run: `pytest tests/test_personalization_planner.py -v -k "hints_block or cache_tag"`
Expected: 5 FAIL — module not found.

- [ ] **Step 3: Create the helper**

Create `pipeline/hints.py`:
```python
"""Helpers for assembling personalization-hint context for the planner.

Two responsibilities:
  - build_hints_block: format the ABOUT-THIS-USER block injected into the
    planner system prompt. Empty string when disabled or no hints.
  - hints_cache_tag:   produce a cache-key suffix so users with different
    hints don't share cached plans. Empty when disabled or no hints — so
    the no-personalization majority of traffic keeps 100% cache hit rate.
"""

from __future__ import annotations

import hashlib
import json
from typing import Iterable


_BLOCK_HEADER = "=== ABOUT THIS USER (soft hints, NOT requirements) ==="
_BLOCK_RULES = """\
RULES for these hints:
1. They may only influence should.skills, should.themes, should.locations,
   should.roles, lexical_terms, semantic_query, or hyde_profile.
2. They MUST NEVER be added to must.*, must_not.*, or any hard-filter field.
   A hint is a soft bias, not a requirement.
3. If the current query contradicts a hint, the current query wins —
   ignore the hint for this search.
4. Do not re-offer a hint that is already in this list."""


def build_hints_block(hints: Iterable[dict] | None, *, enabled: bool) -> str:
    """Return the prompt prefix block, or '' when there's nothing to inject."""
    if not enabled or not hints:
        return ""
    texts = [str(h.get("text", "")).strip() for h in hints if h.get("text")]
    texts = [t for t in texts if t]
    if not texts:
        return ""
    bullets = "\n".join(f"- {t}" for t in texts)
    return f"{_BLOCK_HEADER}\n{bullets}\n\n{_BLOCK_RULES}\n\n"


def hints_cache_tag(hints: Iterable[dict] | None, *, enabled: bool) -> str:
    """Return '|h:<12hex>' when hints affect the prompt, else ''."""
    if not enabled or not hints:
        return ""
    texts = sorted(str(h.get("text", "")).strip() for h in hints if h.get("text"))
    texts = [t for t in texts if t]
    if not texts:
        return ""
    payload = json.dumps(texts, sort_keys=True)
    digest  = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"|h:{digest}"
```

- [ ] **Step 4: Run — confirm passes**

Run: `pytest tests/test_personalization_planner.py -v`
Expected: all tests pass (12 total in this file now).

- [ ] **Step 5: Commit**

```bash
git add pipeline/hints.py tests/test_personalization_planner.py
git commit -m "feat(hints): add build_hints_block + hints_cache_tag helpers"
```

---

## Task 6: Update the planner prompt — add `personalization_offer` to schema + rule

**Files:**
- Modify: `pipeline/prompts.py`
- Test: `tests/test_personalization_planner.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_personalization_planner.py`:
```python
from pipeline.prompts import PLANNER_SYSTEM_PROMPT


def test_prompt_includes_personalization_offer_in_output_schema():
    assert '"personalization_offer"' in PLANNER_SYSTEM_PROMPT


def test_prompt_includes_personalization_offer_rule():
    assert "PERSONALIZATION_OFFER" in PLANNER_SYSTEM_PROMPT
    assert "roughly 1 in 5 searches" in PLANNER_SYSTEM_PROMPT
```

- [ ] **Step 2: Run — confirm failures**

Run: `pytest tests/test_personalization_planner.py -v -k personalization_offer`
Expected: 2 FAIL.

- [ ] **Step 3: Edit the prompt**

In `pipeline/prompts.py`, inside `PLANNER_SYSTEM_PROMPT`, find the `=== OUTPUT SCHEMA ===` JSON block and add the new field. Replace:
```
  "clarify": null
}
```
with:
```
  "clarify": null,
  "personalization_offer": null
}
```

Then, in the `=== RULES (apply in order) ===` section, after rule `10. NEVER`, insert a new rule:
```
11. PERSONALIZATION_OFFER
    - null by default. Only set this when the CURRENT query reveals
      something notable about the user's taste that would be useful to
      remember for FUTURE searches.
    - Examples that warrant an offer: queries that repeatedly emphasize
      company stage ("startup", "scale-up"), work style ("hands-on",
      "deep research"), location pattern, or role-shape ("0→1 builder",
      "platform-not-product").
    - Do NOT offer if the saved hints (see ABOUT THIS USER block, if
      present) already cover the observation.
    - Be selective — roughly 1 in 5 searches should produce an offer.
    - Shape: {"hint": "<one-sentence observation>",
              "question": "<one-sentence yes/no>"}
```

- [ ] **Step 4: Run — confirm passes**

Run: `pytest tests/test_personalization_planner.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pipeline/prompts.py tests/test_personalization_planner.py
git commit -m "feat(prompt): add personalization_offer to schema + selectivity rule"
```

---

## Task 7: Thread `system_prompt_prefix` through `_call_llm` + provider wrappers

**Files:**
- Modify: `pipeline/planner.py`
- Test: `tests/test_personalization_planner.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_personalization_planner.py`:
```python
import asyncio
from unittest.mock import AsyncMock, patch


def test_call_llm_accepts_system_prompt_prefix_kwarg():
    """_call_llm must accept and forward a system_prompt_prefix arg."""
    import inspect
    from pipeline.planner import _call_llm, _call_openai, _call_gemini, _call_groq
    for fn in (_call_llm, _call_openai, _call_gemini, _call_groq):
        params = inspect.signature(fn).parameters
        assert "system_prompt_prefix" in params, (
            f"{fn.__name__} missing system_prompt_prefix")
```

- [ ] **Step 2: Run — confirm failure**

Run: `pytest tests/test_personalization_planner.py::test_call_llm_accepts_system_prompt_prefix_kwarg -v`
Expected: FAIL — parameter missing.

- [ ] **Step 3: Update `_call_llm` signature**

In `pipeline/planner.py`, change:
```python
async def _call_llm(
    sanitized_input: str,
    provider_override: str | None = None,
    model_override: str | None = None,
    thinking_level: str | None = None,
) -> dict | None:
```
to:
```python
async def _call_llm(
    sanitized_input: str,
    provider_override: str | None = None,
    model_override: str | None = None,
    thinking_level: str | None = None,
    system_prompt_prefix: str = "",
) -> dict | None:
```

In the same function body, update the three branch calls. Replace:
```python
        if provider == "openai" and _key_configured(settings.openai_api_key):
            return await _call_openai(sanitized_input, model_override)
        if provider == "gemini" and _key_configured(settings.google_api_key):
            return await _call_gemini(sanitized_input, model_override, thinking_level)
        if provider == "groq" and _key_configured(settings.groq_api_key):
            return await _call_groq(sanitized_input, model_override)
```
with:
```python
        if provider == "openai" and _key_configured(settings.openai_api_key):
            return await _call_openai(sanitized_input, model_override, system_prompt_prefix)
        if provider == "gemini" and _key_configured(settings.google_api_key):
            return await _call_gemini(sanitized_input, model_override, thinking_level, system_prompt_prefix)
        if provider == "groq" and _key_configured(settings.groq_api_key):
            return await _call_groq(sanitized_input, model_override, system_prompt_prefix)
```

- [ ] **Step 4: Update `_call_openai`**

Replace:
```python
async def _call_openai(sanitized_input: str, model_override: str | None = None) -> dict | None:
```
with:
```python
async def _call_openai(
    sanitized_input: str,
    model_override: str | None = None,
    system_prompt_prefix: str = "",
) -> dict | None:
```

Inside, replace:
```python
        messages=[
            {"role": "system",  "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user",    "content": build_user_message(sanitized_input)},
        ],
```
with:
```python
        messages=[
            {"role": "system",  "content": system_prompt_prefix + PLANNER_SYSTEM_PROMPT},
            {"role": "user",    "content": build_user_message(sanitized_input)},
        ],
```

- [ ] **Step 5: Update `_call_groq`** (same change as OpenAI)

Replace:
```python
async def _call_groq(sanitized_input: str, model_override: str | None = None) -> dict | None:
```
with:
```python
async def _call_groq(
    sanitized_input: str,
    model_override: str | None = None,
    system_prompt_prefix: str = "",
) -> dict | None:
```

Inside, replace:
```python
        messages=[
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user",   "content": build_user_message(sanitized_input)},
        ],
```
with:
```python
        messages=[
            {"role": "system", "content": system_prompt_prefix + PLANNER_SYSTEM_PROMPT},
            {"role": "user",   "content": build_user_message(sanitized_input)},
        ],
```

- [ ] **Step 6: Update `_call_gemini`**

Replace the signature:
```python
async def _call_gemini(
    sanitized_input: str,
    model_override: str | None = None,
    thinking_level: str | None = None,
) -> dict | None:
```
with:
```python
async def _call_gemini(
    sanitized_input: str,
    model_override: str | None = None,
    thinking_level: str | None = None,
    system_prompt_prefix: str = "",
) -> dict | None:
```

Inside, replace:
```python
    prompt = f"{PLANNER_SYSTEM_PROMPT}\n\n{build_user_message(sanitized_input)}"
```
with:
```python
    prompt = f"{system_prompt_prefix}{PLANNER_SYSTEM_PROMPT}\n\n{build_user_message(sanitized_input)}"
```

- [ ] **Step 7: Run — confirm passes**

Run: `pytest tests/test_personalization_planner.py -v`
Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add pipeline/planner.py tests/test_personalization_planner.py
git commit -m "feat(planner): thread system_prompt_prefix through _call_llm and providers"
```

---

## Task 8: `plan()` builds hints block from `cfg` and passes it through

**Files:**
- Modify: `pipeline/planner.py`
- Test: `tests/test_personalization_planner.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_personalization_planner.py`:
```python
@pytest.mark.asyncio
async def test_plan_builds_and_passes_hints_prefix(monkeypatch):
    """plan() must read hints from cfg and pass a prefix to _call_llm."""
    import pipeline.planner as planner_mod

    captured = {}

    async def fake_call_llm(sanitized_input, provider_override=None,
                            model_override=None, thinking_level=None,
                            system_prompt_prefix=""):
        captured["prefix"] = system_prompt_prefix
        # Return a minimal valid plan so validate_spec is happy.
        return _raw()

    monkeypatch.setattr(planner_mod, "_call_llm", fake_call_llm)
    # Disable cache to avoid cross-test pollution.
    cfg = {
        "use_cache": False,
        "personalization_enabled": True,
        "personalization_hints": [{"text": "user likes startup builders"}],
    }
    await planner_mod.plan("python engineer", cfg=cfg)
    assert "ABOUT THIS USER" in captured["prefix"]
    assert "user likes startup builders" in captured["prefix"]


@pytest.mark.asyncio
async def test_plan_omits_hints_prefix_when_disabled(monkeypatch):
    import pipeline.planner as planner_mod
    captured = {}
    async def fake_call_llm(sanitized_input, provider_override=None,
                            model_override=None, thinking_level=None,
                            system_prompt_prefix=""):
        captured["prefix"] = system_prompt_prefix
        return _raw()
    monkeypatch.setattr(planner_mod, "_call_llm", fake_call_llm)
    cfg = {
        "use_cache": False,
        "personalization_enabled": False,
        "personalization_hints": [{"text": "anything"}],
    }
    await planner_mod.plan("x", cfg=cfg)
    assert captured["prefix"] == ""


@pytest.mark.asyncio
async def test_plan_omits_hints_prefix_when_empty(monkeypatch):
    import pipeline.planner as planner_mod
    captured = {}
    async def fake_call_llm(sanitized_input, provider_override=None,
                            model_override=None, thinking_level=None,
                            system_prompt_prefix=""):
        captured["prefix"] = system_prompt_prefix
        return _raw()
    monkeypatch.setattr(planner_mod, "_call_llm", fake_call_llm)
    cfg = {"use_cache": False, "personalization_enabled": True,
           "personalization_hints": []}
    await planner_mod.plan("x", cfg=cfg)
    assert captured["prefix"] == ""
```

Add this import at the top of the test file if not already there:
```python
import pytest
```

- [ ] **Step 2: Run — confirm failures**

Run: `pytest tests/test_personalization_planner.py -v -k "passes_hints_prefix or omits_hints_prefix"`
Expected: 3 FAIL — prefix is "" because `plan()` doesn't read cfg keys yet.

- [ ] **Step 3: Read cfg keys and call helper**

In `pipeline/planner.py`, at the top:
```python
from pipeline.hints import build_hints_block, hints_cache_tag
```

Inside `plan()`, in the LLM-planner branch (around the `if use_llm_planner:` block), find:
```python
        with _metrics.Timer("planner_ms"):
            try:
                raw_dict = await _call_llm(sanitized, provider_override, model_override, thinking_level)
```
and replace with:
```python
        hints_enabled = bool(cfg.get("personalization_enabled", True))
        hints_list    = cfg.get("personalization_hints") or []
        prefix        = build_hints_block(hints_list, enabled=hints_enabled)
        with _metrics.Timer("planner_ms"):
            try:
                raw_dict = await _call_llm(
                    sanitized, provider_override, model_override,
                    thinking_level, system_prompt_prefix=prefix,
                )
```

- [ ] **Step 4: Run — confirm passes**

Run: `pytest tests/test_personalization_planner.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pipeline/planner.py tests/test_personalization_planner.py
git commit -m "feat(planner): build hints block from cfg and pass prefix to LLM call"
```

---

## Task 9: Cache key includes hints-hash; offer stripped before cache write

**Files:**
- Modify: `pipeline/planner.py`
- Test: `tests/test_personalization_planner.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_personalization_planner.py`:
```python
@pytest.mark.asyncio
async def test_cache_key_changes_with_hints(monkeypatch):
    """Identical inputs but different hints → different cache keys.
       Identical inputs with empty hints → same key as today."""
    import pipeline.planner as planner_mod
    import pipeline.cache as cache_mod

    seen_keys = []
    orig_get = cache_mod.get
    def spy_get(k):
        seen_keys.append(k)
        return None  # always miss
    monkeypatch.setattr(cache_mod, "get", spy_get)
    monkeypatch.setattr(cache_mod, "set", lambda k, v: None)

    async def fake_call_llm(*a, **kw): return _raw()
    monkeypatch.setattr(planner_mod, "_call_llm", fake_call_llm)

    base_cfg = {"use_cache": True, "personalization_enabled": True}
    await planner_mod.plan("python", cfg={**base_cfg, "personalization_hints": []})
    await planner_mod.plan("python", cfg={**base_cfg, "personalization_hints": [{"text": "a"}]})
    await planner_mod.plan("python", cfg={**base_cfg, "personalization_hints": [{"text": "b"}]})

    # All three keys are distinct except: empty-hints key matches the
    # base behavior (no h: suffix), and different hint texts produce
    # different suffixes.
    assert "|h:" not in seen_keys[0]
    assert "|h:" in seen_keys[1]
    assert "|h:" in seen_keys[2]
    assert seen_keys[1] != seen_keys[2]


@pytest.mark.asyncio
async def test_personalization_offer_stripped_before_cache(monkeypatch):
    """Cached plan dict must NOT contain personalization_offer (offers are
    first-search-only — replaying them on cache hits would spam the user)."""
    import pipeline.planner as planner_mod
    import pipeline.cache as cache_mod

    stored = {}
    monkeypatch.setattr(cache_mod, "get", lambda k: None)
    def spy_set(k, v): stored[k] = v
    monkeypatch.setattr(cache_mod, "set", spy_set)

    raw = _raw({"personalization_offer": {"hint": "h", "question": "q?"}})
    async def fake_call_llm(*a, **kw): return raw
    monkeypatch.setattr(planner_mod, "_call_llm", fake_call_llm)

    cfg = {"use_cache": True, "personalization_enabled": True,
           "personalization_hints": []}
    spec = await planner_mod.plan("xy", cfg=cfg)

    # Spec returned to caller still has the offer.
    assert spec.personalization_offer == {"hint": "h", "question": "q?"}
    # But the cached version does not.
    [(k, cached_dict)] = list(stored.items())
    assert "personalization_offer" not in cached_dict or cached_dict["personalization_offer"] is None
```

- [ ] **Step 2: Run — confirm failures**

Run: `pytest tests/test_personalization_planner.py -v -k "cache_key_changes or stripped_before_cache"`
Expected: 2 FAIL.

- [ ] **Step 3: Update the cache key formula**

In `pipeline/planner.py`, find:
```python
    filters_str = json.dumps(explicit_filters or {}, sort_keys=True)
    provider_for_key = cfg.get("llm_provider") or getattr(settings, "llm_provider", "gemini")
    model_for_key    = cfg.get("llm_model")    or getattr(settings, "llm_model", "")
    route_with_llm   = f"{route}|{provider_for_key}|{model_for_key}"
    ck = _cache.plan_key(raw_input.lower().strip(), filters_str, route_with_llm)
```
and replace with:
```python
    filters_str = json.dumps(explicit_filters or {}, sort_keys=True)
    provider_for_key = cfg.get("llm_provider") or getattr(settings, "llm_provider", "gemini")
    model_for_key    = cfg.get("llm_model")    or getattr(settings, "llm_model", "")
    hints_enabled_key = bool(cfg.get("personalization_enabled", True))
    hints_list_key    = cfg.get("personalization_hints") or []
    hints_suffix      = hints_cache_tag(hints_list_key, enabled=hints_enabled_key)
    route_with_llm    = f"{route}|{provider_for_key}|{model_for_key}{hints_suffix}"
    ck = _cache.plan_key(raw_input.lower().strip(), filters_str, route_with_llm)
```

- [ ] **Step 4: Strip offer before caching**

In `pipeline/planner.py`, find:
```python
    # Write to cache
    if use_cache and not spec.used_fallback:
        _cache.set(ck, spec.to_dict())
```
and replace with:
```python
    # Write to cache. Strip personalization_offer first — it's a
    # first-search-only signal; replaying it on every cache hit would
    # spam the user with the same "want to remember this?" banner.
    if use_cache and not spec.used_fallback:
        cached = spec.to_dict()
        cached.pop("personalization_offer", None)
        _cache.set(ck, cached)
```

- [ ] **Step 5: Run — confirm passes**

Run: `pytest tests/test_personalization_planner.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add pipeline/planner.py tests/test_personalization_planner.py
git commit -m "feat(planner): cache key includes hints hash; strip offer before cache write"
```

---

## Task 10: Pipeline loads recruiter hints and injects into cfg

**Files:**
- Modify: `pipeline/search.py`
- Test: `tests/test_personalization_planner.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_personalization_planner.py`:
```python
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_load_recruiter_hints_returns_enabled_and_hints():
    from pipeline.search import HybridSearchEngine
    pool = AsyncMock()
    pool.fetchrow.return_value = {
        "personalization_enabled": True,
        "personalization_hints": '[{"text":"likes builders","source":"manual","created_at":"t"}]',
    }
    engine = HybridSearchEngine.__new__(HybridSearchEngine)
    engine.pool = pool
    enabled, hints = await engine._load_recruiter_hints("rid-1")
    assert enabled is True
    assert hints == [{"text": "likes builders", "source": "manual", "created_at": "t"}]


@pytest.mark.asyncio
async def test_load_recruiter_hints_handles_jsonb_dict():
    """asyncpg may return JSONB as a Python list/dict directly (not a JSON string)."""
    from pipeline.search import HybridSearchEngine
    pool = AsyncMock()
    pool.fetchrow.return_value = {
        "personalization_enabled": False,
        "personalization_hints": [{"text": "a"}],
    }
    engine = HybridSearchEngine.__new__(HybridSearchEngine)
    engine.pool = pool
    enabled, hints = await engine._load_recruiter_hints("rid-2")
    assert enabled is False
    assert hints == [{"text": "a"}]


@pytest.mark.asyncio
async def test_load_recruiter_hints_returns_defaults_on_miss():
    from pipeline.search import HybridSearchEngine
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    engine = HybridSearchEngine.__new__(HybridSearchEngine)
    engine.pool = pool
    enabled, hints = await engine._load_recruiter_hints("nobody")
    assert enabled is True
    assert hints == []


@pytest.mark.asyncio
async def test_load_recruiter_hints_returns_defaults_for_none_id():
    from pipeline.search import HybridSearchEngine
    engine = HybridSearchEngine.__new__(HybridSearchEngine)
    engine.pool = AsyncMock()
    enabled, hints = await engine._load_recruiter_hints(None)
    assert enabled is True
    assert hints == []
```

- [ ] **Step 2: Run — confirm failures**

Run: `pytest tests/test_personalization_planner.py -v -k "load_recruiter_hints"`
Expected: 4 FAIL — method not defined.

- [ ] **Step 3: Add `_load_recruiter_hints` method**

In `pipeline/search.py`, immediately after the existing `_load_recruiter_prefs` method (ends around line 458), add:
```python
    async def _load_recruiter_hints(
        self, recruiter_id: str | None,
    ) -> tuple[bool, list[dict]]:
        """Fetch (personalization_enabled, hints[]) for a recruiter.

        Returns (True, []) on miss or any error — personalization stays
        opt-out by data, never opt-out by failure mode.
        """
        if not recruiter_id:
            return True, []
        try:
            row = await self.pool.fetchrow(
                """SELECT personalization_enabled, personalization_hints
                   FROM recruiter_preferences WHERE recruiter_id = $1::uuid""",
                recruiter_id,
            )
        except Exception as exc:
            logger.debug("recruiter hints fetch failed: %s", exc)
            return True, []
        if not row:
            return True, []
        enabled = bool(row["personalization_enabled"])
        raw_hints = row["personalization_hints"]
        if isinstance(raw_hints, str):
            import json as _json
            try:
                raw_hints = _json.loads(raw_hints)
            except _json.JSONDecodeError:
                raw_hints = []
        if not isinstance(raw_hints, list):
            raw_hints = []
        return enabled, raw_hints
```

- [ ] **Step 4: Inject hints into cfg before `plan()` call**

In `pipeline/search.py`, find the line where `_load_recruiter_prefs` is called (around line 152):
```python
        recruiter_prefs = await self._load_recruiter_prefs(recruiter_id)
```
After it, add:
```python
        hints_enabled, recruiter_hints = await self._load_recruiter_hints(recruiter_id)
```

Then find where `cfg` is set up (line 148: `cfg = get_config(mode, overrides=config_overrides or {})`). After that line, add:
```python
        cfg["personalization_enabled"] = hints_enabled
        cfg["personalization_hints"]   = recruiter_hints
```

- [ ] **Step 5: Run — confirm passes**

Run: `pytest tests/test_personalization_planner.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add pipeline/search.py tests/test_personalization_planner.py
git commit -m "feat(search): load recruiter hints and inject into planner cfg"
```

---

## Task 11: API `SearchResponse` exposes `personalization_offer`

**Files:**
- Modify: `api/main.py`
- Test: `tests/test_personalization_planner.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_personalization_planner.py`:
```python
def test_search_response_exposes_personalization_offer():
    from api.main import SearchResponse
    assert "personalization_offer" in SearchResponse.model_fields
    # Default is None, type is optional dict.
    f = SearchResponse.model_fields["personalization_offer"]
    assert f.default is None
```

- [ ] **Step 2: Run — confirm failure**

Run: `pytest tests/test_personalization_planner.py::test_search_response_exposes_personalization_offer -v`
Expected: FAIL.

- [ ] **Step 3: Add field to `SearchResponse`**

In `api/main.py`, inside the `SearchResponse` class definition (around line 254), after `personalization_applied: bool = Field(...)`, add:
```python
    personalization_offer: Optional[dict] = Field(
        default=None,
        description="LLM's 'want me to remember this for future searches?' "
                    "offer. Shape: {hint, question}. Null when nothing to offer.",
    )
```

- [ ] **Step 4: Forward from spec to response**

In `api/main.py`, the `/search` handler builds a `SearchResponse` in two places. Add the offer field to both.

**Location 1 — clarify-only short-circuit (around line 449-465).** Find:
```python
    if search_resp.clarify and not search_resp.results:
        return SearchResponse(
            ...
            dropped_items=list(getattr(getattr(search_resp, "spec", None), "dropped_items", []) or []),
        )
```
After the `dropped_items=...` line and before the closing `)`, add:
```python
            personalization_offer=getattr(getattr(search_resp, "spec", None), "personalization_offer", None),
```

**Location 2 — main response (around line 482-489).** Find:
```python
        dropped_items=list(getattr(getattr(search_resp, "spec", None), "dropped_items", []) or []),
        personalization_applied=getattr(search_resp, "personalization_applied", False),
```
After the `personalization_applied=` line, add:
```python
        personalization_offer=getattr(getattr(search_resp, "spec", None), "personalization_offer", None),
```

(If line numbers have shifted, search for the two literal lines `dropped_items=list(getattr(...))` — there are exactly two occurrences in `api/main.py`.)

- [ ] **Step 5: Run — confirm passes**

Run: `pytest tests/test_personalization_planner.py -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add api/main.py tests/test_personalization_planner.py
git commit -m "feat(api): expose personalization_offer on SearchResponse"
```

---

## Task 12: API `GET /api/recruiter/{id}/personalization`

**Files:**
- Modify: `api/main.py`
- Create: `tests/test_personalization_api.py`

- [ ] **Step 1: Create the test file with a failing test**

Create `tests/test_personalization_api.py`:
```python
"""Endpoint tests for personalization-hint CRUD + enable toggle."""

import json
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from api import main as api_main


def _install_pool(monkeypatch, fetchrow_return=None, execute_return=None):
    """Replace app.state.pool with an AsyncMock and return it for inspection."""
    pool = AsyncMock()
    pool.fetchrow.return_value = fetchrow_return
    pool.execute.return_value  = execute_return or "UPDATE 1"
    api_main.app.state.pool = pool
    return pool


def test_get_personalization_returns_defaults_when_row_missing(monkeypatch):
    _install_pool(monkeypatch, fetchrow_return=None)
    r = TestClient(api_main.app).get(
        "/api/recruiter/11111111-1111-1111-1111-111111111111/personalization")
    assert r.status_code == 200
    assert r.json() == {"enabled": True, "hints": []}


def test_get_personalization_returns_stored_values(monkeypatch):
    hints = [{"text": "user likes builders", "source": "manual",
              "created_at": "2026-05-27T12:00:00+00:00"}]
    _install_pool(monkeypatch, fetchrow_return={
        "personalization_enabled": False,
        "personalization_hints": hints,
    })
    r = TestClient(api_main.app).get(
        "/api/recruiter/22222222-2222-2222-2222-222222222222/personalization")
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["hints"] == hints
```

- [ ] **Step 2: Run — confirm failures**

Run: `pytest tests/test_personalization_api.py -v`
Expected: FAIL — 404 (endpoint doesn't exist).

- [ ] **Step 3: Add `import json` to top-of-file imports**

In `api/main.py`, in the existing import block (between `import logging` line 15 and `import time` line 16), add:
```python
import json
```

- [ ] **Step 4: Implement the endpoint**

In `api/main.py`, at the bottom of the file (after the last route), add:
```python
# ── Personalization endpoints ──────────────────────────────────────────────

@app.get("/api/recruiter/{recruiter_id}/personalization")
async def get_personalization(recruiter_id: str):
    """Return (enabled, hints[]) for a recruiter. Defaults if row missing."""
    pool = app.state.pool
    row = await pool.fetchrow(
        """SELECT personalization_enabled, personalization_hints
           FROM recruiter_preferences WHERE recruiter_id = $1::uuid""",
        recruiter_id,
    )
    if not row:
        return {"enabled": True, "hints": []}
    raw = row["personalization_hints"]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            raw = []
    if not isinstance(raw, list):
        raw = []
    return {"enabled": bool(row["personalization_enabled"]), "hints": raw}
```

- [ ] **Step 5: Run — confirm passes**

Run: `pytest tests/test_personalization_api.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add api/main.py tests/test_personalization_api.py
git commit -m "feat(api): GET /api/recruiter/{id}/personalization endpoint"
```

---

## Task 13: API `POST /api/recruiter/{id}/hints` with validation

**Files:**
- Modify: `api/main.py`
- Test: `tests/test_personalization_api.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_personalization_api.py`:
```python
def test_post_hint_adds_manual_entry(monkeypatch):
    """Empty row → POST upserts and adds a hint tagged 'manual' by default."""
    pool = _install_pool(monkeypatch, fetchrow_return=None)
    r = TestClient(api_main.app).post(
        "/api/recruiter/33333333-3333-3333-3333-333333333333/hints",
        json={"text": "user likes builders"},
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["hints"]) == 1
    assert body["hints"][0]["text"]   == "user likes builders"
    assert body["hints"][0]["source"] == "manual"
    assert "created_at" in body["hints"][0]


def test_post_hint_with_suggested_source(monkeypatch):
    _install_pool(monkeypatch, fetchrow_return=None)
    r = TestClient(api_main.app).post(
        "/api/recruiter/44444444-4444-4444-4444-444444444444/hints",
        json={"text": "user likes startups", "source": "suggested"},
    )
    assert r.status_code == 200
    assert r.json()["hints"][0]["source"] == "suggested"


def test_post_hint_rejects_text_over_200_chars(monkeypatch):
    _install_pool(monkeypatch, fetchrow_return=None)
    r = TestClient(api_main.app).post(
        "/api/recruiter/55555555-5555-5555-5555-555555555555/hints",
        json={"text": "x" * 201},
    )
    assert r.status_code == 400


def test_post_hint_rejects_empty_text(monkeypatch):
    _install_pool(monkeypatch, fetchrow_return=None)
    r = TestClient(api_main.app).post(
        "/api/recruiter/66666666-6666-6666-6666-666666666666/hints",
        json={"text": "   "},
    )
    assert r.status_code == 400


def test_post_hint_rejects_when_at_cap(monkeypatch):
    existing = [{"text": f"h{i}", "source": "manual",
                 "created_at": "t"} for i in range(20)]
    _install_pool(monkeypatch, fetchrow_return={
        "personalization_enabled": True,
        "personalization_hints": existing,
    })
    r = TestClient(api_main.app).post(
        "/api/recruiter/77777777-7777-7777-7777-777777777777/hints",
        json={"text": "one more"},
    )
    assert r.status_code == 400


def test_post_hint_sanitizes_text(monkeypatch):
    """Control chars + collapsed whitespace via sanitize_input."""
    _install_pool(monkeypatch, fetchrow_return=None)
    r = TestClient(api_main.app).post(
        "/api/recruiter/88888888-8888-8888-8888-888888888888/hints",
        json={"text": "user\x00likes\n\nbuilders"},
    )
    assert r.status_code == 200
    stored = r.json()["hints"][0]["text"]
    assert "\x00" not in stored
    assert "user likes builders" in stored
```

- [ ] **Step 2: Run — confirm failures**

Run: `pytest tests/test_personalization_api.py -v`
Expected: 6 new FAIL (endpoint missing).

- [ ] **Step 3: Implement the endpoint**

In `api/main.py`, immediately after `get_personalization`, add:
```python
from datetime import datetime, timezone
from pipeline.sanitize import sanitize_input as _sanitize_hint

@app.post("/api/recruiter/{recruiter_id}/hints")
async def add_personalization_hint(recruiter_id: str, body: dict):
    """Append a hint. source defaults to 'manual'.
       400 if text empty/too long, or if user already has 20 hints."""
    text   = _sanitize_hint(str(body.get("text", "")))
    source = body.get("source") if body.get("source") in {"manual", "suggested"} else "manual"

    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 200:
        raise HTTPException(status_code=400, detail="text exceeds 200 chars")

    pool = app.state.pool
    row = await pool.fetchrow(
        """SELECT personalization_hints FROM recruiter_preferences
           WHERE recruiter_id = $1::uuid""", recruiter_id,
    )
    if row and row["personalization_hints"]:
        existing = row["personalization_hints"]
        if isinstance(existing, str):
            existing = _json_personalization.loads(existing)
    else:
        existing = []
    if len(existing) >= 20:
        raise HTTPException(status_code=400, detail="hint cap reached (20)")

    new_hint = {
        "text": text,
        "source": source,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    updated = [new_hint] + existing

    await pool.execute(
        """INSERT INTO recruiter_preferences (recruiter_id, personalization_hints)
           VALUES ($1::uuid, $2::jsonb)
           ON CONFLICT (recruiter_id) DO UPDATE
             SET personalization_hints = EXCLUDED.personalization_hints,
                 updated_at = NOW()""",
        recruiter_id, _json_personalization.dumps(updated),
    )
    return {"hints": updated}
```

Make sure `HTTPException` is imported at the top of `api/main.py` (it usually already is via `from fastapi import ...`); if not, add it to the existing fastapi import line.

- [ ] **Step 4: Run — confirm passes**

Run: `pytest tests/test_personalization_api.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_personalization_api.py
git commit -m "feat(api): POST /api/recruiter/{id}/hints with size + count validation"
```

---

## Task 14: API `DELETE /api/recruiter/{id}/hints/{idx}`

**Files:**
- Modify: `api/main.py`
- Test: `tests/test_personalization_api.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_personalization_api.py`:
```python
def test_delete_hint_removes_at_index(monkeypatch):
    hints = [{"text": "a", "source": "manual", "created_at": "t1"},
             {"text": "b", "source": "manual", "created_at": "t2"},
             {"text": "c", "source": "manual", "created_at": "t3"}]
    _install_pool(monkeypatch, fetchrow_return={
        "personalization_enabled": True,
        "personalization_hints": hints,
    })
    r = TestClient(api_main.app).delete(
        "/api/recruiter/99999999-9999-9999-9999-999999999999/hints/1")
    assert r.status_code == 200
    remaining = [h["text"] for h in r.json()["hints"]]
    assert remaining == ["a", "c"]


def test_delete_hint_out_of_range_returns_404(monkeypatch):
    _install_pool(monkeypatch, fetchrow_return={
        "personalization_enabled": True,
        "personalization_hints": [{"text": "a"}],
    })
    r = TestClient(api_main.app).delete(
        "/api/recruiter/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/hints/5")
    assert r.status_code == 404


def test_delete_hint_when_no_row_returns_404(monkeypatch):
    _install_pool(monkeypatch, fetchrow_return=None)
    r = TestClient(api_main.app).delete(
        "/api/recruiter/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb/hints/0")
    assert r.status_code == 404
```

- [ ] **Step 2: Run — confirm failures**

Run: `pytest tests/test_personalization_api.py -v -k delete_hint`
Expected: 3 FAIL.

- [ ] **Step 3: Implement the endpoint**

In `api/main.py`, immediately after `add_personalization_hint`, add:
```python
@app.delete("/api/recruiter/{recruiter_id}/hints/{idx}")
async def delete_personalization_hint(recruiter_id: str, idx: int):
    pool = app.state.pool
    row = await pool.fetchrow(
        """SELECT personalization_hints FROM recruiter_preferences
           WHERE recruiter_id = $1::uuid""", recruiter_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="no hints for recruiter")
    existing = row["personalization_hints"]
    if isinstance(existing, str):
        existing = _json_personalization.loads(existing)
    if not isinstance(existing, list) or idx < 0 or idx >= len(existing):
        raise HTTPException(status_code=404, detail="hint index out of range")
    updated = existing[:idx] + existing[idx + 1:]
    await pool.execute(
        """UPDATE recruiter_preferences SET personalization_hints = $2::jsonb,
              updated_at = NOW() WHERE recruiter_id = $1::uuid""",
        recruiter_id, _json_personalization.dumps(updated),
    )
    return {"hints": updated}
```

- [ ] **Step 4: Run — confirm passes**

Run: `pytest tests/test_personalization_api.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_personalization_api.py
git commit -m "feat(api): DELETE /api/recruiter/{id}/hints/{idx} with 404 handling"
```

---

## Task 15: API `PATCH /api/recruiter/{id}/personalization` (toggle)

**Files:**
- Modify: `api/main.py`
- Test: `tests/test_personalization_api.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_personalization_api.py`:
```python
def test_patch_personalization_toggles_enabled(monkeypatch):
    pool = _install_pool(monkeypatch, fetchrow_return=None)
    r = TestClient(api_main.app).patch(
        "/api/recruiter/cccccccc-cccc-cccc-cccc-cccccccccccc/personalization",
        json={"enabled": False},
    )
    assert r.status_code == 200
    assert r.json() == {"enabled": False}
    # Verify the upsert SQL was called.
    assert pool.execute.await_count == 1


def test_patch_personalization_requires_bool(monkeypatch):
    _install_pool(monkeypatch, fetchrow_return=None)
    r = TestClient(api_main.app).patch(
        "/api/recruiter/dddddddd-dddd-dddd-dddd-dddddddddddd/personalization",
        json={"enabled": "not a bool"},
    )
    assert r.status_code == 400
```

- [ ] **Step 2: Run — confirm failures**

Run: `pytest tests/test_personalization_api.py -v -k patch_personalization`
Expected: 2 FAIL.

- [ ] **Step 3: Implement the endpoint**

In `api/main.py`, immediately after `delete_personalization_hint`, add:
```python
@app.patch("/api/recruiter/{recruiter_id}/personalization")
async def patch_personalization(recruiter_id: str, body: dict):
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled must be a bool")
    pool = app.state.pool
    await pool.execute(
        """INSERT INTO recruiter_preferences (recruiter_id, personalization_enabled)
           VALUES ($1::uuid, $2)
           ON CONFLICT (recruiter_id) DO UPDATE
             SET personalization_enabled = EXCLUDED.personalization_enabled,
                 updated_at = NOW()""",
        recruiter_id, enabled,
    )
    return {"enabled": enabled}
```

- [ ] **Step 4: Run — confirm passes**

Run: `pytest tests/test_personalization_api.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_personalization_api.py
git commit -m "feat(api): PATCH /api/recruiter/{id}/personalization toggle"
```

---

## Task 16: Hints-leak regression test (extend `tests/test_dropped_items.py`)

**Files:**
- Modify: `tests/test_dropped_items.py`

- [ ] **Step 1: Add the regression test**

In `tests/test_dropped_items.py`, after the existing tests, append:
```python
def test_hint_derived_skill_in_must_is_demoted_not_kept():
    """Safety contract: even when a hint tells the LLM 'user likes fintech',
    a fintech that lands in must.skills (and isn't in the input) must still
    be demoted to should.skills by the existing hallucination demoter."""
    raw = _raw_with_must_skills(["python", "fintech"])
    spec = validate_spec(raw, original_input="python developer")
    assert spec.must.skills == ["python"]
    assert "fintech" in spec.should.skills
    # No drop record — fintech is preserved, just moved.
    assert spec.dropped_items == []
```

- [ ] **Step 2: Run — confirm passes**

Run: `pytest tests/test_dropped_items.py -v`
Expected: all pass (the demoter from prior work already does this; this is regression coverage).

- [ ] **Step 3: Commit**

```bash
git add tests/test_dropped_items.py
git commit -m "test(dropped_items): regression for hint-derived must.skill demote"
```

---

## Task 17: Search UI — render offer banner

**Files:**
- Modify: `api/static/admin.html`

- [ ] **Step 1: Locate the existing search-results rendering**

Open `api/static/admin.html` and find the `searchNotices` rendering region (around line 3210-3247, where dropped_items is rendered into `notices`).

- [ ] **Step 2: Add offer-banner rendering**

In `api/static/admin.html`, inside the function that builds the notices list (immediately before `$("searchNotices").hidden = ...`), insert:
```javascript
      // Personalization offer — proactive "want me to remember this?" prompt.
      // Shown above other notices so the user sees it first.
      if (data.personalization_offer && data.personalization_offer.hint) {
        const offer = data.personalization_offer;
        const hintEsc = escapeHtml(offer.hint);
        const qEsc    = escapeHtml(offer.question || "Want me to remember this?");
        notices.unshift(`
          <div class="notice info" id="personalizationOffer">
            <strong>💡 ${hintEsc}</strong>
            <div>${qEsc}</div>
            <div style="margin-top:6px">
              <button id="acceptPersonalizationBtn" class="primary" type="button">Yes, remember</button>
              <button id="dismissPersonalizationBtn" type="button">Not now</button>
            </div>
          </div>
        `);
      }
```

- [ ] **Step 3: Wire button handlers**

In `api/static/admin.html`, right after the notices innerHTML assignment (`$("searchNotices").innerHTML = notices.join("")`), add:
```javascript
      const acceptBtn = document.getElementById("acceptPersonalizationBtn");
      const dismissBtn = document.getElementById("dismissPersonalizationBtn");
      if (acceptBtn) {
        acceptBtn.addEventListener("click", async () => {
          const recId = $("searchRecruiterId")?.value?.trim();
          if (!recId) {
            alert("Set a recruiter ID first to save personalization hints.");
            return;
          }
          const text = data.personalization_offer.hint;
          try {
            await api(`/api/recruiter/${encodeURIComponent(recId)}/hints`, {
              method: "POST",
              body: JSON.stringify({ text, source: "suggested" }),
            });
            document.getElementById("personalizationOffer")?.remove();
          } catch (e) {
            alert("Could not save hint: " + (e?.message || e));
          }
        });
      }
      if (dismissBtn) {
        dismissBtn.addEventListener("click", () => {
          document.getElementById("personalizationOffer")?.remove();
        });
      }
```

- [ ] **Step 4: Smoke-test in a browser**

Start the dev server (e.g. `uvicorn api.main:app --reload`), open `/ui`, set a recruiter ID, run any search. If the planner emits an offer, confirm the banner renders with both buttons. Test [Yes, remember] (should disappear + POST a hint), test [Not now] (should disappear without a POST). Verify a subsequent identical search does NOT re-show the banner (offer is stripped from the cache).

- [ ] **Step 5: Commit**

```bash
git add api/static/admin.html
git commit -m "feat(ui): render personalization offer banner with accept/dismiss"
```

---

## Task 18: Settings UI — Personalization card

**Files:**
- Modify: `api/static/settings.html`

- [ ] **Step 1: Locate insertion point**

Open `api/static/settings.html`. Find the existing settings card structure (the markup that wraps the Save settings button around line 449). The new card goes before the closing of the main settings container, so it sits alongside other configuration cards.

- [ ] **Step 2: Add the Personalization card markup**

In `api/static/settings.html`, just before the existing `<button id="saveSettingsBtn" ...>` button (line 449), insert:
```html
        <section class="card" id="personalizationCard" style="margin-top:20px">
          <h2>Personalization</h2>
          <p class="muted">
            Soft hints the planner uses to bias future searches toward
            candidates like the ones you tend to pick.
            Hints never become hard filters.
          </p>
          <label for="recruiterIdSetting">Recruiter ID</label>
          <input id="recruiterIdSetting" type="text"
                 placeholder="UUID — needed to load/save your hints"
                 style="width:100%" />
          <div style="margin-top:10px">
            <label>
              <input type="checkbox" id="personalizationEnabledToggle" />
              Use my personalization hints
            </label>
          </div>
          <div id="hintsList" style="margin-top:14px"></div>
          <div style="margin-top:10px;display:flex;gap:6px">
            <input id="newHintInput" type="text" placeholder="Add a hint (≤200 chars)" style="flex:1" />
            <button id="addHintBtn" type="button" class="primary">Add</button>
          </div>
          <div id="hintsStatus" class="muted" style="margin-top:6px"></div>
        </section>
```

- [ ] **Step 3: Add the controller script**

In `api/static/settings.html`, at the end of the existing `<script>` block (before `</script>`), append:
```javascript
      // ── Personalization card controller ─────────────────────────────────
      // Local escape helper — settings.html doesn't have one defined.
      function escapeHtml(s) {
        return String(s).replace(/[&<>"']/g, c => ({
          "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
        }[c]));
      }
      function pState() { return state.__personalization || (state.__personalization = {recId: "", hints: [], enabled: true}); }

      function renderHints() {
        const list = document.getElementById("hintsList");
        const s = pState();
        if (!s.hints.length) {
          list.innerHTML = '<div class="muted">No hints yet.</div>';
          return;
        }
        list.innerHTML = s.hints.map((h, i) => {
          const icon = h.source === "suggested" ? "💡" : "✏️";
          return `<div class="row" style="display:flex;justify-content:space-between;align-items:center;padding:4px 0">
            <span>${icon} ${escapeHtml(h.text)}</span>
            <button data-hint-idx="${i}" type="button">×</button>
          </div>`;
        }).join("");
        list.querySelectorAll("button[data-hint-idx]").forEach(btn => {
          btn.addEventListener("click", async (e) => {
            const idx = parseInt(e.target.dataset.hintIdx, 10);
            await api(`/api/recruiter/${encodeURIComponent(pState().recId)}/hints/${idx}`, {method: "DELETE"});
            await loadHints();
          });
        });
      }

      async function loadHints() {
        const s = pState();
        if (!s.recId) return;
        const data = await api(`/api/recruiter/${encodeURIComponent(s.recId)}/personalization`);
        s.enabled = !!data.enabled;
        s.hints   = data.hints || [];
        document.getElementById("personalizationEnabledToggle").checked = s.enabled;
        renderHints();
      }

      document.getElementById("recruiterIdSetting").addEventListener("change", (e) => {
        pState().recId = e.target.value.trim();
        if (pState().recId) loadHints().catch(() => {});
      });

      document.getElementById("personalizationEnabledToggle").addEventListener("change", async (e) => {
        const s = pState();
        if (!s.recId) return;
        await api(`/api/recruiter/${encodeURIComponent(s.recId)}/personalization`, {
          method: "PATCH",
          body: JSON.stringify({enabled: e.target.checked}),
        });
      });

      document.getElementById("addHintBtn").addEventListener("click", async () => {
        const s = pState();
        const text = document.getElementById("newHintInput").value.trim();
        if (!s.recId) { document.getElementById("hintsStatus").textContent = "Set a recruiter ID first."; return; }
        if (!text)     { document.getElementById("hintsStatus").textContent = "Enter some text.";       return; }
        try {
          await api(`/api/recruiter/${encodeURIComponent(s.recId)}/hints`, {
            method: "POST",
            body: JSON.stringify({text, source: "manual"}),
          });
          document.getElementById("newHintInput").value = "";
          document.getElementById("hintsStatus").textContent = "";
          await loadHints();
        } catch (e) {
          document.getElementById("hintsStatus").textContent = "Error: " + (e?.message || e);
        }
      });
```

- [ ] **Step 4: Smoke-test in a browser**

Start the dev server. Open `/settings`. Enter a recruiter UUID, confirm hints load (or empty list). Add a hint, confirm it appears with ✏️ icon and persists across reload. Toggle the master switch off, run a search, confirm the planner prompt no longer contains the hints block (check the network panel / response `planner_spec` if exposed; otherwise inspect server logs). Delete a hint, confirm it disappears.

- [ ] **Step 5: Commit**

```bash
git add api/static/settings.html
git commit -m "feat(ui): personalization card in settings (toggle + list + add)"
```

---

## Self-Review

Run the full test suite once at the end to make sure nothing regressed:
```bash
pytest tests/ -q
```

If anything fails, debug before reporting completion.

---

## Spec coverage map

| Spec section | Implemented in task(s) |
|---|---|
| Data model — new columns | Task 1 |
| Per-hint ≤ 200 chars cap | Task 13 |
| ≤ 20 hints cap | Task 13 |
| Sanitization at write time | Task 13 |
| `personalization_offer` field on spec | Task 2 |
| `personalization_offer` validator parse | Task 3 |
| `_spec_from_dict` reconstruction | Task 4 |
| System-prompt schema field | Task 6 |
| System-prompt rule (~1 in 5) | Task 6 |
| Hints block format + rules block | Task 5 |
| Pipeline loader + cfg injection | Task 10 |
| Provider wrappers accept prefix | Task 7 |
| `plan()` reads cfg and builds prefix | Task 8 |
| Cache key partitioning by hints hash | Task 9 |
| Cache write strips offer | Task 9 |
| Fallback path (no offer, no hints) | Implicit — fallback never enters LLM branch |
| `SearchResponse.personalization_offer` | Task 11 |
| GET /personalization | Task 12 |
| POST /hints | Task 13 |
| DELETE /hints/{idx} | Task 14 |
| PATCH /personalization | Task 15 |
| Search UI banner | Task 17 |
| Settings UI card | Task 18 |
| Hints-leak regression test | Task 16 |
| Interaction with explicit_filters | Already works — `_merge_explicit` runs after validate_spec and demote |
