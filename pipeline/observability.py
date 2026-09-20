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
import time
from enum import StrEnum
from typing import Any

from pipeline import settings as _settings

logger = logging.getLogger(__name__)

_client = None
_initialized = False
_missing_prompt_cache: dict[tuple[str, str], float] = {}


class _NullContext:
    """No-op context manager used wherever Langfuse is disabled or absent.

    Mirrors the subset of the span API call sites use, so callers never
    branch on whether Langfuse is live.
    """
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def update(self, **kw):
        pass


class ScoreName(StrEnum):
    # Request envelope
    REQUEST_LATENCY_MS = "request.latency_ms"
    # Search Outcome
    SEARCH_CLICKED = "search.clicked"
    SEARCH_ZERO_RESULTS = "search.zero_results"
    SEARCH_LATENCY_MS = "search.latency_ms"
    SEARCH_QUERY_UNDERSTANDING_LATENCY_MS = "search.query_understanding_ms"
    SEARCH_RETRIEVAL_LATENCY_MS = "search.retrieval_ms"
    SEARCH_RANKING_LATENCY_MS = "search.ranking_ms"
    SEARCH_DENSE_LATENCY_MS = "search.dense_ms"
    SEARCH_KEYWORD_LATENCY_MS = "search.keyword_ms"
    SEARCH_SKILL_LATENCY_MS = "search.skill_ms"
    SEARCH_COUNT_LATENCY_MS = "search.count_ms"
    SEARCH_ENRICHMENT_LATENCY_MS = "search.enrichment_ms"
    SEARCH_DB_POOL_WAIT_MAX_MS = "search.db_pool_wait.max_ms"
    SEARCH_DB_ROUNDTRIP_MAX_MS = "search.db_roundtrip.max_ms"
    SEARCH_DB_ROUNDTRIP_TOTAL_MS = "search.db_roundtrip.total_ms"
    SEARCH_DB_APP_OVERHEAD_TOTAL_MS = "search.db_app_overhead.total_ms"
    CACHE_HIT_RATE_REQUEST = "cache.hit_rate.request"
    CACHE_L1_HITS_REQUEST = "cache.l1_hits.request"
    CACHE_L2_HITS_REQUEST = "cache.l2_hits.request"
    CACHE_MISSES_REQUEST = "cache.misses.request"
    SEARCH_RESULT_COUNT = "search.result_count"
    SEARCH_TOP_SCORE = "search.top_score"
    # Retrieval relevance (human 👍/👎 labels — ground truth for retrieval quality)
    RETRIEVAL_RELEVANCE = "retrieval.relevance"
    # Personalization
    PERSONALIZATION_ACCEPTED = "personalization.accepted"
    # LLM Metrics
    INSIGHTS_LATENCY_MS = "insights.latency_ms"
    LLM_LATENCY_MS = "llm.latency_ms"
    LLM_FALLBACK_USED = "llm.fallback_used"
    # User feedback (posted from /outcomes)
    RECRUITER_ACTION  = "recruiter_action"
    POSITIVE_OUTCOME  = "positive_outcome"
    # Eval scripts
    PLANNER_FILTER_ACCURACY = "planner_filter_accuracy"
    TOP1  = "top1"
    MRR10 = "mrr10"
    NDCG10 = "ndcg10"
    # Hallucination analysis
    HALLUCINATION_RATE = "hallucination_rate"


def is_enabled() -> bool:
    return bool(_settings.langfuse_enabled)


def custom_export_filter(span) -> bool:
    """Filter to allow standard OTel HTTP/DB spans to be captured by Langfuse."""
    try:
        from langfuse.span_filter import is_default_export_span
        if is_default_export_span(span):
            return True
    except ImportError:
        pass
    scope = getattr(span, "instrumentation_scope", None)
    if scope:
        name = getattr(scope, "name", "")
        if (
            name.startswith("opentelemetry.instrumentation.fastapi")
            or name.startswith("opentelemetry.instrumentation.asyncpg")
            or name.startswith("opentelemetry.instrumentation.urllib3")
            or name.startswith("opentelemetry.instrumentation.httpx")
        ):
            return True
    return False


