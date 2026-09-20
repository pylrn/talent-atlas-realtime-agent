# Distributed Caching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current process-only cache with a production-ready cache-aside system that keeps the existing fast local LRU behavior while adding optional Redis sharing for multi-worker and multi-node deployments.

**Architecture:** Keep a small in-process L1 LRU cache for sub-millisecond repeated hits inside one worker, then add Redis as an optional L2 cache shared by all app replicas. The application remains correct if Redis is missing or down: cache reads miss, cache writes fall back to local memory, and search still runs normally.

**Tech Stack:** Python 3.11, FastAPI lifespan hooks, `redis.asyncio`, asyncpg, existing `pipeline/cache.py`, existing `/metrics` endpoint, Docker Compose Redis for local scale testing.

---

## Why This Is The Right Cache Shape

The current code already has `pipeline/cache.py`, but it is only an in-process `OrderedDict` LRU. That is fine on one uvicorn worker, but it breaks down when the app scales:

- Multiple uvicorn workers each build their own cache, so hit rate drops.
- Multiple app nodes never share hot planner or embedding entries.
- Deploys and worker restarts wipe hot entries.
- The cache docstring mentions Redis, but there is no Redis backend yet.

The target behavior is cache-aside:

1. Build a stable key from the real inputs that affect the computation.
2. Check L1 local memory.
3. If L1 misses and Redis is enabled, check Redis.
4. If Redis hits, hydrate L1 and return the value.
5. If both miss, compute the value normally.
6. Store the value in L1 and Redis with a namespace-specific TTL.
7. If Redis fails, mark a short circuit-breaker window and keep serving from the app.

This works well for this search system because the expensive pieces are deterministic enough to reuse:

- Planner output for the same query, filters, route, model, and personalization hint tag.
- Query embeddings for the same text, provider, model, and dimensions.
- Short-lived candidate insight inputs after `/search`, so `/search/insights` does not rerun search.

Do not cache full search responses as a default production feature. Search results depend on mutable candidate data, recruiter personalization, impression IDs, optional reranking, and ranking explanations. Caching those too broadly can return stale or user-specific data. Keep result-like caches short-lived and purpose-specific.

## File Structure

- Modify `pyproject.toml` - add the Redis async client dependency.
- Modify `.env.example` - document cache backend and Redis settings.
- Modify `docker-compose.yml` - add a local Redis service for development and staging-like testing.
- Modify `pipeline/__init__.py` - add typed cache settings with validation.
- Replace most of `pipeline/cache.py` - keep key builders, add async L1/L2 backend, JSON serialization, stats, lifecycle, and test hooks.
- Modify `api/main.py` - start and stop the cache backend in lifespan, migrate insights cache reads/writes to async, and store JSON-safe insight inputs.
- Modify `pipeline/planner.py` - await cache reads/writes around planner specs.
- Modify `pipeline/retrieve_dense.py` - await cache reads/writes around query embeddings and include provider/model/dimensions in keys.
- Modify `pipeline/metrics.py` - keep current `/metrics` behavior, but expose richer cache stats from `pipeline.cache.cache_stats()`.
- Modify `tests/test_config_contracts.py` - cover cache settings defaults.
- Create `tests/test_cache_backend.py` - cover local LRU, Redis read-through, Redis fallback, TTL selection, JSON round-trip, and namespace clearing.
- Modify cache-using tests in `tests/test_planner_fallback_control.py`, `tests/test_personalization_planner.py`, and `tests/test_admin_api_helpers.py` - await the async cache API where needed.
- Update `docs/toggle.md`, `docs/storage.md`, and `docs/scaling.md` - document L1/L2 behavior, deployment settings, sizing, and rollout.

## Cache Namespaces And TTLs

| Namespace | Key shape | TTL | Stored value | Why |
|---|---|---:|---|---|
| `plan:` | `plan:{PLANNER_VERSION}:{hash}` | 24 h | JSON dict for `CanonicalSearchSpec` | Avoid repeated paid LLM planner calls. |
| `hyde:` | `hyde:{HYDE_VERSION}:{hash}` | 7 d | HyDE profile string | Preserve existing contract, even though current HyDE is inside planner output. |
| `embed:` | `embed:{EMBEDDING_MODEL_VERSION}:{hash}` | 30 d | `list[float]` query vector | Avoid repeated embedding calls and local model work. |
| `insights:` | `insights:1:{hash}` | 5 min | JSON-safe `CandidateInsightInput` dicts | Let `/search/insights` reuse just-produced search evidence without rerunning retrieval. |

Redis keys should be prefixed with environment and app namespace:

```text
{CACHE_NAMESPACE}:{LANGFUSE_ENVIRONMENT}:{logical_key}
hybrid-search:production:plan:v2:abc123...
hybrid-search:production:embed:minilm-l6-v2:def456...
```

This prevents staging from reading production cache entries and makes namespace clearing safe.

---

### Task 1: Add Cache Configuration And Redis Dependency

