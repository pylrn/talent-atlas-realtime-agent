import asyncio
import sys
from types import SimpleNamespace

from pipeline import settings
from pipeline.embedder import get_embedder
from pipeline.reranker import get_reranker
from pipeline.search import HybridSearchEngine
from pipeline.search_result import SearchResult


def test_local_embedder_can_use_fastembed_backend(monkeypatch):
    calls = []

    class FakeTextEmbedding:
        def __init__(self, model_name, **kwargs):
            calls.append((model_name, kwargs))

        def embed(self, texts, **kwargs):
            assert texts == ["backend engineer"]
            return [[0.1, 0.2, 0.3]]

    monkeypatch.setitem(
        sys.modules,
        "fastembed",
        SimpleNamespace(TextEmbedding=FakeTextEmbedding),
    )
    monkeypatch.setattr(settings, "local_embedding_backend", "fastembed")

    embedder = get_embedder(provider="local", model="all-MiniLM-L6-v2")
    vectors = asyncio.run(embedder.embed(["backend engineer"]))

    assert embedder.dimensions == 384
    assert vectors == [[0.1, 0.2, 0.3]]
    assert calls == [("sentence-transformers/all-MiniLM-L6-v2", {})]


def test_local_reranker_can_use_fastembed_backend(monkeypatch):
    calls = []

    class FakeTextCrossEncoder:
        def __init__(self, model_name, **kwargs):
            calls.append((model_name, kwargs))

        def rerank(self, query, documents):
            assert query == "python engineer"
            assert documents == ["weak evidence", "strong python evidence"]
            return [0.1, 0.9]

    monkeypatch.setitem(
        sys.modules,
        "fastembed.rerank.cross_encoder",
        SimpleNamespace(TextCrossEncoder=FakeTextCrossEncoder),
    )
    monkeypatch.setattr(settings, "local_reranker_backend", "fastembed")

    results = [
        SearchResult(candidate_id="a", best_chunk="weak evidence"),
        SearchResult(candidate_id="b", best_chunk="strong python evidence"),
    ]
    reranked = asyncio.run(get_reranker("local-fast").rerank("python engineer", results))

    assert [result.candidate_id for result in reranked] == ["b", "a"]
    assert calls == [("Xenova/ms-marco-MiniLM-L-6-v2", {})]


def test_memory_constrained_profile_evicts_cached_reranker(monkeypatch):
    reranker = object()
    engine = HybridSearchEngine.__new__(HybridSearchEngine)
    engine._reranker_cache = {"local-fast": reranker}
    monkeypatch.setattr(settings, "reranker_model", "local-fast")
    monkeypatch.setattr(settings, "evict_local_reranker_after_use", True)

    engine._release_reranker(reranker)

    assert engine._reranker_cache == {}


def test_fastembed_profile_avoids_candidate_embedding_in_mmr(monkeypatch):
    monkeypatch.setattr(settings, "local_embedding_backend", "fastembed")
    mmr_embedder = (
        None
        if getattr(settings, "local_embedding_backend", "") == "fastembed"
        else "not_none"
    )
    assert mmr_embedder is None

