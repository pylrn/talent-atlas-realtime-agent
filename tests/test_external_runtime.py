from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_compose_uses_project_local_postgres_bind_mount() -> None:
    compose = (ROOT / "docker-compose.yml").read_text()

    assert "./.runtime/postgres:/var/lib/postgresql/data" in compose
    assert "pgdata:" not in compose


def test_setup_exports_heavy_caches_to_project_runtime() -> None:
    setup = (ROOT / "scripts" / "setup_external_runtime.sh").read_text()

    for name in [
        "HF_HOME",
        "TRANSFORMERS_CACHE",
        "SENTENCE_TRANSFORMERS_HOME",
        "TORCH_HOME",
        "PIP_CACHE_DIR",
        "UV_CACHE_DIR",
        "XDG_CACHE_HOME",
        "TMPDIR",
    ]:
        assert f"export {name}=" in setup

    assert "127.0.0.1:5432/hiring_platform" in setup
    assert "export SEARCH_DATABASE_URL=" in setup
    assert "export LANGFUSE_ENABLED=" in setup
    assert "export OTEL_ENABLED=" in setup


def test_setup_rejects_non_external_project_root() -> None:
    setup = (ROOT / "scripts" / "setup_external_runtime.sh").read_text()

    assert '"$PROJECT_ROOT" != /Volumes/MAC/*' in setup
