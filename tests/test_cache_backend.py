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
    assert stats["l1_hits"] == 1
    assert stats["sets"] == 1
    namespace_stats = cache.cache_namespace_stats()
    assert namespace_stats["plan"]["sets"] == 1
    assert namespace_stats["plan"]["l1_hits"] == 1
    assert namespace_stats["plan"]["hit_rate"] == 1.0


@pytest.mark.asyncio
async def test_redis_hit_hydrates_local_lru():
    fake = FakeRedis()
    cache.configure_for_tests(redis_client=fake, namespace="test")
    key = cache.plan_key("sales", "{}", "query_mode|llm|test|model")
    fake.store[cache.redis_key_for_tests(key)] = json.dumps({"semantic_query": "sales"})

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
    await cache.set(cache.retrieval_key("count", {"where": "TRUE"}), 7)
    await cache.set(cache.retrieval_key("dense", {"q": "x"}), [])
    await cache.set(cache.bm25_negative_key({"q": "x"}), {"reason": "timeout"})
    await cache.set(cache.retrieval_key("skill", {"q": "x"}), [])
    await cache.set(cache.skill_recency_key(["c1"], ["python"]), {"c1": 3.0})

    expiries = sorted(fake.expiry.values())
    assert expiries == sorted([60, 60, 120, 300, 300, 300, 86_400, 2_592_000])


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


@pytest.mark.asyncio
async def test_cache_snapshot_groups_namespaces():
    await cache.set(cache.embed_key("python", provider="local", dimensions=384), [0.1])
    await cache.get(cache.embed_key("python", provider="local", dimensions=384))
    await cache.get(cache.retrieval_key("dense", {"q": "missing"}))

    snapshot = cache.cache_snapshot()

    assert snapshot["backend"] == "memory"
    assert snapshot["summary"]["hits"] == 1
    assert snapshot["namespaces"]["embed"]["hits"] == 1
    assert snapshot["namespaces"]["dense"]["misses"] == 1


@pytest.mark.asyncio
async def test_cache_delta_reports_request_local_namespace_rates():
    before = cache.cache_snapshot()

    key = cache.embed_key("python", provider="local", dimensions=384)
    await cache.set(key, [0.1])
    await cache.get(key)
    await cache.get(cache.retrieval_key("dense", {"q": "missing"}))

    delta = cache.cache_delta(before, cache.cache_snapshot())

    assert delta["summary"]["sets"] == 1
    assert delta["summary"]["l1_hits"] == 1
    assert delta["summary"]["misses"] == 1
    assert delta["summary"]["hit_rate"] == 0.5
    assert delta["namespaces"]["embed"]["hits"] == 1
    assert delta["namespaces"]["dense"]["misses"] == 1


def test_skill_recency_key_is_order_stable_and_skill_sensitive():
    first = cache.skill_recency_key(["c2", "c1"], ["FastAPI", "python"])
    second = cache.skill_recency_key(["c1", "c2"], ["python", "fastapi"])
    third = cache.skill_recency_key(["c1", "c2"], ["python"])

    assert first == second
    assert first != third
