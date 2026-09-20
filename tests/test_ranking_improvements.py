from __future__ import annotations

import pytest

from pipeline import cache as _cache
from pipeline.diversity import mmr_select_async
from pipeline.feature_ranker import _skill_recency_score
from pipeline.search import HybridSearchEngine, _auto_relax_spec, _spec_from_explicit_filters
from pipeline.search_result import SearchResult
from pipeline.spec import CanonicalSearchSpec, MustFilters, MustNotFilters, ShouldFilters


def test_skill_recency_score_defaults_to_neutral_when_missing():
    assert _skill_recency_score(None) == 0.7


def test_skill_recency_score_decays_over_time():
    assert _skill_recency_score(30) > _skill_recency_score(400)
    assert _skill_recency_score(800) == 0.0


def test_spec_from_explicit_filters_keeps_skills_match():
    spec = _spec_from_explicit_filters({
        "skills": ["python", "fastapi"],
        "skills_match": "or",
    })

    assert spec.must.skills == ["python", "fastapi"]
    assert spec.must.skills_match == "or"


def test_auto_relax_switches_skill_match_before_dropping_filters():
    spec = CanonicalSearchSpec(
        input_type="query",
        intent="candidate_search",
        must=MustFilters(skills=["python", "fastapi", "postgres"], skills_match="and"),
        should=ShouldFilters(),
        must_not=MustNotFilters(),
        semantic_query="python fastapi postgres backend engineer",
    )

    relaxed, relaxations = _auto_relax_spec(spec)

    assert relaxed is not None
    assert relaxed.must.skills_match == "or"
    assert relaxations[0]["field"] == "skills_match"


def test_auto_relax_demotes_location_to_soft_preference():
    spec = CanonicalSearchSpec(
        input_type="query",
        intent="candidate_search",
        must=MustFilters(skills=["python"], city="berlin", country="germany"),
        should=ShouldFilters(),
        must_not=MustNotFilters(),
        semantic_query="python engineer berlin",
    )

    relaxed, relaxations = _auto_relax_spec(spec)

    assert relaxed is not None
    assert relaxed.must.city is None
    assert relaxed.must.country is None
    assert relaxed.should.locations == ["berlin", "germany"]
    assert relaxations[0]["field"] == "location"


class _Acquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _ProfileConn:
    def __init__(self):
        self.fetch_calls = 0

    async def fetch(self, sql, *params):
        self.fetch_calls += 1
        return [{
            "candidate_id": "candidate-1",
            "full_name": "Ada Lovelace",
            "email": "ada@example.com",
            "city": "Berlin",
            "country": "Germany",
            "salary_min": 100000,
            "salary_max": 130000,
            "years_exp": 8,
            "skills": ["python", "fastapi"],
            "updated_at": None,
        }]


class _SearchPool:
    def __init__(self):
        self.conn = _ProfileConn()
        self.recency_fetch_calls = 0

    def acquire(self):
        return _Acquire(self.conn)

    async def fetch(self, sql, *params):
        if "FROM candidate_skills" in sql:
            self.recency_fetch_calls += 1
            return [{
                "candidate_id": "candidate-1",
                "matched_skill_recency_days": 42.0,
            }]
        return []


@pytest.mark.asyncio
async def test_enrich_candidates_populates_matched_skill_recency():
    engine = HybridSearchEngine.__new__(HybridSearchEngine)
    engine.search_pool = _SearchPool()

    result = SearchResult(candidate_id="candidate-1")
    spec = CanonicalSearchSpec(
        input_type="query",
        intent="candidate_search",
        must=MustFilters(skills=["python"]),
        should=ShouldFilters(skills=["fastapi"]),
        must_not=MustNotFilters(),
        semantic_query="python fastapi engineer",
    )

    enriched = await engine._enrich_candidates([result], spec=spec, use_cache=False)

    assert enriched[0].full_name == "Ada Lovelace"
    assert enriched[0].matched_skill_recency_days == 42.0


@pytest.mark.asyncio
async def test_enrich_candidates_caches_matched_skill_recency():
    _cache.configure_for_tests(redis_client=None)
    _cache.clear_all()
    try:
        engine = HybridSearchEngine.__new__(HybridSearchEngine)
        pool = _SearchPool()
        engine.search_pool = pool
        spec = CanonicalSearchSpec(
            input_type="query",
            intent="candidate_search",
            must=MustFilters(skills=["python"]),
            should=ShouldFilters(skills=["fastapi"]),
            must_not=MustNotFilters(),
            semantic_query="python fastapi engineer",
        )

        first_timings = {}
        first = await engine._enrich_candidates(
            [SearchResult(candidate_id="candidate-1")],
            spec=spec,
            timings=first_timings,
            use_cache=True,
        )
        second_timings = {}
        second = await engine._enrich_candidates(
            [SearchResult(candidate_id="candidate-1")],
            spec=spec,
            timings=second_timings,
            use_cache=True,
        )

        assert first[0].matched_skill_recency_days == 42.0
        assert second[0].matched_skill_recency_days == 42.0
        assert pool.conn.fetch_calls == 1
        assert pool.recency_fetch_calls == 1
        assert first_timings["skill_recency_cache_hit"] is False
        assert second_timings["skill_recency_cache_hit"] is True
        assert second_timings["cache_hits"] == 1
        assert second_timings["cache_misses"] == 0
    finally:
        _cache.configure_for_tests(redis_client=None)
        _cache.clear_all()


class _FakeEmbedder:
    name = "test/embedder"
    dimensions = 3

    async def embed(self, texts):
        vectors = []
        for text in texts:
            if "rust" in text:
                vectors.append([0.0, 1.0, 0.0])
            else:
                vectors.append([1.0, 0.0, 0.0])
        return vectors


@pytest.mark.asyncio
async def test_mmr_embedding_similarity_promotes_semantic_diversity():
    _cache.configure_for_tests(redis_client=None)
    _cache.clear_all()
    try:
        results = [
            SearchResult(candidate_id="dup1", feature_score=95, skills=["python"], best_chunk="python backend apis"),
            SearchResult(candidate_id="dup2", feature_score=94, skills=["python"], best_chunk="python backend services"),
            SearchResult(candidate_id="diff", feature_score=80, skills=["rust"], best_chunk="rust systems programming"),
        ]

        out = await mmr_select_async(results, top_k=2, lam=0.5, embedder=_FakeEmbedder(), use_cache=True)
    finally:
        _cache.configure_for_tests(redis_client=None)
        _cache.clear_all()

    ids = [item.candidate_id for item in out]
    assert "dup1" in ids
    assert "diff" in ids
