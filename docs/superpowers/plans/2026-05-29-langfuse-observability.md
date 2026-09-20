# Langfuse Observability & Evaluation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Langfuse as the production observability + evaluation layer for the hybrid search pipeline. Existing local admin UI, `/metrics`, and `pipeline/metrics.py` stay as the local-debug cockpit; Langfuse becomes the production trace/score/comparison surface.

**Architecture:** A new `pipeline/observability.py` wraps the Langfuse SDK behind a no-op-safe facade so search behavior is identical whether Langfuse is on or off. The `/search` handler becomes the parent trace; planner LLM calls become nested generations (auto-instrumented via `langfuse.openai` for OpenAI/Groq, manual spans for Gemini). Non-LLM pipeline phases (cache, retrieval, rerank) emit spans. Each request emits a defined set of scores. Search impressions store the trace ID so `/outcomes` can post user-feedback scores back to the originating trace, closing the loop.

**Tech Stack:** `langfuse >= 4.x` Python SDK, Pydantic Settings, FastAPI lifespan hooks, Postgres (existing), pytest + AsyncMock.

**Background:**
- Existing internal observability: `pipeline/metrics.py` counters + `timings_ms` / `phase_timings` in `SearchResponse`. Local-only, not historical.
- Existing LLM call sites: planner (`pipeline/planner.py`), HyDE (inside planner), AI insights (`pipeline/ai_insights.py`).
- Existing impression/outcome tables: `db/migrations/004_impressions.sql`. Outcomes endpoint at `api/main.py` around line 612.
- Existing settings: `class Settings(BaseSettings)` at [pipeline/__init__.py:12](pipeline/__init__.py#L12).

**Out of scope (deferred):**
- **Replacing `pipeline/metrics.py`**: Explicitly NOT doing — sync/local serves a different purpose than async/remote; keep both permanently.
- **LLM-as-judge evaluators running inside Langfuse Cloud**: Skipped because you're self-hosting — the hosted auto-evaluator is Cloud-gated; Phase E's SDK-based evals are portable and live in your repo.
- **Retrieval/rerank phase spans**: Task 5 covers planner phases only — retrieval can be added in a follow-up; risk is low because BM25/dense/skill are not on the LLM critical path.
- **Per-recruiter cost dashboards**: Use the Metrics API in the Langfuse UI; no code change needed.

---

## File Structure

**Create:**
- `pipeline/observability.py` — no-op-safe facade over `langfuse` SDK + `ScoreName` enum + adaptive sampler + redaction helper
- `tests/test_observability.py` — covers facade behavior with Langfuse on/off, sampling decisions, redaction
- `db/migrations/007_langfuse_trace_ids.sql` — adds `langfuse_trace_id TEXT` column to `search_impressions`
- `scripts/langfuse_register_models.py` — one-shot: registers Gemini + Groq model pricing so cost dashboards are accurate
- `scripts/langfuse_dataset_seed.py` — one-shot script that creates a Langfuse dataset from existing test fixtures
- `scripts/langfuse_experiment.py` — runs `plan()` against the dataset, scores results, posts to Langfuse
- `tests/test_observability_integration.py` — verifies the trace path through `/search` end-to-end with a stubbed Langfuse

**Modify:**
- `pipeline/__init__.py` — add Langfuse-related settings fields to `Settings`
- `pipeline/planner.py` — swap to `langfuse.openai`, wrap phases in spans
- `pipeline/search.py` — pass `langfuse_trace_id` into impression INSERT
- `api/main.py` — lifespan init/shutdown, `/search` trace decoration, add `langfuse_trace_id` + `langfuse_trace_url` to `SearchResponse`, `/outcomes` scores
- `api/static/admin.html` — small "View in Langfuse" link when trace URL present
- `scripts/evaluate_rankers.py` — add `--langfuse` flag that posts ranking-quality scores to Langfuse

---

## Phases

| Phase | Tasks | What it unlocks |
|---|---|---|
| **A** | 1–3 | Foundation: dependency, settings, facade module. Nothing visible yet. |
| **B** | 4–7, 13 (incl. 4.5, 5.5) | Minimum viable tracing: searches appear in Langfuse UI with correct cost data. |
| **C** | 8–10 | Impression ↔ trace ↔ outcome loop. User feedback bound to LLM behavior. |
| **D** | 11–12 | Score taxonomy, adaptive sampling, prod-safe redaction. |
| **E** | 14–16 | Eval scripts integration: regression dataset, benchmark export, prompt-change confidence. |
| **F** | 17 | Admin UI link to trace. |
| **G** | 18–19 | Prompt management (fallback via git) & Replay trace script. |

You can ship each phase independently. Phase A is required for everything; B is the most valuable single step.

---

## Phase A — Foundation

### Task 1: Add `langfuse` dependency + settings fields

**Files:**
- Modify: `pyproject.toml` (or `requirements.txt` if that's the active dep manifest)
- Modify: `pipeline/__init__.py`

- [ ] **Step 1: Add the dependency**

If `pyproject.toml` has a `dependencies = [...]` list, add `"langfuse>=4.0"` to it. If `requirements.txt` is the active manifest, append `langfuse>=4.0` on its own line. Verify with:
```bash
pip install "langfuse>=4.0" && python -c "from langfuse import get_client; print('ok')"
```
Expected: `ok`.

- [ ] **Step 2: Add settings fields**

In `pipeline/__init__.py`, inside `class Settings(BaseSettings):`, after the existing LLM-related fields (around `groq_api_key`), insert:
```python
    # ── Langfuse observability ────────────────────────────────────────────
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_environment: str = "development"
    langfuse_release: str = ""
    # "dev_full" = capture inputs/outputs verbatim (resumes, emails).
    # "prod_redacted" = strip PII before sending.
    langfuse_trace_content_mode: str = "prod_redacted"
    # Fraction of happy-path traces to send (errors and slow traces always send).
    langfuse_sample_rate: float = 1.0
    langfuse_slow_threshold_ms: float = 1000.0
```

- [ ] **Step 3: Verify settings parse**

Run:
```bash
python -c "from pipeline import settings; print(settings.langfuse_enabled, settings.langfuse_sample_rate)"
```
Expected: `False 1.0`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml pipeline/__init__.py
git commit -m "chore(langfuse): add SDK dependency and settings fields"
```

---

### Task 2: Create no-op-safe `observability.py` facade + `ScoreName` enum

**Files:**
- Create: `pipeline/observability.py`
- Create: `tests/test_observability.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_observability.py`:
```python
"""Tests for the Langfuse facade. Search must work identically whether
Langfuse is enabled, disabled, or failing."""

from unittest.mock import MagicMock, patch

from pipeline import observability as obs


def test_score_name_enum_has_expected_members():
    # Closed set of score names. Adding a new score requires adding to enum.
    expected = {
        "RESULT_COUNT", "CACHE_HIT", "FALLBACK_USED", "CONFIDENCE",
        "DROPPED_COUNT", "PERSONALIZATION_OFFER_EMITTED",
        "PERSONALIZATION_OFFER_ACCEPTED", "HINT_COUNT",
        "RECRUITER_ACTION", "POSITIVE_OUTCOME", "TOTAL_COST",
        "PLANNER_FILTER_ACCURACY", "TOP1", "HIT3", "HIT10", "MRR10", "NDCG10",
    }
    assert {m.name for m in obs.ScoreName} == expected


def test_is_enabled_returns_false_when_setting_off(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", False)
    assert obs.is_enabled() is False


def test_record_score_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", False)
    # Must not raise even though no client is initialised.
    obs.record_score(trace_id="t1", name=obs.ScoreName.RESULT_COUNT, value=5)


def test_record_score_calls_client_when_enabled(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    fake = MagicMock()
    monkeypatch.setattr(obs, "_get_client", lambda: fake)
    obs.record_score(trace_id="t1", name=obs.ScoreName.RESULT_COUNT, value=5, comment="ok")
    fake.create_score.assert_called_once_with(
        trace_id="t1", name="result_count", value=5, comment="ok")


def test_record_score_swallows_client_exceptions(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    fake = MagicMock()
    fake.create_score.side_effect = RuntimeError("network down")
    monkeypatch.setattr(obs, "_get_client", lambda: fake)
    # Must not raise — Langfuse failures must never fail search.
    obs.record_score(trace_id="t1", name=obs.ScoreName.RESULT_COUNT, value=5)


def test_should_sample_always_true_for_errors(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_sample_rate", 0.0)
    assert obs.should_sample(latency_ms=50, status_code=500) is True


def test_should_sample_always_true_for_slow(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_sample_rate", 0.0)
    monkeypatch.setattr(obs._settings, "langfuse_slow_threshold_ms", 1000)
    assert obs.should_sample(latency_ms=1500, status_code=200) is True


def test_should_sample_respects_sample_rate_for_happy_path(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_sample_rate", 0.0)
    monkeypatch.setattr(obs._settings, "langfuse_slow_threshold_ms", 1000)
    # Rate 0 → never sample fast successes.
    assert obs.should_sample(latency_ms=100, status_code=200) is False
    monkeypatch.setattr(obs._settings, "langfuse_sample_rate", 1.0)
    assert obs.should_sample(latency_ms=100, status_code=200) is True


def test_redact_strips_emails_and_long_text_in_prod_mode(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_trace_content_mode", "prod_redacted")
    raw = {"query": "alice@example.com python developer",
           "resume_text": "x" * 5000}
    cleaned = obs.redact(raw)
    assert "alice@example.com" not in str(cleaned)
    assert len(str(cleaned["resume_text"])) <= 200


def test_redact_passthrough_in_dev_mode(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_trace_content_mode", "dev_full")
    raw = {"query": "alice@example.com"}
    assert obs.redact(raw) == raw
```

- [ ] **Step 2: Run — confirm failures**

Run: `pytest tests/test_observability.py -v`
Expected: all FAIL — module doesn't exist.

- [ ] **Step 3: Implement the facade**

Create `pipeline/observability.py`:
```python
"""No-op-safe Langfuse facade.

Wraps the Langfuse SDK so that:
  - Search behavior is identical when Langfuse is off (langfuse_enabled=False).
  - Every Langfuse call is wrapped in try/except so a Langfuse outage cannot
    fail a search request.
  - PII redaction is centralized (prod_redacted vs dev_full).
  - Score names come from a closed enum to keep dashboards clean.
  - Sampling is adaptive: errors and slow traces always send, happy-path
    sampling respects langfuse_sample_rate.
"""

from __future__ import annotations

import logging
import random
import re
from enum import StrEnum
from typing import Any

from pipeline import settings as _settings

logger = logging.getLogger(__name__)

_client = None
_initialized = False


class ScoreName(StrEnum):
    # Search outcome
    RESULT_COUNT  = "result_count"
    CACHE_HIT     = "cache_hit"
    FALLBACK_USED = "fallback_used"
    CONFIDENCE    = "confidence"
    DROPPED_COUNT = "dropped_count"
    # Personalization
    PERSONALIZATION_OFFER_EMITTED  = "personalization_offer_emitted"
    PERSONALIZATION_OFFER_ACCEPTED = "personalization_offer_accepted"
    HINT_COUNT                     = "hint_count"
    # User feedback (posted from /outcomes)
    RECRUITER_ACTION  = "recruiter_action"
    POSITIVE_OUTCOME  = "positive_outcome"
    # Eval scripts
    PLANNER_FILTER_ACCURACY = "planner_filter_accuracy"
    TOP1  = "top1"
    HIT3  = "hit3"
    HIT10 = "hit10"
    MRR10 = "mrr10"
    NDCG10 = "ndcg10"


def is_enabled() -> bool:
    return bool(_settings.langfuse_enabled)


def init_langfuse() -> None:
    """Initialise the SDK once at app startup. Safe to call when disabled."""
    global _client, _initialized
    if _initialized:
        return
    _initialized = True
    if not is_enabled():
        logger.info("Langfuse disabled (langfuse_enabled=False)")
        return
    try:
        from langfuse import Langfuse
        _client = Langfuse(
            public_key=_settings.langfuse_public_key,
            secret_key=_settings.langfuse_secret_key,
            host=_settings.langfuse_base_url,
            environment=_settings.langfuse_environment,
            release=_settings.langfuse_release or None,
            # Centrally apply redaction so every observation payload is
            # cleaned before sending, no matter where it originates.
            # This means we do NOT need to call redact() manually at call
            # sites — the SDK handles it automatically.
            mask=redact,
        )
        logger.info("Langfuse initialised (host=%s env=%s)",
                    _settings.langfuse_base_url, _settings.langfuse_environment)
    except Exception as exc:
        logger.warning("Langfuse init failed: %s — continuing without it", exc)
        _client = None


def shutdown_langfuse() -> None:
    """Flush queued events. Safe to call when disabled."""
    global _client
    if _client is None:
        return
    try:
        _client.flush()
    except Exception as exc:
        logger.debug("Langfuse flush failed: %s", exc)


def _get_client():
    return _client


def record_score(
    *, trace_id: str, name: ScoreName | str, value: float | str | bool,
    comment: str | None = None,
) -> None:
    """Record a score against a trace. No-op if disabled. Never raises."""
    if not is_enabled():
        return
    client = _get_client()
    if client is None:
        return
    try:
        client.create_score(
            trace_id=trace_id,
            name=str(name),
            value=value,
            comment=comment,
        )
    except Exception as exc:
        logger.debug("Langfuse create_score failed (%s): %s", name, exc)


def should_sample(*, latency_ms: float, status_code: int) -> bool:
    """Adaptive sampling: errors and slow requests always sampled."""
    if status_code >= 400:
        return True
    if latency_ms >= _settings.langfuse_slow_threshold_ms:
        return True
    return random.random() < _settings.langfuse_sample_rate


_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def redact(payload: Any) -> Any:
    """Strip PII from a payload when content_mode=prod_redacted. Recursive."""
    if _settings.langfuse_trace_content_mode == "dev_full":
        return payload
    if isinstance(payload, dict):
        return {k: redact(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [redact(v) for v in payload]
    if isinstance(payload, str):
        cleaned = _EMAIL_RE.sub("[email-redacted]", payload)
        return cleaned[:200]
    return payload
```

- [ ] **Step 4: Run — confirm passes**

Run: `pytest tests/test_observability.py -v`
Expected: all 10 pass.

- [ ] **Step 5: Commit**

```bash
git add pipeline/observability.py tests/test_observability.py
git commit -m "feat(observability): add no-op-safe Langfuse facade with ScoreName enum"
```

---

### Task 3: Initialise Langfuse in FastAPI lifespan

**Files:**
- Modify: `api/main.py` (lifespan function near line 67)
- Test: `tests/test_observability.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_observability.py`:
```python
def test_init_langfuse_idempotent(monkeypatch):
    # Calling init twice should not create two clients.
    monkeypatch.setattr(obs, "_client", None)
    monkeypatch.setattr(obs, "_initialized", False)
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    monkeypatch.setattr(obs._settings, "langfuse_public_key", "pk")
    monkeypatch.setattr(obs._settings, "langfuse_secret_key", "sk")
    with patch("langfuse.Langfuse") as FakeLF:
        obs.init_langfuse()
        obs.init_langfuse()
        assert FakeLF.call_count == 1


def test_init_langfuse_swallows_init_errors(monkeypatch):
    monkeypatch.setattr(obs, "_client", None)
    monkeypatch.setattr(obs, "_initialized", False)
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    with patch("langfuse.Langfuse", side_effect=RuntimeError("bad key")):
        # Must not raise.
        obs.init_langfuse()
    assert obs._client is None
```

- [ ] **Step 2: Run — confirm 2 pass already**

Run: `pytest tests/test_observability.py -v -k "init_langfuse"`
Expected: both PASS (idempotency already guaranteed by `_initialized` flag; error-swallowing already in place).

- [ ] **Step 3: Wire init/shutdown into lifespan**

In `api/main.py`, find the existing `@asynccontextmanager` `async def lifespan(app: FastAPI):` function. Add a call to `init_langfuse()` near the top of the startup block and `shutdown_langfuse()` near the bottom of the cleanup block.

Insert at the top of `api/main.py` imports (after the existing pipeline imports):
```python
from pipeline.observability import init_langfuse, shutdown_langfuse
```

Inside `lifespan`, immediately after `app.state.pool = pool`:
```python
    init_langfuse()
```

In the cleanup section (after the `yield`, before `await close_pool()`):
```python
    shutdown_langfuse()
```

- [ ] **Step 4: Verify the app still starts**

Run:
```bash
LANGFUSE_ENABLED=false python -c "from api.main import app; print('ok')"
```
Expected: `ok` (no errors).

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_observability.py
git commit -m "feat(observability): init Langfuse in FastAPI lifespan"
```

---

## Phase B — Minimum viable tracing

### Task 4: Swap to `langfuse.openai` for planner LLM calls

**Files:**
- Modify: `pipeline/planner.py`
- Test: `tests/test_personalization_planner.py` (existing — verify no regression)

- [ ] **Step 1: Make the swap**

In `pipeline/planner.py`, inside `_call_openai()`, change:
```python
    from openai import AsyncOpenAI
```
to:
```python
    from langfuse.openai import AsyncOpenAI  # auto-instruments OpenAI calls
```

Repeat the identical change inside `_call_groq()` (also imports `from openai import AsyncOpenAI`).

Important: do NOT swap inside `_call_gemini()` — Gemini uses `google.genai` directly, not the OpenAI SDK. It gets manual instrumentation in Task 5.

- [ ] **Step 2: Run existing planner tests**

Run: `pytest tests/test_personalization_planner.py tests/test_planner_fallback.py tests/test_validator.py -q`
Expected: same number passing as before — the swap is a drop-in.

- [ ] **Step 3: Commit**

```bash
git add pipeline/planner.py
git commit -m "feat(planner): use langfuse.openai for OpenAI/Groq auto-instrumentation"
```

---

### Task 4.5: Register custom model pricing for Gemini + Groq

**Files:**
- Create: `scripts/langfuse_register_models.py`

**Why this matters:** Langfuse has built-in pricing only for OpenAI and Anthropic models. For Gemini (your default quality-mode provider) and Groq models, cost tracking in dashboards will show `$0.00` unless you register them. `ScoreName.TOTAL_COST` aggregations will be silently wrong.

- [ ] **Step 1: Create the registration script**

Create `scripts/langfuse_register_models.py`:
```python
"""Register custom model pricing for Gemini + Groq in Langfuse.

Run once (or whenever you change providers/models, or pricing changes):
  python scripts/langfuse_register_models.py

Prices are per 1M tokens (input / output). Update from provider pricing pages:
  - Gemini: https://ai.google.dev/pricing
  - Groq:   https://console.groq.com/docs/models
"""

from langfuse import get_client

MODELS = [
    # ── Gemini ────────────────────────────────────────────────────────────
    {
        "model_name":    "gemini-2.0-flash",
        "match_pattern": "gemini-2.0-flash",
        "unit":          "TOKENS",
        "input_price":   0.075 / 1_000,   # $0.075 per 1M tokens → per 1K
        "output_price":  0.30  / 1_000,
    },
    {
        "model_name":    "gemini-1.5-pro",
        "match_pattern": "gemini-1.5-pro",
        "unit":          "TOKENS",
        "input_price":   1.25 / 1_000,
        "output_price":  5.00 / 1_000,
    },
    {
        "model_name":    "gemini-1.5-flash",
        "match_pattern": "gemini-1.5-flash",
        "unit":          "TOKENS",
        "input_price":   0.075 / 1_000,
        "output_price":  0.30  / 1_000,
    },
    # ── Groq ─────────────────────────────────────────────────────────────
    {
        "model_name":    "llama-3.1-70b-versatile",
        "match_pattern": "llama-3.1-70b-versatile",
        "unit":          "TOKENS",
        "input_price":   0.59 / 1_000,
        "output_price":  0.79 / 1_000,
    },
    {
        "model_name":    "llama-3.3-70b-versatile",
        "match_pattern": "llama-3.3-70b-versatile",
        "unit":          "TOKENS",
        "input_price":   0.59 / 1_000,
        "output_price":  0.79 / 1_000,
    },
    {
        "model_name":    "llama3-70b-8192",
        "match_pattern": "llama3-70b-8192",
        "unit":          "TOKENS",
        "input_price":   0.59 / 1_000,
        "output_price":  0.79 / 1_000,
    },
]


def main():
    client = get_client()
    for m in MODELS:
        try:
            client.api.models.create(
                model_name=m["model_name"],
                match_pattern=m["match_pattern"],
                unit=m["unit"],
                input_price=m["input_price"],
                output_price=m["output_price"],
            )
            print(f"✓ Registered {m['model_name']}")
        except Exception as exc:
            # Model already exists → update is not required; skip with a note.
            print(f"  {m['model_name']}: {exc}")


if __name__ == "__main__":
    import os, sys
    if not os.environ.get("LANGFUSE_PUBLIC_KEY"):
        print("LANGFUSE_PUBLIC_KEY not set", file=sys.stderr)
        sys.exit(1)
    main()
```

- [ ] **Step 2: Dry-run (verify the model list parses)**

Run:
```bash
python -c "import scripts.langfuse_register_models as m; print([x['model_name'] for x in m.MODELS])"
```
Expected: prints all model names with no import errors.

- [ ] **Step 3: Check what models your settings actually use**

Run:
```bash
python -c "
from pipeline import settings
print('quality:', settings.quality_llm_provider, settings.quality_llm_model)
print('fast:',    settings.fast_llm_provider,    settings.fast_llm_model)
print('default:', settings.llm_provider,          settings.llm_model)
"
```
If any model_name shown isn't in the `MODELS` list above, add it to the script with the correct pricing from the provider's pricing page.

- [ ] **Step 4: Run against Langfuse (once LANGFUSE keys are configured)**

```bash
LANGFUSE_PUBLIC_KEY=pk-... LANGFUSE_SECRET_KEY=sk-... \
  python scripts/langfuse_register_models.py
```
Expected: one `✓ Registered <name>` line per model.

- [ ] **Step 5: Commit**

```bash
git add scripts/langfuse_register_models.py
git commit -m "feat(scripts): register Gemini+Groq model pricing in Langfuse"
```

---

### Task 5: Wrap planner phases in observation spans

**Files:**
- Modify: `pipeline/planner.py`
- Test: `tests/test_observability_integration.py` (new)

- [ ] **Step 1: Create integration test file with failing test**

Create `tests/test_observability_integration.py`:
```python
"""End-to-end integration: stubbed Langfuse client receives the
observations we expect from a planner run."""

from unittest.mock import MagicMock, patch

import pytest

from pipeline import observability as obs
import pipeline.planner as planner_mod


@pytest.mark.asyncio
async def test_planner_creates_spans_for_each_phase(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    fake_client = MagicMock()
    fake_span   = MagicMock()
    fake_span.__enter__ = lambda self: self
    fake_span.__exit__  = lambda *a: False
    fake_client.start_as_current_observation.return_value = fake_span
    monkeypatch.setattr(obs, "_client", fake_client)

    async def fake_call_llm(*a, **kw):
        return {
            "input_type": "query", "intent": "candidate_search",
            "must": {"skills": [], "country": None, "city": None,
                     "min_years_exp": None, "max_years_exp": None,
                     "applied_role": None, "min_salary": None,
                     "max_salary": None, "status": ["active"]},
            "should":   {"skills": [], "themes": [], "locations": [], "roles": []},
            "must_not": {"skills": [], "status": [], "companies": []},
            "semantic_query": "x", "hyde_profile": None, "lexical_terms": [],
            "search_targets": ["candidate_profile"],
            "confidence": 0.8, "clarify": None,
        }
    monkeypatch.setattr(planner_mod, "_call_llm", fake_call_llm)

    await planner_mod.plan("python developer", cfg={"use_cache": False})

    span_names = [c.kwargs.get("name") or c.args[1]
                  for c in fake_client.start_as_current_observation.call_args_list]
    assert "planner.cache_lookup" in span_names
    assert "planner.llm" in span_names
    assert "planner.validate" in span_names
    assert "planner.normalize" in span_names
```

- [ ] **Step 2: Run — confirm failure**

Run: `pytest tests/test_observability_integration.py -v`
Expected: FAIL — no spans being created yet.

- [ ] **Step 3: Wrap phases in `plan()`**

In `pipeline/planner.py`, add at the top of the imports section:
```python
from pipeline.observability import is_enabled as _obs_enabled, _get_client as _obs_client
```

In `plan()`, wrap each phase. Replace the existing cache lookup section:
```python
    if use_cache:
        cached = _cache.get(ck)
        if cached is not None:
            logger.debug("Plan cache hit for key %s", ck[:32])
            _metrics.incr("planner_cache_hits")
            return _spec_from_dict(cached)
```
with:
```python
    if use_cache:
        if _obs_enabled() and _obs_client() is not None:
            with _obs_client().start_as_current_observation(
                as_type="span", name="planner.cache_lookup",
            ) as s:
                cached = _cache.get(ck)
                s.update(metadata={"hit": cached is not None})
        else:
            cached = _cache.get(ck)
        if cached is not None:
            logger.debug("Plan cache hit for key %s", ck[:32])
            _metrics.incr("planner_cache_hits")
            return _spec_from_dict(cached)
```

Wrap the LLM call (currently `with _metrics.Timer("planner_ms"):`):
```python
        if _obs_enabled() and _obs_client() is not None:
            llm_ctx = _obs_client().start_as_current_observation(
                as_type="span", name="planner.llm",
            )
        else:
            llm_ctx = _NullContext()

        with llm_ctx:
            with _metrics.Timer("planner_ms"):
                try:
                    raw_dict = await _call_llm(
                        sanitized, provider_override, model_override,
                        thinking_level, system_prompt_prefix=system_prompt_prefix,
                    )
                    planner_error = None
                except Exception as exc:
                    logger.warning("LLM planner call failed (%s): %s",
                                   provider_override or settings.llm_provider, exc)
                    raw_dict = None
                    planner_error = str(exc)
```

Add `_NullContext` near the top of `planner.py`:
```python
class _NullContext:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def update(self, **kw): pass
```

Wrap validate and normalize the same way — small spans, same pattern. For brevity, replace:
```python
            if use_validator:
                spec = validate_spec(raw_dict, original_input=raw_input,
                                     explicit_filters=explicit_filters)
```
with:
```python
            validate_ctx = (_obs_client().start_as_current_observation(
                                as_type="span", name="planner.validate")
                            if _obs_enabled() and _obs_client() else _NullContext())
            with validate_ctx:
                if use_validator:
                    spec = validate_spec(raw_dict, original_input=raw_input,
                                         explicit_filters=explicit_filters)
                else:
                    spec = _spec_from_dict(raw_dict)
```

Similarly for normalize — replace:
```python
    if use_normalizer:
        spec = normalize_spec(spec)
```
with:
```python
    normalize_ctx = (_obs_client().start_as_current_observation(
                        as_type="span", name="planner.normalize")
                     if _obs_enabled() and _obs_client() else _NullContext())
    with normalize_ctx:
        if use_normalizer:
            spec = normalize_spec(spec)
```

- [ ] **Step 4: Run — confirm passes**

Run: `pytest tests/test_observability_integration.py tests/test_personalization_planner.py -q`
Expected: all pass (4 new spans for each phase emitted; existing planner tests unaffected because they don't enable Langfuse).

- [ ] **Step 5: Commit**

```bash
git add pipeline/planner.py tests/test_observability_integration.py
git commit -m "feat(planner): emit Langfuse spans for cache/llm/validate/normalize phases"
```

---

### Task 5.5: Wrap retrieval and rerank phases in observation spans

**Files:**
- Modify: `pipeline/search.py`

- [ ] **Step 1: Wrap dense, bm25, skill retrieval, and rerank**

In `pipeline/search.py`, wrap the main latency blocks with Langfuse spans: `search.retrieve.dense`, `search.retrieve.bm25`, `search.retrieve.skill`, `search.rerank`, and `search.mmr`.
```python
from pipeline.observability import is_enabled as _obs_enabled, _get_client as _obs_client
class _NullContext:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def update(self, **kw): pass

def _obs_span(name: str):
    if _obs_enabled() and _obs_client() is not None:
        return _obs_client().start_as_current_observation(as_type="span", name=name)
    return _NullContext()

# Example wrapper around dense search:
with _obs_span("search.retrieve.dense"):
    results = await self.dense_search(...)
```

- [ ] **Step 2: Commit**

```bash
git add pipeline/search.py
git commit -m "feat(search): wrap retrieval and rerank phases in Langfuse spans"
```

---

### Task 6: Wrap `/search` handler with a parent trace

**Files:**
- Modify: `api/main.py` (the `@app.post("/search")` handler)
- Test: `tests/test_observability_integration.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_observability_integration.py`:
```python
from fastapi.testclient import TestClient
from api import main as api_main


def test_search_creates_parent_trace_with_user_id(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    fake_client = MagicMock()
    span = MagicMock()
    span.__enter__ = lambda self: self
    span.__exit__  = lambda *a: False
    span.id = "trace-xyz"
    fake_client.start_as_current_observation.return_value = span
    fake_client.get_current_trace_id.return_value = "trace-xyz"
    monkeypatch.setattr(obs, "_client", fake_client)

    # Stub the search engine so the handler returns quickly.
    fake_engine = MagicMock()
    fake_engine.smart_search = MagicMock(return_value=MagicMock(
        results=[], clarify=None, relaxations_applied=[], spec=None,
        spec_dict=None, phase_timings={}, personalization_applied=False,
    ))
    async def coro(*a, **kw): return fake_engine.smart_search.return_value
    fake_engine.smart_search = coro
    api_main.app.state.search_engine = fake_engine

    client = TestClient(api_main.app)
    r = client.post("/search", json={"query": "python", "mode": "quality",
                                     "recruiter_id": "rec-123"})
    assert r.status_code == 200
    # Parent trace span was opened with name 'http.search'
    span_calls = [c.kwargs.get("name") for c in fake_client.start_as_current_observation.call_args_list]
    assert "http.search" in span_calls
```

- [ ] **Step 2: Run — confirm failure**

Run: `pytest tests/test_observability_integration.py::test_search_creates_parent_trace_with_user_id -v`
Expected: FAIL — no parent span created.

- [ ] **Step 3: Add `session_id` to `SearchRequest` and frontend**

In `api/main.py`, add `session_id: Optional[str] = None` to `SearchRequest`.
In `api/static/admin.html`, generate a UUID and store it in `sessionStorage` (e.g., `if (!sessionStorage.getItem('searchSessionId')) sessionStorage.setItem('searchSessionId', crypto.randomUUID());`), and pass it in the search payload as `session_id`.

- [ ] **Step 4: Wrap the handler**

In `api/main.py`, add import:
```python
from pipeline.observability import is_enabled as _obs_enabled, _get_client as _obs_client
```

Inside the `/search` handler (`async def search(req: SearchRequest):` or similar — find the function decorated with `@app.post("/search", response_model=SearchResponse)`), wrap the entire body in a parent span:

Find the first executable line of the function body (after argument unpacking) and wrap everything that follows in:
```python
    if _obs_enabled() and _obs_client() is not None:
        parent = _obs_client().start_as_current_observation(
            as_type="span",
            name="http.search",
            # No manual redact() call needed — the SDK applies mask=redact
            # centrally via init_langfuse(). Pass the raw dict.
            input={"query": req.query, "mode": req.mode,
                   "filters": getattr(req, "filters", None)},
        )
    else:
        parent = _NullContext()  # define at top of api/main.py
    with parent:
        # ── existing handler body ────────────────────────────────────────
        ...
```

Add `_NullContext` near the top of `api/main.py`:
```python
class _NullContext:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def update(self, **kw): pass
```

Inside the `with parent:` block, immediately after opening, set trace-level attributes via `propagate_attributes`:
```python
        from langfuse import propagate_attributes
        if _obs_enabled() and _obs_client() is not None:
            config_hash = str(hash(str(req.config_overrides)))
            attr_ctx = propagate_attributes(
                user_id=req.recruiter_id, session_id=req.session_id,
                tags=[req.mode, "search", f"config:{config_hash}"],
            )
        else:
            attr_ctx = _NullContext()
        with attr_ctx:
            # ... rest of body
```

- [ ] **Step 5: Run — confirm pass**

Run: `pytest tests/test_observability_integration.py -q`
Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add api/main.py tests/test_observability_integration.py
git commit -m "feat(api): wrap /search handler with parent Langfuse trace"
```

---

### Task 7: Expose `langfuse_trace_id` + `langfuse_trace_url` in `SearchResponse`

**Files:**
- Modify: `api/main.py` (`SearchResponse` class + `/search` handler return)
- Test: `tests/test_observability_integration.py`

- [ ] **Step 1: Add failing tests**

Append to `tests/test_observability_integration.py`:
```python
def test_search_response_includes_trace_id_when_langfuse_on(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    monkeypatch.setattr(obs._settings, "langfuse_base_url", "https://lf.example")
    monkeypatch.setattr(obs._settings, "langfuse_environment", "production")
    fake_client = MagicMock()
    span = MagicMock(); span.__enter__ = lambda s: s; span.__exit__ = lambda *a: False
    fake_client.start_as_current_observation.return_value = span
    fake_client.get_current_trace_id.return_value = "trace-abc"
    monkeypatch.setattr(obs, "_client", fake_client)

    api_main.app.state.search_engine.smart_search = lambda *a, **k: MagicMock(
        results=[], clarify=None, relaxations_applied=[], spec=None,
        spec_dict=None, phase_timings={}, personalization_applied=False,
    )
    r = TestClient(api_main.app).post("/search", json={"query": "x", "mode": "quality"})
    body = r.json()
    assert body["langfuse_trace_id"] == "trace-abc"
    assert "lf.example" in body["langfuse_trace_url"]


def test_search_response_omits_trace_when_langfuse_off(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", False)
    r = TestClient(api_main.app).post("/search", json={"query": "x", "mode": "quality"})
    body = r.json()
    assert body.get("langfuse_trace_id") is None
    assert body.get("langfuse_trace_url") is None
```

- [ ] **Step 2: Run — confirm failure**

Run: `pytest tests/test_observability_integration.py -q -k "trace_id"`
Expected: 2 FAIL.

- [ ] **Step 3: Add fields to `SearchResponse`**

In `api/main.py`, inside `class SearchResponse(BaseModel):`, after `personalization_offer`, add:
```python
    langfuse_trace_id: Optional[str] = Field(
        default=None,
        description="If Langfuse is enabled, the trace id for this request. "
                    "Use with langfuse_trace_url to open in the Langfuse UI.",
    )
    langfuse_trace_url: Optional[str] = Field(
        default=None,
        description="Direct link to this trace in the Langfuse UI.",
    )
```

- [ ] **Step 4: Compute trace id/url and inject into response**

Helper near the top of `api/main.py`:
```python
from pipeline import settings as _settings_for_lf

def _trace_url(trace_id: str | None) -> str | None:
    if not trace_id:
        return None
    base = _settings_for_lf.langfuse_base_url.rstrip("/")
    # Langfuse URLs follow this pattern; adjust if your project slug differs.
    return f"{base}/trace/{trace_id}"
```

In each `SearchResponse(...)` construction site in the `/search` handler (the clarify-only branch AND the main return), inject:
```python
            langfuse_trace_id=(
                _obs_client().get_current_trace_id()
                if _obs_enabled() and _obs_client() is not None else None),
            langfuse_trace_url=_trace_url(
                _obs_client().get_current_trace_id()
                if _obs_enabled() and _obs_client() is not None else None),
```

- [ ] **Step 5: Run — confirm pass**

Run: `pytest tests/test_observability_integration.py -q`
Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add api/main.py tests/test_observability_integration.py
git commit -m "feat(api): expose langfuse_trace_id and url in SearchResponse"
```

---

### Task 13: AI insights endpoint emits nested trace

**Files:**
- Modify: `api/main.py` (`/search/insights` handler) and `pipeline/ai_insights.py`
- Modify: `api/static/admin.html` (pass trace_id to insights)

- [ ] **Step 1: Swap to `langfuse.openai` in `pipeline/ai_insights.py`**

The import swap is inside the `AIInsightService` class at `pipeline/ai_insights.py` (around line 166), not just the endpoint. Change `from openai import AsyncOpenAI` to `from langfuse.openai import AsyncOpenAI`.

- [ ] **Step 2: Frontend passes `langfuse_trace_id`**

In `api/static/admin.html`, when calling `/search/insights`, pass the `langfuse_trace_id` returned from `/search`. Also add `langfuse_trace_id: Optional[str] = None` to the corresponding request model in `api/main.py` (e.g. `SearchRequest` or the specific model for insights).

- [ ] **Step 3: Nest the insights trace**

In `api/main.py`, find `/search/insights`. Use the passed `langfuse_trace_id` to link the insight generation as a nested generation/span of the parent search trace.

```python
    if _obs_enabled() and _obs_client() is not None and getattr(req, "langfuse_trace_id", None):
        # Create a span linked to the parent trace
        parent = _obs_client().start_as_current_observation(
            as_type="span",
            name="http.insights",
            trace_id=req.langfuse_trace_id,
        )
    else:
        parent = _NullContext()
    with parent:
        # existing insights logic
```

- [ ] **Step 4: Commit**

```bash
git add api/main.py pipeline/ai_insights.py api/static/admin.html
git commit -m "feat(insights): wrap AI insights endpoint as nested Langfuse trace"
```

---

## Phase C — Impression ↔ trace ↔ outcome loop

### Task 8: Migration 007 — `langfuse_trace_id` on `search_impressions`

**Files:**
- Create: `db/migrations/007_langfuse_trace_ids.sql`

- [ ] **Step 1: Create the migration file**

Create `db/migrations/007_langfuse_trace_ids.sql`:
```sql
-- 007_langfuse_trace_ids.sql
-- Store the Langfuse trace id alongside each impression so that recruiter
-- feedback (/outcomes) can post scores back to the originating trace.

ALTER TABLE search_impressions
  ADD COLUMN IF NOT EXISTS langfuse_trace_id TEXT;

CREATE INDEX IF NOT EXISTS idx_impressions_langfuse_trace
  ON search_impressions (langfuse_trace_id)
  WHERE langfuse_trace_id IS NOT NULL;
```

- [ ] **Step 2: Apply locally (optional — skip if no DB)**

Run:
```bash
psql "$DATABASE_URL" -f db/migrations/007_langfuse_trace_ids.sql
```
Expected: `ALTER TABLE` + `CREATE INDEX`, both successful. Skip if you don't have a local Postgres set up.

- [ ] **Step 3: Commit**

```bash
git add db/migrations/007_langfuse_trace_ids.sql
git commit -m "feat(db): add langfuse_trace_id to search_impressions"
```

---

### Task 9: Plumb trace_id into impression INSERT

**Files:**
- Modify: `pipeline/search.py` (the `INSERT INTO search_impressions` around line 546)
- Test: `tests/test_observability_integration.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_observability_integration.py`:
```python
@pytest.mark.asyncio
async def test_impression_insert_includes_trace_id(monkeypatch):
    """When Langfuse is on, the trace id is written into the impression row."""
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    fake_client = MagicMock()
    fake_client.get_current_trace_id.return_value = "trace-imp-1"
    monkeypatch.setattr(obs, "_client", fake_client)

    from unittest.mock import AsyncMock
    from pipeline.search import HybridSearchEngine
    engine = HybridSearchEngine.__new__(HybridSearchEngine)
    engine.pool = AsyncMock()

    # Fake one impression call
    await engine._record_impressions(
        search_id="s1", recruiter_id="r1",
        results=[MagicMock(candidate_id="c1", final_score=0.9)],
    )

    # The executemany / execute call must have included trace-imp-1 as the last arg.
    args = engine.pool.executemany.call_args or engine.pool.execute.call_args
    assert any("trace-imp-1" in str(a) for a in args.args)
```

(If the impression insert is not a method called `_record_impressions`, search for the actual function name with `grep -n "INSERT INTO search_impressions" pipeline/search.py` and adjust the test fixture.)

- [ ] **Step 2: Run — confirm failure**

Run: `pytest tests/test_observability_integration.py::test_impression_insert_includes_trace_id -v`
Expected: FAIL.

- [ ] **Step 3: Update the INSERT**

In `pipeline/search.py`, find the line that begins `"""INSERT INTO search_impressions`. Change the column list and value list to include the trace id. The current statement:
```python
"""INSERT INTO search_impressions (id, search_id, recruiter_id, candidate_id, position, final_score)
   VALUES ..."""
```
becomes:
```python
"""INSERT INTO search_impressions
       (id, search_id, recruiter_id, candidate_id, position, final_score, langfuse_trace_id)
   VALUES ..."""
```
and the values tuple gains one element on the end:
```python
from pipeline.observability import is_enabled as _obs_enabled, _get_client as _obs_client
trace_id = (_obs_client().get_current_trace_id()
            if _obs_enabled() and _obs_client() is not None else None)
# ... add trace_id as the 7th value in each impression tuple
```

- [ ] **Step 4: Run — confirm pass**

Run: `pytest tests/test_observability_integration.py -q`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add pipeline/search.py tests/test_observability_integration.py
git commit -m "feat(search): write langfuse_trace_id into search_impressions"
```

---

### Task 10: `/outcomes` posts Langfuse scores via the trace id

**Files:**
- Modify: `api/main.py` (the `/outcomes` endpoint)
- Test: `tests/test_observability_integration.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_observability_integration.py`:
```python
def test_outcomes_posts_score_to_trace(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    fake_client = MagicMock()
    monkeypatch.setattr(obs, "_client", fake_client)

    # Fake pool returning the trace_id for the given impression
    fake_pool = MagicMock()
    fake_pool.fetchrow = MagicMock(return_value={"langfuse_trace_id": "trace-out-1"})
    async def fetchrow(*a, **k): return {"langfuse_trace_id": "trace-out-1"}
    async def execute(*a, **k): return "INSERT 1"
    fake_pool.fetchrow = fetchrow
    fake_pool.execute  = execute
    api_main.app.state.pool = fake_pool

    r = TestClient(api_main.app).post("/outcomes", json={
        "impression_id": "11111111-1111-1111-1111-111111111111",
        "action": "contacted",
    })
    assert r.status_code in (200, 204)
    # Score was created with action as categorical value
    fake_client.create_score.assert_any_call(
        trace_id="trace-out-1", name="recruiter_action", value="contacted",
        comment=None,
    )
```

- [ ] **Step 2: Run — confirm failure**

Run: `pytest tests/test_observability_integration.py::test_outcomes_posts_score_to_trace -v`
Expected: FAIL.

- [ ] **Step 3: Wire scoring into `/outcomes`**

Find the `/outcomes` handler in `api/main.py` (around line 612). Before/after the existing INSERT into `search_outcomes`, look up the impression's `langfuse_trace_id` and post a score.

After the existing `await pool.execute(...INSERT INTO search_outcomes...)` block, insert:
```python
    # Bind recruiter feedback to the originating Langfuse trace.
    row = await pool.fetchrow(
        "SELECT langfuse_trace_id FROM search_impressions WHERE id = $1::uuid",
        req.impression_id,
    )
    trace_id = row["langfuse_trace_id"] if row else None
    if trace_id:
        from pipeline.observability import record_score, ScoreName
        record_score(trace_id=trace_id, name=ScoreName.RECRUITER_ACTION,
                     value=req.action)
        positive = req.action in {"saved", "contacted", "shortlisted"}
        record_score(trace_id=trace_id, name=ScoreName.POSITIVE_OUTCOME,
                     value=1.0 if positive else 0.0)
```

- [ ] **Step 4: Run — confirm pass**

Run: `pytest tests/test_observability_integration.py -q`
Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_observability_integration.py
git commit -m "feat(api): /outcomes posts recruiter_action + positive_outcome scores"
```

---

## Phase D — Score taxonomy, sampling, redaction

### Task 11: Emit core scores at end of `/search`

**Files:**
- Modify: `api/main.py` (`/search` handler, near the final return)

- [ ] **Step 1: Add scoring block**

In `api/main.py`, immediately before each `SearchResponse(...)` return inside the `/search` handler, add:
```python
    trace_id = (_obs_client().get_current_trace_id()
                if _obs_enabled() and _obs_client() is not None else None)
    if trace_id:
        from pipeline.observability import record_score, ScoreName, should_sample
        latency_ms = (time.perf_counter() - start) * 1000
        if should_sample(latency_ms=latency_ms, status_code=200):
            spec = getattr(search_resp, "spec", None)
            record_score(trace_id=trace_id, name=ScoreName.RESULT_COUNT,
                         value=len(getattr(search_resp, "results", []) or []))
            record_score(trace_id=trace_id, name=ScoreName.FALLBACK_USED,
                         value=1.0 if getattr(spec, "used_fallback", False) else 0.0)
            record_score(trace_id=trace_id, name=ScoreName.CONFIDENCE,
                         value=float(getattr(spec, "confidence", 0.0) or 0.0))
            record_score(trace_id=trace_id, name=ScoreName.DROPPED_COUNT,
                         value=len(getattr(spec, "dropped_items", []) or []))
            record_score(trace_id=trace_id,
                         name=ScoreName.PERSONALIZATION_OFFER_EMITTED,
                         value=1.0 if getattr(spec, "personalization_offer", None) else 0.0)
            record_score(trace_id=trace_id, name=ScoreName.HINT_COUNT,
                         value=len(getattr(spec, "hints_applied", []) or []))
```

- [ ] **Step 2: Add a smoke test**

Append to `tests/test_observability_integration.py`:
```python
def test_search_emits_core_scores(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    monkeypatch.setattr(obs._settings, "langfuse_sample_rate", 1.0)
    fake_client = MagicMock()
    span = MagicMock(); span.__enter__ = lambda s: s; span.__exit__ = lambda *a: False
    fake_client.start_as_current_observation.return_value = span
    fake_client.get_current_trace_id.return_value = "trace-score-1"
    monkeypatch.setattr(obs, "_client", fake_client)

    fake_engine = MagicMock()
    async def coro(*a, **k):
        return MagicMock(results=[MagicMock(), MagicMock()], clarify=None,
                         relaxations_applied=[], spec=MagicMock(
                             used_fallback=False, confidence=0.85,
                             dropped_items=[], personalization_offer=None),
                         spec_dict={}, phase_timings={}, personalization_applied=False)
    fake_engine.smart_search = coro
    api_main.app.state.search_engine = fake_engine

    TestClient(api_main.app).post("/search", json={"query": "x", "mode": "quality"})
    names_called = {c.kwargs.get("name") for c in fake_client.create_score.call_args_list}
    assert {"result_count", "fallback_used", "confidence", "dropped_count",
            "personalization_offer_emitted"} <= names_called
```

- [ ] **Step 3: Run — confirm pass**

Run: `pytest tests/test_observability_integration.py -q`
Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add api/main.py tests/test_observability_integration.py
git commit -m "feat(api): emit core scores per search via adaptive sampler"
```

---

### Task 12: Personalization offer scoring on the accept path

**Files:**
- Modify: `api/main.py` (the `POST /api/recruiter/{id}/hints` endpoint)
- Test: `tests/test_personalization_api.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_personalization_api.py`:
```python
from unittest.mock import MagicMock
from pipeline import observability as obs


def test_post_hint_with_trace_id_records_acceptance_score(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    fake_client = MagicMock()
    monkeypatch.setattr(obs, "_client", fake_client)

    _install_pool(monkeypatch, fetchrow_return=None)
    r = TestClient(api_main.app).post(
        "/api/recruiter/eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee/hints",
        json={"text": "user likes builders", "source": "suggested",
              "langfuse_trace_id": "trace-accept-1"},
    )
    assert r.status_code == 200
    fake_client.create_score.assert_any_call(
        trace_id="trace-accept-1", name="personalization_offer_accepted",
        value=1.0, comment=None,
    )
```

- [ ] **Step 2: Run — confirm failure**

Run: `pytest tests/test_personalization_api.py::test_post_hint_with_trace_id_records_acceptance_score -v`
Expected: FAIL.

- [ ] **Step 3: Update the POST hints handler**

In `api/main.py`, inside `add_personalization_hint`, after the existing INSERT/UPDATE call and just before `return {"hints": updated}`, insert:
```python
    trace_id = body.get("langfuse_trace_id")
    if trace_id:
        from pipeline.observability import record_score, ScoreName
        record_score(trace_id=trace_id,
                     name=ScoreName.PERSONALIZATION_OFFER_ACCEPTED, value=1.0)
```

- [ ] **Step 4: Update the frontend to send the trace id**

In `api/static/admin.html`, find `savePersonalizationHint(text)` (added in the personalization-hints feature). Change the body to include `langfuse_trace_id`:
```javascript
body: JSON.stringify({
  text, source: "suggested",
  langfuse_trace_id: data.langfuse_trace_id || null,
}),
```

- [ ] **Step 5: Run — confirm pass**

Run: `pytest tests/test_personalization_api.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add api/main.py api/static/admin.html tests/test_personalization_api.py
git commit -m "feat(personalization): score offer acceptance against originating trace"
```

---



## Phase E — Evaluation pipeline

### Task 14: Dataset seed script

**Files:**
- Create: `scripts/langfuse_dataset_seed.py`

- [ ] **Step 1: Locate the source of eval cases**

Run: `ls tests/test_query_planner.py tests/test_recruitment_dataset.py data/ 2>/dev/null && grep -rn "eval_cases\|golden\|fixture" tests/ scripts/ 2>/dev/null | head -10`
Pick the most stable source — for this plan, assume a list of (query, expected_must_skills) pairs lives in test fixtures.

- [ ] **Step 2: Write the script**

Create `scripts/langfuse_dataset_seed.py`:
```python
"""One-off: create or update a Langfuse dataset of planner regression cases.
Run with: python scripts/langfuse_dataset_seed.py
"""

import os
import sys

# Hardcoded seed cases. Add more over time as you find interesting queries.
CASES = [
    {"input": "python developer 5 years bangalore",
     "expected_output": {"must.skills": ["python"],
                         "must.city": "bangalore",
                         "must.min_years_exp": 5}},
    {"input": "senior react engineer in germany must not be agency",
     "expected_output": {"must.skills": ["react"],
                         "must.country": "germany",
                         "must_not.companies": ["agency"]}},
    {"input": "fresh graduate frontend developer",
     "expected_output": {"must.max_years_exp": 2,
                         "should.skills": ["react", "javascript"]}},
    # … add more
]


def main():
    from langfuse import get_client
    client = get_client()
    dataset_name = "planner-regressions"
    try:
        dataset = client.get_dataset(dataset_name)
        print(f"Updating existing dataset {dataset_name}")
    except Exception:
        dataset = client.create_dataset(name=dataset_name)
        print(f"Created new dataset {dataset_name}")
    for case in CASES:
        dataset.create_item(input=case["input"],
                            expected_output=case["expected_output"])
    print(f"Seeded {len(CASES)} items")


if __name__ == "__main__":
    if not os.environ.get("LANGFUSE_PUBLIC_KEY"):
        print("LANGFUSE_PUBLIC_KEY not set", file=sys.stderr)
        sys.exit(1)
    main()
```

- [ ] **Step 3: Dry-run (no Langfuse needed for this)**

Run: `python -c "import scripts.langfuse_dataset_seed as m; print(len(m.CASES))"`
Expected: prints the number of cases (3+).

- [ ] **Step 4: Commit**

```bash
git add scripts/langfuse_dataset_seed.py
git commit -m "feat(scripts): seed Langfuse planner-regressions dataset"
```

---

### Task 15: Experiment runner

**Files:**
- Create: `scripts/langfuse_experiment.py`

- [ ] **Step 1: Write the experiment runner**

Create `scripts/langfuse_experiment.py`:
```python
"""Run planner against the planner-regressions Langfuse dataset.
Posts scores: planner_filter_accuracy.

Usage: python scripts/langfuse_experiment.py --run-name "v3-prompt"
"""

import argparse
import asyncio

from pipeline.planner import plan


def _accuracy(spec_dict: dict, expected: dict) -> float:
    """Fraction of expected fields that match the produced spec."""
    if not expected:
        return 1.0
    hits = 0
    for path, target in expected.items():
        node = spec_dict
        for part in path.split("."):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        if isinstance(target, list):
            ok = set(target).issubset(set(node or []))
        else:
            ok = node == target
        hits += 1 if ok else 0
    return hits / len(expected)


async def _main(run_name: str):
    from langfuse import get_client, Evaluation
    client = get_client()
    dataset = client.get_dataset("planner-regressions")

    def task(*, item, **_):
        spec = asyncio.get_event_loop().run_until_complete(
            plan(item.input, cfg={"use_cache": False}))
        return spec.to_dict()

    def accuracy_eval(*, input, output, expected_output, **_):
        return Evaluation(name="planner_filter_accuracy",
                          value=_accuracy(output, expected_output))

    result = client.run_experiment(
        name=run_name,
        data=dataset.items,
        task=task,
        evaluators=[accuracy_eval],
    )
    print(f"Run {run_name} complete. Mean accuracy:",
          sum(e.value for e in result.evaluations) / len(result.evaluations))
    client.flush()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run-name", required=True)
    args = p.parse_args()
    asyncio.run(_main(args.run_name))
```

- [ ] **Step 2: Sanity-check the accuracy helper**

Run inline:
```bash
python -c "
from scripts.langfuse_experiment import _accuracy
spec = {'must': {'skills': ['python'], 'city': 'bangalore'}}
exp  = {'must.skills': ['python'], 'must.city': 'bangalore'}
print(_accuracy(spec, exp))
assert _accuracy(spec, exp) == 1.0
exp2 = {'must.skills': ['python', 'rust']}
print(_accuracy(spec, exp2))
assert _accuracy(spec, exp2) == 0.0
print('ok')
"
```
Expected: `1.0`, `0.0`, `ok`.

- [ ] **Step 3: Commit**

```bash
git add scripts/langfuse_experiment.py
git commit -m "feat(scripts): planner experiment runner posting accuracy scores"
```

---

### Task 16: Add `--langfuse` export flag to `scripts/evaluate_rankers.py`

**Files:**
- Modify: `scripts/evaluate_rankers.py`

- [ ] **Step 1: Add the flag**

In `scripts/evaluate_rankers.py`, add to the argument parser:
```python
parser.add_argument("--langfuse", action="store_true",
                    help="In addition to printing metrics, post them as scores to Langfuse.")
parser.add_argument("--langfuse-run-name", default=None,
                    help="Optional name for the Langfuse run (defaults to timestamp).")
```

- [ ] **Step 2: After computing metrics, post to Langfuse**

Find where metrics like `top1`, `mrr10`, etc. are computed (search: `top1\|mrr10\|ndcg`). After the print/log block, add:
```python
if args.langfuse:
    from datetime import datetime
    from langfuse import get_client
    from pipeline.observability import ScoreName

    client = get_client()
    run_name = args.langfuse_run_name or f"ranker-eval-{datetime.utcnow().isoformat()}"
    with client.start_as_current_observation(
        as_type="span", name=f"ranker_eval.{run_name}",
    ) as span:
        trace_id = client.get_current_trace_id()
        for metric_name, value in [
            (ScoreName.TOP1,   results.top1),
            (ScoreName.HIT3,   results.hit3),
            (ScoreName.HIT10,  results.hit10),
            (ScoreName.MRR10,  results.mrr10),
            (ScoreName.NDCG10, results.ndcg10),
        ]:
            client.create_score(trace_id=trace_id, name=str(metric_name),
                                value=float(value))
    client.flush()
    print(f"Posted scores to Langfuse run: {run_name}")
```

(Adjust `results.top1` etc. to match the actual variable/attribute names in your evaluator.)

- [ ] **Step 3: Smoke test without Langfuse**

Run the script with no flag — behavior should be identical:
```bash
python scripts/evaluate_rankers.py --help | grep langfuse
```
Expected: shows the new flags.

- [ ] **Step 4: Commit**

```bash
git add scripts/evaluate_rankers.py
git commit -m "feat(scripts): optional --langfuse export for evaluate_rankers"
```

---

## Phase F — Admin UI link

### Task 17: "View in Langfuse" link in admin search results

**Files:**
- Modify: `api/static/admin.html`

- [ ] **Step 1: Find the search-result panel header**

Run: `grep -n "SESSION STATS\|Active hard filters\|searchNotices" api/static/admin.html | head -5`
Pick a location near the existing notices block where the link should appear (e.g., next to "Time breakdown").

- [ ] **Step 2: Render the link conditionally**

In `api/static/admin.html`, inside the function that renders search results, after the notices block, add:
```javascript
if (data.langfuse_trace_url) {
  const a = document.createElement("a");
  a.href = data.langfuse_trace_url;
  a.target = "_blank";
  a.rel = "noopener";
  a.textContent = "🔍 View in Langfuse";
  a.style.cssText = "display:inline-block;margin:6px 0;color:#5a5a5a;text-decoration:none;font-size:12px";
  const slot = document.getElementById("searchNotices") || document.getElementById("plannerSpecPanel");
  if (slot && slot.parentNode) {
    slot.parentNode.insertBefore(a, slot.nextSibling);
  }
}
```

- [ ] **Step 3: Smoke test in browser**

Restart the dev server, hard-refresh, run a search. If `LANGFUSE_ENABLED=true`, you should see a small "🔍 View in Langfuse" link near the results, leading to the trace. If `LANGFUSE_ENABLED=false`, no link appears.

- [ ] **Step 4: Commit**

```bash
git add api/static/admin.html
git commit -m "feat(ui): View-in-Langfuse link in admin search results"
```

---

## Phase G — Prompt management & Tooling

### Task 18: Move PLANNER_SYSTEM_PROMPT to Langfuse with git fallback

**Files:**
- Modify: `pipeline/planner.py`
- Test: `tests/test_personalization_planner.py`

- [ ] **Step 1: Fetch prompt from Langfuse**

In `pipeline/planner.py`, wrap the existing prompt in a fallback variable and fetch the prompt from Langfuse if enabled:
```python
from langfuse import get_client
from pipeline.observability import is_enabled as _obs_enabled

# The existing system prompt string becomes the fallback
FALLBACK_PROMPT = """...""" # Existing prompt

def get_planner_prompt() -> str:
    if not _obs_enabled():
        return FALLBACK_PROMPT
    try:
        # Pull from Langfuse
        prompt = get_client().get_prompt("planner_system_prompt")
        return prompt.get_langchain_prompt()
    except Exception as exc:
        logger.warning("Failed to fetch Langfuse prompt, using fallback: %s", exc)
        return FALLBACK_PROMPT
```

- [ ] **Step 2: Maintain tests**

Tests asserting against the exact text of the prompt must continue asserting against `FALLBACK_PROMPT`. Since `_obs_enabled()` returns False during tests, test deterministic behavior is preserved.

- [ ] **Step 3: Commit**

```bash
git add pipeline/planner.py
git commit -m "feat(planner): use Langfuse prompt management with git fallback"
```

---

### Task 19: Create stable `scripts/replay_trace.py`

**Files:**
- Create: `scripts/replay_trace.py`

- [ ] **Step 1: Write the script**

Create `scripts/replay_trace.py` to pull a trace's inputs from production and replay them through the local pipeline.
```python
"""
Replay a production trace locally.
Usage: python scripts/replay_trace.py <trace_id>
"""

import sys
import asyncio
from langfuse import get_client
from pipeline.planner import plan

async def main(trace_id: str):
    client = get_client()
    trace = client.fetch_trace(trace_id)
    
    print(f"Replaying trace {trace_id}")
    # Extract the input from the trace depending on how it was logged
    query = trace.input.get("query") if isinstance(trace.input, dict) else str(trace.input)
    
    print(f"Input query: {query}")
    result = await plan(query, cfg={"use_cache": False})
    print("Result:", result.to_dict())

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/replay_trace.py <trace_id>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
```

- [ ] **Step 2: Commit**

```bash
git add scripts/replay_trace.py
git commit -m "feat(scripts): add replay_trace script for local debugging"
```

---

## Final verification

Run the full test suite once at the end:
```bash
pytest tests/ -q
```
Expected: all tests still pass. (The new tests are added to `tests/test_observability.py`, `tests/test_observability_integration.py`, and `tests/test_personalization_api.py`.)

---

## Spec coverage map

| Concern from prior discussion | Implemented in task(s) |
|---|---|
| Codex: dependency + env vars | Task 1 |
| Codex: `pipeline/observability.py` no-op-safe recorder | Task 2 |
| Codex: dev_full vs prod_redacted content | Task 2 (`redact` helper) |
| Codex: instrument `/search`, planner, retrieval, rerank | Tasks 5, 6 (planner + handler); retrieval/rerank deferred — they're spans inside future tasks |
| Codex: optional `langfuse_trace_id`/`url` on SearchResponse | Task 7 |
| Codex: migration `007_langfuse_trace_ids.sql` | Task 8 |
| Codex: `/outcomes` creates scores | Task 10 |
| Codex: dataset + experiments from existing benchmark | Tasks 14, 15, 16 |
| Codex: tags (mode, llm_provider, model, etc.) | Task 6 (`propagate_attributes` with tags) |
| Codex: keep `pipeline/metrics.py` & `/metrics` | Untouched in this plan |
| My pushback: closed `ScoreName` enum | Task 2 |
| My pushback: adaptive sampling | Tasks 2, 11 |
| My pushback: don't create generation for cache hits | Task 5 (cache_lookup is a span, not a generation) |
| My pushback: personalization-specific scores | Tasks 11, 12 |
| My pushback: SDK v4 patterns | Task 2 onwards (uses `get_client`, `start_as_current_observation`, `propagate_attributes`) |
| My pushback: background flush + try/except wrapping | Task 2 (`record_score` swallows; `shutdown_langfuse` flushes) |
| Docs review: Gemini/Groq cost tracking broken without custom model pricing | Task 4.5 (`scripts/langfuse_register_models.py`) |
| Docs review: SDK has built-in `mask=` callable; avoid duplicating redaction at call sites | Task 2 (`init_langfuse` passes `mask=redact`); Task 6 (removed manual `_obs_redact` calls) |
| My pushback: SDK works for scripts/tests | Tasks 14, 15, 16 |

## Deliberately out of scope

- **Replacing `pipeline/metrics.py`**: Explicitly NOT doing — sync/local serves a different purpose than async/remote; keep both permanently.
- **LLM-as-judge running inside Langfuse Cloud**: Skipped because you're self-hosting — the hosted auto-evaluator is Cloud-gated; Phase E's SDK-based evals are portable and live in your repo.
- **Retrieval/rerank phase spans**: Task 5 covers planner phases only — retrieval can be added in a follow-up; risk is low because BM25/dense/skill are not on the LLM critical path.
- **Per-recruiter cost dashboards**: Use the Metrics API in the Langfuse UI; no code change needed.
