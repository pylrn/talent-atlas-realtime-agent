from pathlib import Path
import tomllib

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _env_value(text: str, name: str) -> str:
    prefix = f"{name}="
    for line in text.splitlines():
        if line.startswith(prefix):
            return line.removeprefix(prefix).strip()
    raise AssertionError(f"{name} not found")


def test_default_embedding_dimensions_match_fresh_database_schema():
    env_example = (ROOT / ".env.example").read_text()
    tables_sql = (ROOT / "db/migrations/002_tables.sql").read_text()

    assert _env_value(env_example, "EMBEDDING_PROVIDER") == "local"
    assert _env_value(env_example, "EMBEDDING_MODEL") == "all-MiniLM-L6-v2"
    assert _env_value(env_example, "EMBEDDING_DIMENSIONS") == "384"
    assert "embedding     VECTOR(384)" in tables_sql


def test_env_example_parses_with_empty_pending_embedding_values(monkeypatch):
    from pipeline import Settings

    monkeypatch.delenv("PENDING_EMBEDDING_PROVIDER", raising=False)
    monkeypatch.delenv("PENDING_EMBEDDING_MODEL", raising=False)
    monkeypatch.delenv("PENDING_EMBEDDING_DIMENSIONS", raising=False)
    loaded = Settings(_env_file=ROOT / ".env.example")

    assert loaded.pending_embedding_provider is None
    assert loaded.pending_embedding_model is None
    assert loaded.pending_embedding_dimensions is None


def test_default_local_embedder_dependency_is_installed_by_project():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dependencies = pyproject["project"]["dependencies"]

    assert any(dep.startswith("sentence-transformers") for dep in dependencies)


def test_implicit_outcome_personalization_is_disabled_by_default(monkeypatch):
    from pipeline import Settings
    from pipeline.config import SEARCH_CONFIG

    monkeypatch.delenv("USE_PERSONALIZATION", raising=False)
    loaded = Settings(_env_file=ROOT / ".env.example")

    assert loaded.use_personalization is False
    assert SEARCH_CONFIG["use_personalization"] is False


def test_search_database_and_bm25_backend_defaults_are_safe(monkeypatch):
    from pipeline import Settings

    monkeypatch.delenv("SEARCH_DATABASE_URL", raising=False)
    monkeypatch.delenv("BM25_BACKEND", raising=False)
    loaded = Settings(_env_file=ROOT / ".env.example")

    assert loaded.search_database_url == ""
    assert loaded.bm25_backend == "fts"


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
    assert loaded.search_use_retrieval_cache is False


def test_retrieval_cache_env_switch_flows_into_mode_config(monkeypatch):
    from pipeline import settings
    from pipeline.modes import get_config

    monkeypatch.setattr(settings, "search_use_retrieval_cache", True)

    assert get_config("no-llm")["use_retrieval_cache"] is True
    assert get_config("no-llm", overrides={"use_retrieval_cache": False})["use_retrieval_cache"] is False


def test_cache_backend_accepts_only_memory_or_redis(monkeypatch):
    from pydantic import ValidationError
    from pipeline import Settings

    monkeypatch.setenv("CACHE_BACKEND", "not-a-cache")

    with pytest.raises(ValidationError, match="cache_backend"):
        Settings(_env_file=ROOT / ".env.example")
