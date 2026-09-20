"""Application cache helpers.

Cache-aside layout:
  1. Check process-local L1 LRU.
  2. Check optional Redis L2.
  3. Compute in the caller on miss.
  4. Write to L1 and L2 with namespace TTLs.

Redis is an optimization, not a correctness dependency. If Redis is down, the
application keeps serving with memory cache only and increments redis_errors.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections import OrderedDict
from threading import RLock
from typing import Any, Awaitable, Callable

from pipeline import settings
from pipeline.constants import (
    EMBEDDING_MODEL_VERSION,
    HYDE_VERSION,
    PLANNER_VERSION,
)

logger = logging.getLogger(__name__)

_TTL = {
    "plan": 86_400,           # 24 h
    "hyde": 604_800,          # 7 d
    "embed": 2_592_000,       # 30 d
    "insights": 300,          # 5 min
    "count": 60,              # 1 min
    "dense": 120,             # 2 min
    "bm25": 120,              # 2 min
    "bm25_negative": 60,      # 1 min
    "skill": 300,             # 5 min
    "skill_recency": 300,     # 5 min
    "candidate": 900,         # 15 min
    "chunk": 3_600,           # 60 min
    "recruiter": 600,         # 10 min
    "recruiter_prefs": 300,   # 5 min
    "recruiter_hints": 300,   # 5 min
}

_stats: dict[str, int] = {
    "l1_hits": 0,
    "l2_hits": 0,
    "misses": 0,
    "sets": 0,
    "redis_sets": 0,
    "redis_errors": 0,
}
_namespace_stats: dict[str, dict[str, int]] = {}


class _LRUCache:
    """Small thread-safe LRU cache for same-worker hot paths."""

    def __init__(self, maxsize: int = 2048):
        self._store: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._maxsize = max(1, int(maxsize))
        self._lock = RLock()

    def get(self, key: str) -> Any:
        with self._lock:
            if key not in self._store:
                return None
            value, expires_at = self._store[key]
            if time.time() > expires_at:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (value, time.time() + ttl_seconds)
            while len(self._store) > self._maxsize:
                self._store.popitem(last=False)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


def _configured_local_maxsize() -> int:
    return max(1, int(getattr(settings, "cache_local_maxsize", 2048) or 2048))


_lru = _LRUCache(maxsize=_configured_local_maxsize())
_redis: Any | None = None
_redis_enabled = False
_redis_backoff_until = 0.0
_namespace_override: str | None = None
_inflight: dict[str, asyncio.Lock] = {}


def _json_default(value: Any) -> str:
    return str(value)


def stable_json(value: Any) -> str:
    """Stable JSON string for cache keys. Values should already be compact."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def hash_payload(value: Any) -> str:
    return _hash(stable_json(value))


def plan_key(raw_input: str, explicit_filters_str: str = "", route: str = "") -> str:
    payload = f"{raw_input}|{explicit_filters_str}|{route}"
    return f"plan:{PLANNER_VERSION}:{_hash(payload)}"


def hyde_key(jd_text: str) -> str:
    return f"hyde:{HYDE_VERSION}:{_hash(jd_text)}"


def embed_key(text: str, *, provider: str = "", dimensions: int | None = None) -> str:
    payload = {"text": text, "provider": provider, "dimensions": dimensions}
    return f"embed:{EMBEDDING_MODEL_VERSION}:{hash_payload(payload)}"


def insights_key(payload_json: str) -> str:
    return f"insights:1:{_hash(payload_json)}"


def retrieval_key(namespace: str, payload: Any) -> str:
    if namespace not in {"count", "dense", "bm25", "skill"}:
        raise ValueError(f"unknown retrieval cache namespace: {namespace}")
    return f"{namespace}:1:{hash_payload(payload)}"


def bm25_negative_key(payload: Any) -> str:
    return f"bm25_negative:1:{hash_payload(payload)}"


def candidate_profile_key(candidate_id: str, updated_at: Any | None = None) -> str:
    suffix = str(updated_at or "unknown")
    return f"candidate:profile:{_hash(f'{candidate_id}|{suffix}')}"


def candidate_profile_prefix(candidate_id: str) -> str:
    return f"candidate:profile:{_hash(f'{candidate_id}|')[:8]}"


def chunk_key(chunk_id: str) -> str:
    return f"chunk:1:{_hash(chunk_id)}"


