import pytest


@pytest.mark.asyncio
async def test_get_search_pool_falls_back_to_primary_pool(monkeypatch):
    from pipeline import database

    pool = object()

    async def fake_get_pool():
        return pool

    monkeypatch.setattr(database.settings, "search_database_url", "")
    monkeypatch.setattr(database, "get_pool", fake_get_pool)
    monkeypatch.setattr(database, "_search_pool", None)

    assert await database.get_search_pool() is pool


@pytest.mark.asyncio
async def test_get_search_pool_uses_separate_dsn_and_pool_sizes(monkeypatch):
    from pipeline import database

    pool = object()
    captured = {}

    async def fake_create_pool(**kwargs):
        captured.update(kwargs)
        return pool

    monkeypatch.setattr(database.settings, "search_database_url", "postgresql://search-db")
    monkeypatch.setattr(database.settings, "db_pool_min", 5)
    monkeypatch.setattr(database.settings, "db_pool_max", 20)
    monkeypatch.setattr(database.settings, "search_db_pool_min", 2)
    monkeypatch.setattr(database.settings, "search_db_pool_max", 7)
    monkeypatch.setattr(database, "_search_pool", None)
    monkeypatch.setattr(database, "create_pool", fake_create_pool)

    assert await database.get_search_pool() is pool
    assert captured == {
        "dsn": "postgresql://search-db",
        "min_size": 2,
        "max_size": 7,
        "label": "search",
    }