def init_langfuse() -> None:
    """Initialise the SDK once at app startup. Safe to call when disabled."""
    global _client, _initialized
    if _initialized:
        return
    _initialized = True
    if not is_enabled():
        logger.info("Langfuse Observability: DISABLED (langfuse_enabled=False)")
        return
    
    if not _settings.langfuse_public_key or not _settings.langfuse_secret_key:
        logger.warning("Langfuse Observability: MISCONFIGURED (missing public or secret key). Running in NO-OP mode.")
        return
    try:
        from langfuse import Langfuse
        # Prune noisy per-SQL-statement spans outside of dev: asyncpg
        # auto-instrumentation emits ~20 observations per search that are not
        # useful for product-quality analysis and inflate ingestion volume/cost.
        instrument_db = _settings.langfuse_instrument_db

        def _should_export(span) -> bool:
            scope = getattr(span, "instrumentation_scope", None)
            name = getattr(scope, "name", "") if scope else ""
            if not instrument_db and name.startswith("opentelemetry.instrumentation.asyncpg"):
                return False
            return True

        _client = Langfuse(
            public_key=_settings.langfuse_public_key,
            secret_key=_settings.langfuse_secret_key,
            host=_settings.langfuse_base_url,
            environment=_settings.langfuse_environment,
            release=_settings.langfuse_release or None,
            mask=redact,
            should_export_span=_should_export,
        )
        try:
            from pydantic_ai import Agent
            Agent.instrument_all()
            logger.info("PydanticAI Langfuse instrumentation: ENABLED")
        except Exception as exc:
            logger.debug("PydanticAI instrumentation unavailable: %s", exc)
        logger.info("Langfuse Observability: ENABLED (host=%s env=%s sample_rate=%.2f)",
                    _settings.langfuse_base_url, _settings.langfuse_environment, _settings.langfuse_sample_rate)
    except Exception as exc:
        logger.error("Langfuse Observability: INIT FAILED (%s). Running in NO-OP mode.", exc)
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


def get_current_trace_id() -> str | None:
    """Get the active trace ID from context, if any."""
    if not is_enabled():
        return None
    try:
        client = _get_client()
        trace_id = client.get_current_trace_id() if client else None
        if trace_id:
            return trace_id
            
        # Fallback to OpenTelemetry context for auto-instrumented FastAPI requests
        from opentelemetry import trace
        span = trace.get_current_span()
        if span.get_span_context().is_valid:
            return format(span.get_span_context().trace_id, "032x")
            
        return None
    except Exception:
        return None


def get_trace_url(trace_id: str | None = None) -> str | None:
    """Build a UI link for a trace. No-op (None) when disabled/uninstalled.

    Delegates to the SDK's built-in get_trace_url() which correctly
    includes the project ID in the URL path:
        {base}/project/{project_id}/traces/{trace_id}
    """
    if not is_enabled() or _get_client() is None:
        return None
    try:
        return _get_client().get_trace_url(trace_id=trace_id or get_current_trace_id())
    except Exception as exc:
        logger.debug("Langfuse get_trace_url failed: %s", exc)
        return None


def get_prompt(name: str, label: str = "production") -> Any | None:
    """Fetch a Langfuse managed prompt, or None when unavailable.

    Keeping prompt access behind this facade makes prompt experiments optional:
    the app falls back to the in-code prompt whenever Langfuse is disabled,
    misconfigured, or the requested label does not exist.
    """
    if not is_enabled() or _get_client() is None:
        return None
    cache_key = (str(name), str(label or "production"))
    now = time.time()
    expires_at = _missing_prompt_cache.get(cache_key)
    if expires_at is not None and expires_at > now:
        return None
    try:
        prompt = _get_client().get_prompt(
            name,
            label=label,
            cache_ttl_seconds=300,
        )
        if prompt is None:
            _missing_prompt_cache[cache_key] = now + 60.0
            return None
        _missing_prompt_cache.pop(cache_key, None)
        return prompt
    except Exception as exc:
        _missing_prompt_cache[cache_key] = now + 60.0
        logger.debug("Langfuse get_prompt failed (%s:%s): %s", name, label, exc)
        return None


def trace_attributes(**kwargs):
    """Return a context manager that propagates trace attributes (user_id,
    session_id, tags, …). Returns a no-op context when Langfuse is disabled,
    uninstalled, or failing — so the import never escapes this module.
    """
    if not is_enabled() or _get_client() is None:
        return _NullContext()
    try:
        from langfuse import propagate_attributes
        return propagate_attributes(**kwargs)
    except Exception as exc:
        logger.debug("Langfuse propagate_attributes unavailable: %s", exc)
        return _NullContext()