def skill_recency_key(candidate_ids: list[str], matched_skill_terms: list[str]) -> str:
    payload = {
        "candidate_ids": sorted(str(candidate_id) for candidate_id in candidate_ids),
        "matched_skill_terms": sorted(str(term).strip().lower() for term in matched_skill_terms),
    }
    return f"skill_recency:1:{hash_payload(payload)}"


def recruiter_profile_key(recruiter_id: str, lookback_days: int = 90) -> str:
    return f"recruiter:profile:{_hash(f'{recruiter_id}|{lookback_days}')}"


def recruiter_prefs_key(recruiter_id: str) -> str:
    return f"recruiter_prefs:1:{_hash(recruiter_id)}"


def recruiter_hints_key(recruiter_id: str) -> str:
    return f"recruiter_hints:1:{_hash(recruiter_id)}"


def _ttl_for_key(key: str) -> int:
    prefix = key.split(":", 1)[0]
    return _TTL.get(prefix, 3600)


def namespace_for_key(key: str) -> str:
    """Return the cache namespace used for metrics and TTL selection."""
    return key.split(":", 1)[0]


def _incr_namespace(key: str, metric: str) -> None:
    namespace = namespace_for_key(key)
    bucket = _namespace_stats.setdefault(
        namespace,
        {"l1_hits": 0, "l2_hits": 0, "misses": 0, "sets": 0, "redis_sets": 0},
    )
    bucket[metric] = bucket.get(metric, 0) + 1


def _namespace() -> str:
    if _namespace_override:
        return _namespace_override
    app_name = getattr(settings, "cache_namespace", "hybrid-search") or "hybrid-search"
    env = getattr(settings, "langfuse_environment", "development") or "development"
    return f"{app_name}:{env}"


def _redis_key(key: str) -> str:
    return f"{_namespace()}:{key}"


def redis_key_for_tests(key: str) -> str:
    return _redis_key(key)


def _encode(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=_json_default)


def _decode(raw: str | bytes) -> Any:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def cache_stats() -> dict[str, int | float]:
    out: dict[str, int | float] = dict(_stats)
    hits = int(out["l1_hits"]) + int(out["l2_hits"])
    total = max(hits + int(out["misses"]), 1)
    out["hits"] = hits
    out["hit_rate"] = round(hits / total, 4)
    out["l2_share_of_hits"] = round(int(out["l2_hits"]) / max(hits, 1), 4)
    return out


def cache_namespace_stats() -> dict[str, dict[str, int | float]]:
    """Per-namespace counters for dashboards and Langfuse metadata."""
    out: dict[str, dict[str, int | float]] = {}
    for namespace, stats in sorted(_namespace_stats.items()):
        l1_hits = int(stats.get("l1_hits", 0))
        l2_hits = int(stats.get("l2_hits", 0))
        misses = int(stats.get("misses", 0))
        hits = l1_hits + l2_hits
        total = max(hits + misses, 1)
        out[namespace] = {
            **dict(stats),
            "hits": hits,
            "hit_rate": round(hits / total, 4),
            "l2_share_of_hits": round(l2_hits / max(hits, 1), 4),
        }
    return out


def cache_snapshot() -> dict[str, Any]:
    """Compact complete cache snapshot for `/metrics` and trace metadata."""
    return {
        "summary": cache_stats(),
        "namespaces": cache_namespace_stats(),
        "backend": "redis" if _redis_enabled else "memory",
        "namespace": _namespace(),
    }


def _counter_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int | float]:
    keys = ("l1_hits", "l2_hits", "misses", "sets", "redis_sets", "redis_errors")
    out: dict[str, int | float] = {
        key: int(after.get(key, 0) or 0) - int(before.get(key, 0) or 0)
        for key in keys
    }
    hits = int(out["l1_hits"]) + int(out["l2_hits"])
    misses = int(out["misses"])
    total = hits + misses
    out["hits"] = hits
    out["hit_rate"] = round(hits / total, 4) if total else 0.0
    out["l2_share_of_hits"] = round(int(out["l2_hits"]) / hits, 4) if hits else 0.0
    return out