**Files:**
- Modify: `pyproject.toml`
- Modify: `.env.example`
- Modify: `docker-compose.yml`
- Modify: `pipeline/__init__.py`
- Test: `tests/test_config_contracts.py`

- [ ] **Step 1: Write failing config tests**

Add these tests to `tests/test_config_contracts.py`:

```python
def test_cache_settings_default_to_memory_backend(monkeypatch):
    from pipeline import Settings

    monkeypatch.delenv("CACHE_BACKEND", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    loaded = Settings(_env_file=ROOT / ".env.example")

    assert loaded.cache_backend == "memory"
    assert loaded.redis_url == ""
    assert loaded.cache_namespace == "hybrid-search"
    assert loaded.cache_local_maxsize == 2048
    assert loaded.cache_redis_socket_timeout_seconds == 0.2
    assert loaded.cache_redis_connect_timeout_seconds == 0.2


def test_cache_backend_accepts_only_memory_or_redis(monkeypatch):
    from pydantic import ValidationError
    from pipeline import Settings

    monkeypatch.setenv("CACHE_BACKEND", "not-a-cache")

    with pytest.raises(ValidationError, match="cache_backend"):
        Settings(_env_file=ROOT / ".env.example")
```

Also add `import pytest` near the top of the file if it is not already present.

- [ ] **Step 2: Run the config tests and verify they fail**

Run:

```bash
pytest tests/test_config_contracts.py -v -k "cache_settings or cache_backend"
```

Expected: failure because `Settings` does not yet define the cache fields.

- [ ] **Step 3: Add the Redis dependency**

In `pyproject.toml`, add this item to `[project].dependencies`:

```toml
    "redis>=5.2.0",
```

Why: use `redis.asyncio` so cache network calls do not block the FastAPI event loop.

- [ ] **Step 4: Add cache settings**

In `pipeline/__init__.py`, add these fields inside `class Settings`, near the other infrastructure settings:

```python
    # -- Cache ---------------------------------------------------------------
    # memory = process-local LRU only.
    # redis  = process-local LRU plus Redis L2 shared by workers/app nodes.
    cache_backend: str = "memory"
    redis_url: str = ""
    cache_namespace: str = "hybrid-search"
    cache_local_maxsize: int = 2048
    cache_redis_socket_timeout_seconds: float = 0.2
    cache_redis_connect_timeout_seconds: float = 0.2
```

Add this validator below the existing validators:

```python
    @field_validator("cache_backend", mode="before")
    @classmethod
    def _validate_cache_backend(cls, value):
        backend = str(value or "memory").strip().lower()
        if backend not in {"memory", "redis"}:
            raise ValueError("cache_backend must be 'memory' or 'redis'")
        return backend
```

Why: defaults keep current behavior. Redis is opt-in, which makes rollout safe.

- [ ] **Step 5: Document environment variables**

Add this block to `.env.example` after the database settings:

```dotenv
# -- Cache -----------------------------------------------------------------
# memory keeps today's process-local LRU behavior.
# redis adds a shared L2 cache across workers and app nodes.
CACHE_BACKEND=memory
REDIS_URL=
CACHE_NAMESPACE=hybrid-search
CACHE_LOCAL_MAXSIZE=2048
CACHE_REDIS_SOCKET_TIMEOUT_SECONDS=0.2
CACHE_REDIS_CONNECT_TIMEOUT_SECONDS=0.2
```

Why: production can set `CACHE_BACKEND=redis` and `REDIS_URL=redis://...` without code changes.

- [ ] **Step 6: Add local Redis to Docker Compose**

In `docker-compose.yml`, add a Redis service:

```yaml
  redis:
    image: redis:7-alpine
    container_name: hybrid_search_redis
    command: ["redis-server", "--appendonly", "no", "--maxmemory-policy", "allkeys-lru"]
    ports:
      - "6379:6379"
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5
```

Why: local Redis lets us test the future multi-worker topology without adding managed infrastructure.

- [ ] **Step 7: Run the config tests and verify they pass**

Run:

```bash
pytest tests/test_config_contracts.py -v -k "cache_settings or cache_backend"
```

Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml .env.example docker-compose.yml pipeline/__init__.py tests/test_config_contracts.py
git commit -m "feat(cache): add cache backend configuration"
```

---

### Task 2: Add Cache Backend Tests

**Files:**
- Create: `tests/test_cache_backend.py`
- Modify: `pipeline/cache.py`

- [ ] **Step 1: Create failing cache backend tests**

Create `tests/test_cache_backend.py`:

```python
import asyncio
import json

import pytest

from pipeline import cache


class FakeRedis:
    def __init__(self, *, fail_get: bool = False, fail_set: bool = False):
        self.store: dict[str, str] = {}
        self.expiry: dict[str, int] = {}
        self.fail_get = fail_get
        self.fail_set = fail_set
        self.closed = False

    async def ping(self):
        return True

    async def get(self, key: str):
        if self.fail_get:
            raise RuntimeError("redis get failed")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int):
        if self.fail_set:
            raise RuntimeError("redis set failed")
        self.store[key] = value
        self.expiry[key] = ex
        return True

    async def delete(self, *keys: str):
        for key in keys:
            self.store.pop(key, None)
            self.expiry.pop(key, None)
        return len(keys)

    async def scan_iter(self, match: str):
        prefix = match.removesuffix("*")
        for key in list(self.store):
            if key.startswith(prefix):
                yield key

    async def aclose(self):
        self.closed = True


