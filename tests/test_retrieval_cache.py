import pytest
import asyncpg

from pipeline import cache as _cache
from pipeline.retrieve_bm25 import retrieve_bm25
from pipeline.retrieve_skill import retrieve_skill
from pipeline.search import HybridSearchEngine
from pipeline.spec import CanonicalSearchSpec, MustFilters, ShouldFilters


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _SkillConn:
    def __init__(self):
        self.fetch_calls = 0

    async def fetch(self, sql, *params):
        self.fetch_calls += 1
        return [{
            "chunk_id": "chunk-1",
            "candidate_id": "candidate-1",
            "content": "",
            "document_id": "doc-1",
            "doc_type": "resume",
            "document_title": "Resume",
            "distance": None,
            "bm25_score": None,
            "skill_score": 2.0,
        }]


class _CountConn:
    def __init__(self):
        self.fetchval_calls = 0

    async def fetchval(self, sql, *params):
        self.fetchval_calls += 1
        return 42


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return _Acquire(self.conn)


class _CancelingFetchPool:
    def __init__(self):
        self.fetch_calls = 0

    async def fetch(self, sql, *params):
        self.fetch_calls += 1
        raise asyncpg.exceptions.QueryCanceledError("statement timeout")


@pytest.fixture(autouse=True)
def reset_cache():
    _cache.configure_for_tests(redis_client=None)
    _cache.clear_all()
    yield
    _cache.configure_for_tests(redis_client=None)
    _cache.clear_all()


@pytest.mark.asyncio
async def test_skill_rowset_cache_requires_flag_and_deferred_content():
    spec = CanonicalSearchSpec(
        input_type="query",
        intent="candidate_search",
        must=MustFilters(skills=["python"]),
        should=ShouldFilters(skills=["fastapi"]),
    )
    conn = _SkillConn()
    pool = _Pool(conn)

    first_timings = {}
    first = await retrieve_skill(
        spec,
        "status = $1",
        ["active"],
        pool,
        top_k=10,
        include_content=False,
        use_retrieval_cache=True,
        timings=first_timings,
    )
    second_timings = {}
    second = await retrieve_skill(
        spec,
        "status = $1",
        ["active"],
        pool,
        top_k=10,
        include_content=False,
        use_retrieval_cache=True,
        timings=second_timings,
    )

    assert first == second
    assert conn.fetch_calls == 1
    assert first_timings["cache_namespace"] == "skill"
    assert first_timings["rowset_cache_hit"] is False
    assert "rowset_cache_lookup_ms" in first_timings
    assert second_timings["cache_namespace"] == "skill"
    assert second_timings["rowset_cache_hit"] is True

    content_timings = {}
    await retrieve_skill(
        spec,
        "status = $1",
        ["active"],
        pool,
        top_k=10,
        include_content=True,
        use_retrieval_cache=True,
        timings=content_timings,
    )

    assert conn.fetch_calls == 2
    assert content_timings["rowset_cache_skipped_reason"] == "include_content_true"


@pytest.mark.asyncio
async def test_count_filtered_cache_is_opt_in():
    conn = _CountConn()
    engine = HybridSearchEngine.__new__(HybridSearchEngine)
    engine.search_pool = _Pool(conn)

    assert await engine._count_filtered("status = $1", ["active"], use_cache=False) == 42
    assert await engine._count_filtered("status = $1", ["active"], use_cache=False) == 42
    assert conn.fetchval_calls == 2

    assert await engine._count_filtered("status = $1", ["active"], use_cache=True) == 42
    assert await engine._count_filtered("status = $1", ["active"], use_cache=True) == 42
    assert conn.fetchval_calls == 3


@pytest.mark.asyncio
async def test_bm25_timeout_negative_cache_skips_repeat_fetch():
    spec = CanonicalSearchSpec(
        input_type="query",
        intent="candidate_search",
        must=MustFilters(),
        should=ShouldFilters(),
        lexical_terms=["benchmark"],
        semantic_query="benchmark",
    )
    pool = _CancelingFetchPool()

    with pytest.raises(asyncpg.exceptions.QueryCanceledError):
        await retrieve_bm25(
            spec,
            "TRUE",
            [],
            pool,
            top_k=10,
            include_content=False,
            statement_timeout_ms=100,
            use_retrieval_cache=True,
            timings={},
        )

    timings = {}
    rows = await retrieve_bm25(
        spec,
        "TRUE",
        [],
        pool,
        top_k=10,
        include_content=False,
        statement_timeout_ms=100,
        use_retrieval_cache=True,
        timings=timings,
    )

    assert rows == []
    assert pool.fetch_calls == 1
    assert timings["negative_cache_hit"] is True
    assert timings["negative_cache_reason"] == "timeout"
