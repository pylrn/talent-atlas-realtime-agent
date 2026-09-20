import pytest

from api import main as api_main


@pytest.mark.asyncio
async def test_lifespan_reuses_one_embedder_for_search_and_ingestion(monkeypatch):
    pool = object()
    embedder = object()

    async def fake_get_pool():
        return pool

    async def fake_close_pool():
        return None

    class FakeSearchEngine:
        def __init__(self, pool_arg, *, embedder, **kwargs):
            self.pool = pool_arg
            self.embedder = embedder

    class FakeIngestionPipeline:
        def __init__(self, pool_arg, *, embedder):
            self.pool = pool_arg
            self.embedder = embedder

    monkeypatch.setattr(api_main, "get_pool", fake_get_pool)
    monkeypatch.setattr(api_main, "close_pool", fake_close_pool)
    monkeypatch.setattr(api_main, "get_embedder", lambda: embedder, raising=False)
    monkeypatch.setattr(api_main, "HybridSearchEngine", FakeSearchEngine)
    monkeypatch.setattr(api_main, "IngestionPipeline", FakeIngestionPipeline)

    async with api_main.lifespan(api_main.app):
        assert api_main.app.state.pool is pool
        assert api_main.app.state.search_engine.embedder is embedder
        assert api_main.app.state.ingestion.embedder is embedder