@pytest.fixture(autouse=True)
def reset_cache():
    cache.configure_for_tests(redis_client=None)
    cache.clear_all()
    yield
    cache.configure_for_tests(redis_client=None)
    cache.clear_all()


@pytest.mark.asyncio
async def test_memory_cache_round_trips_json_values():
    key = cache.plan_key("python engineer", "{}", "query_mode|llm|test|model")

    await cache.set(key, {"semantic_query": "python engineer", "confidence": 0.9})

    assert await cache.get(key) == {"semantic_query": "python engineer", "confidence": 0.9}
    stats = cache.cache_stats()
    assert stats["hits"] == 1
    assert stats["sets"] == 1


@pytest.mark.asyncio
async def test_redis_hit_hydrates_local_lru():
    fake = FakeRedis()
    cache.configure_for_tests(redis_client=fake, namespace="test")
    key = cache.plan_key("sales", "{}", "query_mode|llm|test|model")
    redis_key = cache.redis_key_for_tests(key)
    fake.store[redis_key] = json.dumps({"semantic_query": "sales"})

    assert await cache.get(key) == {"semantic_query": "sales"}

    fake.fail_get = True
    assert await cache.get(key) == {"semantic_query": "sales"}
    stats = cache.cache_stats()
    assert stats["l2_hits"] == 1
    assert stats["l1_hits"] == 1


@pytest.mark.asyncio
async def test_redis_failure_falls_back_to_local_cache():
    fake = FakeRedis(fail_set=True)
    cache.configure_for_tests(redis_client=fake, namespace="test")
    key = cache.embed_key("python", provider="local/all-MiniLM-L6-v2", dimensions=384)

    await cache.set(key, [0.1, 0.2, 0.3])

    assert await cache.get(key) == [0.1, 0.2, 0.3]
    stats = cache.cache_stats()
    assert stats["redis_errors"] == 1
    assert stats["hits"] == 1


@pytest.mark.asyncio
async def test_ttl_is_selected_from_namespace():
    fake = FakeRedis()
    cache.configure_for_tests(redis_client=fake, namespace="test")

    await cache.set(cache.plan_key("x"), {"value": 1})
    await cache.set(cache.embed_key("x", provider="local/model", dimensions=384), [0.1])
    await cache.set(cache.insights_key("{}"), [{"candidate_id": "c1"}])

    expiries = sorted(fake.expiry.values())
    assert expiries == sorted([300, 86_400, 2_592_000])


@pytest.mark.asyncio
async def test_get_or_set_collapses_same_process_concurrency():
    calls = 0

    async def produce():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"value": calls}

    key = cache.plan_key("same")
    values = await asyncio.gather(*(cache.get_or_set(key, produce) for _ in range(10)))

    assert calls == 1
    assert values == [{"value": 1}] * 10
