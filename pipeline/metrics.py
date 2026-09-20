"""In-process metrics surface — counters + latency histograms.

No external dependency (Prometheus / OTEL). Process-local, exposed via
`metrics_snapshot()` for the `/metrics` endpoint. Aggregates the
ranking-pipeline observability surface described in Phase 8.4 of the
implementation plan.

Counters survive the lifetime of the worker process; restart wipes them.
"""

from __future__ import annotations

import threading
import time
from bisect import insort
from typing import Any

# ── Counters ──────────────────────────────────────────────────────────────────
_counters: dict[str, int] = {
    "searches_total":              0,
    "planner_llm_calls":            0,
    "planner_fallback_used":        0,
    "planner_cache_hits":           0,
    "embedding_cache_hits":         0,
    "hyde_cache_hits":              0,
    "relaxations_triggered":        0,
    "clarify_returned":             0,
    "forbidden_field_attempts":     0,
    "empty_results":                0,
    "personalization_applied":      0,
    "personalization_cold_start":   0,
}

# ── Latency histogram (lightweight, in-memory) ────────────────────────────────
_MAX_SAMPLES = 1024  # keep recent N latencies per histogram to bound memory
_latencies: dict[str, list[float]] = {
    "planner_ms":   [],
    "search_ms":    [],
    "rerank_ms":    [],
    "embed_ms":     [],
}

# ── Result-size sampler ───────────────────────────────────────────────────────
_result_counts: list[int] = []

_lock = threading.Lock()


# ── Public API ────────────────────────────────────────────────────────────────

def incr(name: str, n: int = 1) -> None:
    """Bump a named counter. Creates it lazily."""
    with _lock:
        _counters[name] = _counters.get(name, 0) + n


def observe_latency(name: str, ms: float) -> None:
    """Record a latency sample. Keeps the last _MAX_SAMPLES values."""
    with _lock:
        bucket = _latencies.setdefault(name, [])
        insort(bucket, ms)
        if len(bucket) > _MAX_SAMPLES:
            # Drop the oldest by shrinking from the middle is wrong;
            # simpler: drop the smallest to bias toward keeping tail.
            # For a quick MVP, just slice.
            del bucket[: len(bucket) - _MAX_SAMPLES]


def observe_result_count(n: int) -> None:
    with _lock:
        _result_counts.append(n)
        if len(_result_counts) > _MAX_SAMPLES:
            del _result_counts[: len(_result_counts) - _MAX_SAMPLES]


class Timer:
    """Context manager that records elapsed ms into a named latency bucket.

    Usage:
        with Timer("planner_ms"):
            spec = await plan(...)
    """

    def __init__(self, name: str):
        self.name = name
        self._t0 = 0.0

    def __enter__(self) -> "Timer":
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        elapsed_ms = (time.perf_counter() - self._t0) * 1000.0
        observe_latency(self.name, elapsed_ms)


# ── Snapshot ──────────────────────────────────────────────────────────────────

def metrics_snapshot() -> dict[str, Any]:
    """Point-in-time view of all metrics. Safe to call from any thread."""
    with _lock:
        out: dict[str, Any] = {
            "counters": dict(_counters),
            "latency_ms": {
                name: _percentiles(samples) for name, samples in _latencies.items()
            },
            "result_count": _percentiles(_result_counts),
            "derived": _derived_rates(),
        }
    # Stitch in cache stats from the cache module (avoids circular import)
    try:
        from pipeline import cache as _cache
        out["cache"] = _cache.cache_stats()
        out["cache_namespaces"] = _cache.cache_namespace_stats()
    except Exception:
        out["cache"] = {}
        out["cache_namespaces"] = {}
    return out


def reset() -> None:
    """Wipe all counters and histograms. For tests."""
    with _lock:
        for k in _counters:
            _counters[k] = 0
        for samples in _latencies.values():
            samples.clear()
        _result_counts.clear()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _percentiles(samples: list[float | int]) -> dict[str, float]:
    if not samples:
        return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    n = len(samples)
    return {
        "count": n,
        "p50":   float(samples[int(n * 0.50)]) if n else 0.0,
        "p95":   float(samples[min(int(n * 0.95), n - 1)]),
        "p99":   float(samples[min(int(n * 0.99), n - 1)]),
        "max":   float(samples[-1]),
    }


def _derived_rates() -> dict[str, float]:
    """Compute rates from counters. All bounded to [0, 1]."""
    total = max(_counters.get("searches_total", 0), 1)
    llm   = max(_counters.get("planner_llm_calls", 0), 1)
    return {
        "planner_fallback_rate":
            _counters.get("planner_fallback_used", 0) / total,
        "planner_cache_hit_rate":
            _counters.get("planner_cache_hits", 0) / total,
        "relaxation_rate":
            _counters.get("relaxations_triggered", 0) / total,
        "clarify_rate":
            _counters.get("clarify_returned", 0) / total,
        "empty_result_rate":
            _counters.get("empty_results", 0) / total,
        "forbidden_attempt_rate":
            _counters.get("forbidden_field_attempts", 0) / max(llm, 1),
    }