def start_observation(name: str, *, as_type: str, **kwargs):
    """Return a typed observation context manager, or a no-op context."""
    if not is_enabled() or _get_client() is None:
        return _NullContext()
    try:
        return _get_client().start_as_current_observation(
            as_type=as_type, name=name, **kwargs
        )
    except Exception as exc:
        logger.debug("Langfuse start_observation failed (%s:%s): %s", as_type, name, exc)
        return _NullContext()


def start_span(name: str, **kwargs):
    """Return a span context manager, or a no-op context when Langfuse is
    disabled/uninstalled/failing. Keeps `start_as_current_observation` calls
    out of application code.
    """
    return start_observation(name, as_type="span", **kwargs)


def start_agent(name: str, **kwargs):
    """Return an agent observation context manager, or a no-op context."""
    return start_observation(name, as_type="agent", **kwargs)


def start_tool(name: str, **kwargs):
    """Return a tool observation context manager, or a no-op context."""
    return start_observation(name, as_type="tool", **kwargs)


def start_generation(name: str, **kwargs):
    """Return a generation context manager for LLM calls.

    Generation observations render the prompt view, model name, and token
    counts in Langfuse — use this instead of start_span for LLM call sites.
    """
    return start_observation(name, as_type="generation", **kwargs)


def get_async_openai():
    """Return an AsyncOpenAI class.

    Langfuse-wrapped (auto-instrumenting) when Langfuse is installed, so LLM
    calls show up as generations under the active trace. Falls back to the
    plain `openai` class if Langfuse is unavailable — so OpenAI/Groq calls
    never break just because Langfuse isn't installed.
    """
    if is_enabled():
        try:
            from langfuse.openai import AsyncOpenAI
            return AsyncOpenAI
        except Exception as exc:
            logger.debug("langfuse.openai unavailable, using plain openai: %s", exc)
    from openai import AsyncOpenAI
    return AsyncOpenAI


def update_current_span(**kwargs) -> None:
    """Update the active span's metadata/attributes. No-op + never raises."""
    if not is_enabled() or _get_client() is None:
        return
    try:
        _get_client().update_current_span(**kwargs)
    except Exception as exc:
        logger.debug("Langfuse update_current_span failed: %s", exc)


def update_current_generation(**kwargs) -> None:
    """Update the active generation's metadata/attributes/usage. No-op + never raises."""
    if not is_enabled() or _get_client() is None:
        return
    try:
        _get_client().update_current_generation(**kwargs)
    except Exception as exc:
        logger.debug("Langfuse update_current_generation failed: %s", exc)


def init_otel(app: Any) -> None:
    """Initialise OpenTelemetry instrumentation for FastAPI and asyncpg.

    Safe to call when disabled.
    """
    if not is_enabled() or not _settings.otel_enabled:
        logger.info("OpenTelemetry Instrumentation: DISABLED (langfuse_enabled=False or otel_enabled=False)")
        return

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor

        logger.info("Initializing OpenTelemetry auto-instrumentation...")
        # Instrument FastAPI app
        FastAPIInstrumentor().instrument_app(app)
        # Instrument asyncpg database calls
        AsyncPGInstrumentor().instrument()
        logger.info("OpenTelemetry Instrumentation: ENABLED (FastAPI & asyncpg)")
    except Exception as exc:
        logger.error("OpenTelemetry Instrumentation: INIT FAILED (%s)", exc)


def should_sample(*, latency_ms: float, status_code: int, route: str | None = None) -> bool:
    """Adaptive sampling: errors, slow requests, and specific routes always sampled."""
    if status_code >= 400:
        return True
    if latency_ms >= _settings.langfuse_slow_threshold_ms:
        return True
    # Always sample outcomes to ensure feedback loops work
    if route == "/outcomes":
        return True
    return random.random() < _settings.langfuse_sample_rate


_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def redact(data: Any = None) -> Any:
    """Strip PII from a payload when content_mode=prod_redacted. Recursive."""
    if _settings.langfuse_trace_content_mode == "dev_full":
        return data
    if isinstance(data, dict):
        return {k: redact(v) for k, v in data.items()}
    if isinstance(data, list):
        return [redact(v) for v in data]
    if isinstance(data, str):
        cleaned = _EMAIL_RE.sub("[email-redacted]", data)
        return cleaned[:200]
    return data