```

Why: these tests pin down the key production guarantees before changing call sites.

- [ ] **Step 2: Run the cache tests and verify they fail**

Run:

```bash
pytest tests/test_cache_backend.py -v
```

Expected: failure because `configure_for_tests`, async `get`, async `set`, `get_or_set`, and Redis support do not exist yet.

- [ ] **Step 3: Commit the failing tests**

```bash
git add tests/test_cache_backend.py
git commit -m "test(cache): specify distributed cache behavior"
```

---

### Task 3: Replace Cache Core With L1/L2 Async Cache

**Files:**
- Modify: `pipeline/cache.py`
- Test: `tests/test_cache_backend.py`

- [ ] **Step 1: Replace `pipeline/cache.py` with the async L1/L2 implementation**

Replace the current contents of `pipeline/cache.py` with this implementation:

```python
"""Application cache helpers.

The cache is intentionally cache-aside:
  1. Check process-local L1 LRU.
  2. Check optional Redis L2.
  3. Compute in the caller on miss.
  4. Write back to L1 and L2 with namespace TTLs.

Redis is an optimization, not a dependency for correctness. If Redis is down,
the app keeps serving with process-local caching only.
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
    "plan": 86_400,
    "hyde": 604_800,
    "embed": 2_592_000,
    "insights": 300,
}

_stats: dict[str, int] = {
    "l1_hits": 0,
    "l2_hits": 0,
    "misses": 0,
    "sets": 0,
    "redis_sets": 0,
    "redis_errors": 0,
}


class _LRUCache:
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


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def plan_key(raw_input: str, explicit_filters_str: str = "", route: str = "") -> str:
    payload = f"{raw_input}|{explicit_filters_str}|{route}"
    return f"plan:{PLANNER_VERSION}:{_hash(payload)}"


def hyde_key(jd_text: str) -> str:
    return f"hyde:{HYDE_VERSION}:{_hash(jd_text)}"


def embed_key(text: str, *, provider: str = "", dimensions: int | None = None) -> str:
    payload = f"{text}|{provider}|{dimensions or ''}"
    return f"embed:{EMBEDDING_MODEL_VERSION}:{_hash(payload)}"


def insights_key(payload_json: str) -> str:
    return f"insights:1:{_hash(payload_json)}"


def _ttl_for_key(key: str) -> int:
    prefix = key.split(":", 1)[0]
    return _TTL.get(prefix, 3600)


def _namespace() -> str:
    if _namespace_override:
        return _namespace_override
    app = getattr(settings, "cache_namespace", "hybrid-search") or "hybrid-search"
    env = getattr(settings, "langfuse_environment", "development") or "development"
    return f"{app}:{env}"


def _redis_key(key: str) -> str:
    return f"{_namespace()}:{key}"


def redis_key_for_tests(key: str) -> str:
    return _redis_key(key)


def _encode(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _decode(raw: str) -> Any:
    return json.loads(raw)


def cache_stats() -> dict[str, int]:
    out = dict(_stats)
    out["hits"] = out["l1_hits"] + out["l2_hits"]
    return out


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
    _lru = _LRUCache(maxsize=32)


def clear_all() -> None:
    _lru.clear()
    _inflight.clear()
    for key in _stats:
        _stats[key] = 0


async def clear_namespace() -> int:
    """Delete this app/environment namespace from Redis and clear local state."""
    clear_all()
    if not _redis_available():
        return 0

    deleted = 0
    batch: list[str] = []
    try:
        async for key in _redis.scan_iter(match=f"{_namespace()}:*"):
            batch.append(key)
            if len(batch) >= 100:
                deleted += int(await _redis.delete(*batch))
                batch.clear()
        if batch:
            deleted += int(await _redis.delete(*batch))
        return deleted
    except Exception as exc:
        _mark_redis_error(exc)
        return deleted


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
        return value

    if _redis_available():
        try:
            raw = await _redis.get(_redis_key(key))
            if raw is not None:
                value = _decode(raw)
                _lru.set(key, value, _ttl_for_key(key))
                _stats["l2_hits"] += 1
                return value
        except Exception as exc:
            _mark_redis_error(exc)

    _stats["misses"] += 1
    return None


async def set(key: str, value: Any) -> None:  # noqa: A001
    ttl = _ttl_for_key(key)
    _lru.set(key, value, ttl)
    _stats["sets"] += 1

    if not _redis_available():
        return

    try:
        await _redis.set(_redis_key(key), _encode(value), ex=ttl)
        _stats["redis_sets"] += 1
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
```

How it works:

- `_LRUCache` is still the hot path and remains extremely cheap.
- Redis is only queried after an L1 miss.
- Every Redis value is JSON, so the cache never unpickles untrusted bytes.
- Redis failures increment metrics and back off for five seconds, preventing slow repeated failures.
- `get_or_set` collapses duplicate concurrent work inside a worker, which matters when many users submit the same search after a deploy.

- [ ] **Step 2: Run cache backend tests**

Run:

```bash
pytest tests/test_cache_backend.py -v
```

Expected: pass.

- [ ] **Step 3: Run existing cache-adjacent tests**

Run:

```bash
pytest tests/test_planner_fallback_control.py tests/test_personalization_planner.py tests/test_admin_api_helpers.py -v
```

Expected: some failures because call sites still use sync `get`/`set`. Those failures are handled in later tasks.

- [ ] **Step 4: Commit**

```bash
git add pipeline/cache.py tests/test_cache_backend.py
git commit -m "feat(cache): add async l1 redis cache backend"
```

---

### Task 4: Wire Cache Lifecycle Into FastAPI

**Files:**
- Modify: `api/main.py`
- Test: `tests/test_api_lifespan.py`

- [ ] **Step 1: Write a lifespan test for cache startup and shutdown**

Add this test to `tests/test_api_lifespan.py`:

```python
@pytest.mark.asyncio
async def test_lifespan_starts_and_closes_cache(monkeypatch):
    from api import main as api_main

    events = []

    async def fake_cache_start():
        events.append("cache_start")

    async def fake_cache_close():
        events.append("cache_close")

    async def fake_get_pool():
        class Pool:
            async def acquire(self):
                raise AssertionError("warmup is patched out")
        return Pool()

    monkeypatch.setattr(api_main, "get_pool", fake_get_pool)
    monkeypatch.setattr(api_main, "get_search_pool", fake_get_pool)
    monkeypatch.setattr(api_main, "_warmup_db_pool", lambda *args, **kwargs: {"status": "ok"})
    monkeypatch.setattr(api_main, "_warmup_active_embedder", lambda app: {"status": "ok"})
    monkeypatch.setattr(api_main, "_warmup_reranker", lambda app, choice: {"status": "ok"})
    monkeypatch.setattr(api_main, "close_pool", lambda: None)
    monkeypatch.setattr(api_main.shutdown_langfuse, "__call__", lambda: None, raising=False)
    monkeypatch.setattr(api_main._cache, "start", fake_cache_start)
    monkeypatch.setattr(api_main._cache, "close", fake_cache_close)

    async with api_main.lifespan(api_main.app):
        assert "cache_start" in events

    assert events[-1] == "cache_close"
```

If `tests/test_api_lifespan.py` uses a different fixture style, keep the assertion: cache start happens during startup and cache close happens during shutdown.

- [ ] **Step 2: Run the lifespan test and verify it fails**

Run:

```bash
pytest tests/test_api_lifespan.py -v -k cache
```

Expected: failure because the lifespan does not call cache lifecycle functions.

- [ ] **Step 3: Import cache in `api/main.py`**

Near the existing pipeline imports, add:

```python
from pipeline import cache as _cache
```

- [ ] **Step 4: Start cache during lifespan**

In `lifespan(app: FastAPI)`, after pools are created and before model warmups, add:

```python
    await _cache.start()
```

Why: cache should be available before warmup or request handlers can use it.

- [ ] **Step 5: Close cache during shutdown**

In the `finally:` block of `lifespan`, before `await close_pool()`, add:

```python
        await _cache.close()
```

Why: Redis connections should be closed cleanly so app shutdown does not leak sockets.

- [ ] **Step 6: Run the lifespan test**

Run:

```bash
pytest tests/test_api_lifespan.py -v -k cache
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add api/main.py tests/test_api_lifespan.py
git commit -m "feat(cache): manage cache lifecycle in api startup"
```

---

### Task 5: Migrate Planner Cache Calls To Async

**Files:**
- Modify: `pipeline/planner.py`
- Modify: `tests/test_planner_fallback_control.py`
- Modify: `tests/test_personalization_planner.py`
- Test: planner tests

- [ ] **Step 1: Update tests that spy on cache get/set**

In `tests/test_personalization_planner.py`, change sync spies to async spies:

```python
    async def spy_get(k):
        seen_keys.append(k)
        return None

    async def spy_set(k, v):
        stored[k] = v

    monkeypatch.setattr(cache_mod, "get", spy_get)
    monkeypatch.setattr(cache_mod, "set", spy_set)
```

When a test only needs to clear local cache, keep:

```python
    _cache.clear_all()
```

Why: `clear_all()` remains sync for tests and local memory, but `get()` and `set()` become async because Redis is async.

- [ ] **Step 2: Run planner tests and verify async call failures**

Run:

```bash
pytest tests/test_planner_fallback_control.py tests/test_personalization_planner.py -v
```

Expected: failures until `pipeline/planner.py` awaits cache calls.

- [ ] **Step 3: Await planner cache lookup**

In `pipeline/planner.py`, change:

```python
            cached = _cache.get(ck)
```

to:

```python
            cached = await _cache.get(ck)
```

- [ ] **Step 4: Await planner cache write**

Change:

```python
        _cache.set(ck, spec_dict)
```

to:

```python
        await _cache.set(ck, spec_dict)
```

Why: planner is already async, so this keeps the event loop non-blocking and allows Redis to be used safely.

- [ ] **Step 5: Run planner tests**

Run:

```bash
pytest tests/test_planner_fallback_control.py tests/test_personalization_planner.py -v
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add pipeline/planner.py tests/test_planner_fallback_control.py tests/test_personalization_planner.py
git commit -m "refactor(cache): await planner cache operations"
```

---

### Task 6: Migrate Dense Embedding Cache To Async And Safer Keys

**Files:**
- Modify: `pipeline/retrieve_dense.py`
- Modify: `pipeline/metrics.py`
- Test: `tests/test_local_embedder_fallback.py` or a new focused dense-cache test

- [ ] **Step 1: Add a focused dense-cache test**

Create or extend a retrieval test with this behavior:

```python
@pytest.mark.asyncio
async def test_dense_embedding_cache_key_includes_embedder_identity(monkeypatch):
    from pipeline import cache as cache_mod
    from pipeline.retrieve_dense import retrieve_dense

    seen_keys = []

    async def fake_get(key):
        seen_keys.append(key)
        return [0.1, 0.2, 0.3]

    monkeypatch.setattr(cache_mod, "get", fake_get)

    class FakeEmbedder:
        name = "local/test-model"
        dimensions = 3

        async def embed(self, texts):
            raise AssertionError("cache hit should skip embedding")

    class FakePool:
        async def acquire(self):
            raise AssertionError("database is not needed for this key test")

    from pipeline.spec import CanonicalSearchSpec

    spec = CanonicalSearchSpec(
        input_type="query",
        intent="candidate_search",
        semantic_query="python engineer",
    )

    await retrieve_dense(
        spec,
        "FALSE",
        [],
        FakePool(),
        FakeEmbedder(),
        top_k=0,
    )

    assert seen_keys
    assert seen_keys[0].startswith("embed:")
```

If the existing dense function reaches SQL even for `top_k=0`, assert against a fake pool that returns empty rows instead of raising.

- [ ] **Step 2: Run the focused test and verify failure**

Run:

```bash
pytest tests -v -k "dense_embedding_cache_key"
```

Expected: failure until `retrieve_dense.py` awaits cache calls and uses the new key signature.

- [ ] **Step 3: Update `retrieve_dense.py` cache lookup**

Change:

```python
    ek = _cache.embed_key(embed_text)
    embedding: list[float] | None = _cache.get(ek)
```

to:

```python
    ek = _cache.embed_key(
        embed_text,
        provider=embedder.name,
        dimensions=embedder.dimensions,
    )
    embedding: list[float] | None = await _cache.get(ek)
```

Why: the same text embedded by different models or dimensions is a different vector. The key must encode that identity.

- [ ] **Step 4: Update embedding cache write**

Change:

```python
        _cache.set(ek, embedding)
```

to:

```python
        await _cache.set(ek, embedding)
```

- [ ] **Step 5: Increment embedding cache metric on hit**

After:

```python
    timings["embedding_cache_hit"] = embedding is not None
```

add:

```python
    if embedding is not None:
        from pipeline import metrics as _metrics
        _metrics.incr("embedding_cache_hits")
```

Why: `metrics.py` already defines the counter, but the current dense path does not increment it.

- [ ] **Step 6: Run dense and metrics tests**

Run:

```bash
pytest tests/test_local_embedder_fallback.py tests/test_observability.py tests -v -k "dense_embedding_cache_key or metrics"
```

Expected: pass for the focused dense test and no metrics regressions.

- [ ] **Step 7: Commit**

```bash
git add pipeline/retrieve_dense.py pipeline/metrics.py tests
git commit -m "refactor(cache): use async embedding cache with model-safe keys"
```

---

### Task 7: Make Insights Cache JSON-Safe And Async

**Files:**
- Modify: `api/main.py`
- Modify: `tests/test_admin_api_helpers.py`

- [ ] **Step 1: Add serialization helpers in `api/main.py`**

Near `_insights_cache_key`, add:

```python
def _insight_inputs_to_cache_payload(items: list) -> list[dict[str, Any]]:
    from dataclasses import asdict, is_dataclass

    payload = []
    for item in items:
        payload.append(asdict(item) if is_dataclass(item) else dict(item))
    return payload


def _insight_inputs_from_cache_payload(payload: list[dict[str, Any]]) -> list:
    return [CandidateInsightInput(**item) for item in payload]
```

Also import `CandidateInsightInput` from `pipeline.ai_insights` where the other insight imports live:

```python
from pipeline.ai_insights import (
    AIInsightResult as PipelineAIInsightResult,
    AIInsightService,
    CandidateInsightInput,
    candidate_insight_input_from_result,
)
```

Why: Redis stores JSON. `SearchResult` dataclass instances should not be stored directly in Redis.

- [ ] **Step 2: Store insight inputs after `/search`**

Change this block:

```python
            from pipeline import cache as _cache
            # Cache with stable key so /search/insights never reruns the search
            _cache.set(_insights_cache_key(req), search_resp.results)
```

to:

```python
            insight_inputs = [
                candidate_insight_input_from_result(r)
                for r in search_resp.results
            ]
            await _cache.set(
                _insights_cache_key(req),
                _insight_inputs_to_cache_payload(insight_inputs),
            )
```

Why: the cache now contains only the compact evidence matrix input, not full mutable search result objects.

- [ ] **Step 3: Read insight inputs in `_candidate_inputs_for_insights`**

Change:

```python
    cached = _cache.get(ckey)

    if cached is not None:
        # Use whatever was cached — never rerun just because top_k differs
        return [candidate_insight_input_from_result(r) for r in cached[:top_k]]
```

to:

```python
    cached = await _cache.get(ckey)

    if cached is not None:
        return _insight_inputs_from_cache_payload(cached[:top_k])
```

Then change:

```python
    _cache.set(ckey, raw_results)
    return [candidate_insight_input_from_result(r) for r in raw_results[:top_k]]
```

to:

```python
    insight_inputs = [candidate_insight_input_from_result(r) for r in raw_results]
    await _cache.set(ckey, _insight_inputs_to_cache_payload(insight_inputs))
    return insight_inputs[:top_k]
```

- [ ] **Step 4: Update admin helper tests to clear cache safely**

Keep sync clear calls:

```python
    _cache.clear_all()
```

If any test monkeypatches `_cache.get` or `_cache.set`, use async fakes:

```python
    async def fake_get(key):
        return None

    async def fake_set(key, value):
        captured["cache_value"] = value
```

- [ ] **Step 5: Run insight-related tests**

Run:

```bash
pytest tests/test_admin_api_helpers.py -v -k "insights or latency"
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add api/main.py tests/test_admin_api_helpers.py
git commit -m "refactor(cache): store json safe insight cache payloads"
```

---

### Task 8: Expose Richer Cache Metrics

**Files:**
- Modify: `pipeline/metrics.py`
- Test: `tests/test_observability.py`

- [ ] **Step 1: Add a metrics test**

Add a test that verifies `metrics_snapshot()` exposes the expanded cache shape:

```python
def test_metrics_snapshot_includes_expanded_cache_stats(monkeypatch):
    from pipeline import metrics
    from pipeline import cache as cache_mod

    monkeypatch.setattr(
        cache_mod,
        "cache_stats",
        lambda: {
            "l1_hits": 2,
            "l2_hits": 3,
            "misses": 4,
            "sets": 5,
            "redis_sets": 6,
            "redis_errors": 0,
            "hits": 5,
        },
    )

    snapshot = metrics.metrics_snapshot()

    assert snapshot["cache"]["l1_hits"] == 2
    assert snapshot["cache"]["l2_hits"] == 3
    assert snapshot["cache"]["hits"] == 5
```

- [ ] **Step 2: Run the metrics test**

Run:

```bash
pytest tests/test_observability.py -v -k "cache_stats"
```

Expected: pass if `metrics_snapshot()` already stitches in `cache_stats()`. If it fails because a test expects only old keys, update that expectation to include the expanded shape.

- [ ] **Step 3: Add derived cache rates**

In `pipeline/metrics.py`, after `out["cache"] = _cache.cache_stats()`, add:

```python
        cache_stats = out["cache"]
        cache_total = max(
            int(cache_stats.get("hits", 0)) + int(cache_stats.get("misses", 0)),
            1,
        )
        out["cache"]["hit_rate"] = round(float(cache_stats.get("hits", 0)) / cache_total, 4)
        out["cache"]["l2_share_of_hits"] = round(
            float(cache_stats.get("l2_hits", 0)) / max(int(cache_stats.get("hits", 0)), 1),
            4,
        )
```

Why: at scale you need to know whether Redis is helping or whether all hits are only process-local.

- [ ] **Step 4: Run metrics tests**

Run:

```bash
pytest tests/test_observability.py tests/test_search_telemetry.py -v
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add pipeline/metrics.py tests/test_observability.py
git commit -m "feat(cache): expose distributed cache metrics"
```

---

### Task 9: Update Cache Documentation

**Files:**
- Modify: `docs/toggle.md`
- Modify: `docs/storage.md`
- Modify: `docs/scaling.md`

- [ ] **Step 1: Update `docs/toggle.md` cache section**

Replace the `use_cache` section with text that states:

```markdown
### 3. `use_cache` - *step 3*

**What it does:** Enables cache-aside reuse for planner specs, query embeddings,
HyDE strings, and short-lived insight inputs.

**Backend behavior:**
- `CACHE_BACKEND=memory`: process-local LRU only.
- `CACHE_BACKEND=redis`: process-local L1 plus Redis L2 shared by workers/nodes.

**Why it matters:** Identical planner inputs skip the LLM, and identical semantic
queries skip embedding. Redis keeps this benefit when the API runs more than one
worker or node.

**Correctness guardrails:** Keys include version constants and model/provider
identity. Full search responses are not cached by default because candidate data,
personalization, and impression IDs are mutable.
```

- [ ] **Step 2: Update `docs/storage.md` cache section**

Add this table:

```markdown
| Layer | Location | Lifetime | Purpose |
|---|---|---|---|
| L1 | Process memory in `pipeline/cache.py` | TTL plus process lifetime | Fastest hot-path hits inside one worker |
| L2 | Redis, when `CACHE_BACKEND=redis` | TTL, survives app restarts | Shared cache across workers and app nodes |
```

Also document the Redis key prefix:

```markdown
Redis keys are prefixed as `{CACHE_NAMESPACE}:{LANGFUSE_ENVIRONMENT}:{logical_key}`
so staging and production never share entries.
```

- [ ] **Step 3: Update `docs/scaling.md` deployment sections**

Add these scale notes:

```markdown
For one app process, `CACHE_BACKEND=memory` is enough. For two or more workers
or app nodes, use `CACHE_BACKEND=redis`; otherwise every worker rebuilds its own
planner and embedding cache.

Redis sizing rule of thumb:
- Planner entry: usually 2-8 KB.
- 384-dim embedding entry as JSON: roughly 4-8 KB.
- Insight evidence payload: usually 5-50 KB and only lives 5 minutes.

Production Redis should run on a private network with AUTH/TLS where available,
memory limits, and an eviction policy such as `allkeys-lru` or `volatile-ttl`.
The app remains available if Redis is down, but cache hit rate and latency will
temporarily degrade.
```

- [ ] **Step 4: Run documentation grep checks**

Run:

```bash
rg -n "CACHE_BACKEND|REDIS_URL|L1|L2|Redis" docs .env.example docker-compose.yml
```

Expected: the new settings appear in docs and local config.

- [ ] **Step 5: Commit**

```bash
git add docs/toggle.md docs/storage.md docs/scaling.md
git commit -m "docs(cache): document distributed cache deployment"
```

---

### Task 10: End-To-End Verification

**Files:**
- No new files.

- [ ] **Step 1: Run unit tests for cache and planner**

Run:

```bash
pytest tests/test_cache_backend.py tests/test_config_contracts.py tests/test_planner_fallback_control.py tests/test_personalization_planner.py -v
```

Expected: pass.

- [ ] **Step 2: Run API helper and observability tests**

Run:

```bash
pytest tests/test_admin_api_helpers.py tests/test_api_lifespan.py tests/test_observability.py -v
```

Expected: pass.

- [ ] **Step 3: Run the broader test suite**

Run:

```bash
pytest -q
```

Expected: pass. If unrelated tests fail because local services are not running, record the exact failing command and error in the final implementation note.

- [ ] **Step 4: Verify memory backend manually**

Run the API with default memory cache:

```bash
venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

In another terminal, run the same search twice:

```bash
curl -s http://127.0.0.1:8000/metrics
```

Expected after repeated searches:

- `cache.hits` increases.
- `cache.l1_hits` increases.
- `cache.redis_errors` remains `0`.

- [ ] **Step 5: Verify Redis backend locally**

Start Redis:

```bash
docker compose up -d redis
```

Run the API with Redis enabled:

```bash
CACHE_BACKEND=redis REDIS_URL=redis://localhost:6379 venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8000
```

Run the same search once, restart the API, then run it again.

Expected:

- After restart, `cache.l2_hits` increases on repeated planner or embedding inputs.
- Search still succeeds if Redis is stopped after startup.
- `cache.redis_errors` increases only when Redis is intentionally stopped.

- [ ] **Step 6: Run a focused benchmark**

Run:

```bash
./venv/bin/python scripts/benchmark_candidate_query_modes.py --sample-size 7 --modes no-llm,agent-quality --top-k 10
```

Expected:

- Cold run has higher planner or embedding time.
- Warm run has lower planner or embedding time.
- No quality metric changes caused by caching.

- [ ] **Step 7: Commit final verification fixes**

If verification required small fixes:

```bash
git add <fixed-files>
git commit -m "fix(cache): complete distributed cache verification"
```

If no fixes were needed, do not create an empty commit.

---

## Scale Deployment Plan

### Phase A: Current Local Or Single-Worker Deploy

Use:

```dotenv
CACHE_BACKEND=memory
REDIS_URL=
CACHE_LOCAL_MAXSIZE=2048
```

Why: it preserves today's behavior and avoids introducing Redis operational work before it is needed.

### Phase B: Multiple Uvicorn Workers On One Host

Use:

```dotenv
CACHE_BACKEND=redis
REDIS_URL=redis://127.0.0.1:6379
CACHE_LOCAL_MAXSIZE=2048
```

Why: worker A can compute a planner spec once, then worker B can reuse it through Redis. L1 still protects Redis from being called on every repeated request inside the same worker.

### Phase C: Multiple App Nodes

Use managed or private-network Redis:

```dotenv
CACHE_BACKEND=redis
REDIS_URL=redis://:<password>@cache.internal:6379/0
CACHE_NAMESPACE=hybrid-search
LANGFUSE_ENVIRONMENT=production
```

Operational requirements:

- Redis must be reachable only from app infrastructure.
- Enable Redis AUTH and TLS if the provider supports it.
- Set a memory limit and eviction policy.
- Monitor Redis p95 latency and error rate.
- Keep the app fallback behavior enabled so Redis outages degrade latency, not availability.

### Phase D: High-QPS Future

If cold-start cache stampedes show up in metrics, add distributed locking around `get_or_set` for `plan:` and `embed:` keys:

```text
SET lock:{key} token NX EX 30
```

The lock holder computes and writes the value. Other workers poll the value for a short jittered window, then compute locally if the lock holder fails. This is only worth adding after metrics show duplicate cold computations across nodes.

### Rollout Checklist

- Deploy code with `CACHE_BACKEND=memory`.
- Verify no regressions in `/metrics`.
- Enable Redis in staging.
- Run repeated-search benchmark before and after API restart.
- Check `cache.l2_hits > 0` after restart.
- Check `cache.redis_errors == 0` under normal load.
- Enable Redis in production during low traffic.
- Watch p95 search latency, planner LLM calls, embedding cache hits, and Redis errors for the first day.

### Success Metrics

- Planner cache hit rate: at least 30% on repeated production-like traffic.
- Embedding cache hit rate: at least 30% on repeated production-like traffic.
- Redis p95 latency: under 20 ms from app nodes.
- Redis error rate: under 0.1% of cache operations.
- Search correctness: benchmark ranking metrics unchanged between cache cold and cache warm runs.

## Self-Review

Spec coverage:

- Existing process cache is preserved as L1.
- Redis is added as optional L2 for scaled deployment.
- Planner, embedding, and insights cache call sites are covered.
- Observability, docs, local Docker, and production rollout are covered.
- Full search response caching is intentionally excluded for correctness.

Placeholder scan:

- No task depends on unspecified files or unnamed behavior.
- Each code-changing task includes exact code snippets and commands.

Type consistency:

- Cache `get`, `set`, `invalidate`, `clear_namespace`, and `get_or_set` are async.
- Cache `clear_all` remains sync for local test cleanup.
- Insights Redis payload uses `CandidateInsightInput` dicts, not `SearchResult` instances.