def cache_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Return request-local cache counter deltas between two snapshots."""
    before_namespaces = before.get("namespaces", {}) or {}
    after_namespaces = after.get("namespaces", {}) or {}
    namespaces = {
        namespace: _counter_delta(
            before_namespaces.get(namespace, {}) or {},
            after_namespaces.get(namespace, {}) or {},
        )
        for namespace in sorted(before_namespaces.keys() | after_namespaces.keys())
    }
    return {
        "summary": _counter_delta(
            before.get("summary", {}) or {},
            after.get("summary", {}) or {},
        ),
        "namespaces": namespaces,
        "backend": after.get("backend"),
        "namespace": after.get("namespace"),
    }


async def start() -> None:
    """Initialize optional Redis L2 cache."""
    global _redis, _redis_enabled, _lru
    _lru = _LRUCache(maxsize=_configured_local_maxsize())

    backend = getattr(settings, "cache_backend", "memory")
    redis_url = (getattr(settings, "redis_url", "") or "").strip()
    if backend != "redis" or not redis_url:
        _redis = None
        _redis_enabled = False
        logger.info("Cache backend: memory")
        return

    try:
        import redis.asyncio as redis

        _redis = redis.from_url(
            redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_timeout=float(getattr(settings, "cache_redis_socket_timeout_seconds", 0.2)),
            socket_connect_timeout=float(
                getattr(settings, "cache_redis_connect_timeout_seconds", 0.2)
            ),
            protocol=2,
        )
        await _redis.ping()
        _redis_enabled = True
        logger.info("Cache backend: redis namespace=%s", _namespace())
    except Exception as exc:
        _redis = None
        _redis_enabled = False
        _stats["redis_errors"] += 1
        logger.warning("Redis cache unavailable; using memory cache only: %s", exc)


async def close() -> None:
    global _redis, _redis_enabled
    if _redis is not None:
        try:
            await _redis.aclose()
        except Exception as exc:
            logger.debug("Redis cache close failed: %s", exc)
    _redis = None
    _redis_enabled = False


def configure_for_tests(redis_client: Any | None = None, *, namespace: str = "test") -> None:
    global _redis, _redis_enabled, _namespace_override, _lru, _redis_backoff_until
    _redis = redis_client
    _redis_enabled = redis_client is not None
    _namespace_override = namespace
    _redis_backoff_until = 0.0
    _lru = _LRUCache(maxsize=64)


def clear_all() -> None:
    _lru.clear()
    _inflight.clear()
    for key in _stats:
        _stats[key] = 0
    _namespace_stats.clear()


def _redis_available() -> bool:
    return bool(_redis_enabled and _redis is not None and time.time() >= _redis_backoff_until)


def _mark_redis_error(exc: Exception) -> None:
    global _redis_backoff_until
    _stats["redis_errors"] += 1
    _redis_backoff_until = time.time() + 5.0
    logger.debug("Redis cache operation failed; backing off briefly: %s", exc)


async def get(key: str) -> Any:
    value = _lru.get(key)
    if value is not None:
        _stats["l1_hits"] += 1
        _incr_namespace(key, "l1_hits")
        return value

    if _redis_available():
        try:
            raw = await _redis.get(_redis_key(key))
            if raw is not None:
                value = _decode(raw)
                _lru.set(key, value, _ttl_for_key(key))
                _stats["l2_hits"] += 1
                _incr_namespace(key, "l2_hits")
                return value
        except Exception as exc:
            _mark_redis_error(exc)

    _stats["misses"] += 1
    _incr_namespace(key, "misses")
    return None


async def set(key: str, value: Any) -> None:  # noqa: A001
    ttl = _ttl_for_key(key)
    _lru.set(key, value, ttl)
    _stats["sets"] += 1
    _incr_namespace(key, "sets")

    if not _redis_available():
        return
    try:
        await _redis.set(_redis_key(key), _encode(value), ex=ttl)
        _stats["redis_sets"] += 1
        _incr_namespace(key, "redis_sets")
    except Exception as exc:
        _mark_redis_error(exc)


async def invalidate(key: str) -> None:
    _lru.delete(key)
    if not _redis_available():
        return
    try:
        await _redis.delete(_redis_key(key))
    except Exception as exc:
        _mark_redis_error(exc)


async def invalidate_many(keys: list[str]) -> None:
    for key in keys:
        _lru.delete(key)
    if not keys or not _redis_available():
        return
    try:
        await _redis.delete(*[_redis_key(key) for key in keys])
    except Exception as exc:
        _mark_redis_error(exc)


async def get_or_set(
    key: str,
    producer: Callable[[], Awaitable[Any]],
    *,
    cache_none: bool = False,
) -> Any:
    cached = await get(key)
    if cached is not None:
        return cached

    lock = _inflight.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            cached = await get(key)
            if cached is not None:
                return cached
            value = await producer()
            if value is not None or cache_none:
                await set(key, value)
            return value
    finally:
        if not lock.locked():
            _inflight.pop(key, None)
