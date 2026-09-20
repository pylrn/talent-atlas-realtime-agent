import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from api import main as api_main
from pipeline import settings

def test_model_settings_endpoint_masks_keys_and_reports_active_and_pending(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("GOOGLE_API_KEY=AIzaSySecretValue123\n", encoding="utf-8")
    monkeypatch.setattr(api_main, "MODEL_ENV_FILE", env_path)
    monkeypatch.setattr(settings, "embedding_provider", "local")
    monkeypatch.setattr(settings, "embedding_model", "all-MiniLM-L6-v2")
    monkeypatch.setattr(settings, "embedding_dimensions", 384)
    monkeypatch.setattr(settings, "reranker_model", settings.reranker_model)
    monkeypatch.setattr(
        settings,
        "pending_embedding_provider",
        settings.pending_embedding_provider,
    )
    monkeypatch.setattr(settings, "pending_embedding_model", settings.pending_embedding_model)
    monkeypatch.setattr(
        settings,
        "pending_embedding_dimensions",
        settings.pending_embedding_dimensions,
    )
    monkeypatch.setattr(settings, "google_api_key", settings.google_api_key)
    monkeypatch.setattr(settings, "pending_embedding_provider", "openai", raising=False)
    monkeypatch.setattr(settings, "pending_embedding_model", "text-embedding-3-small", raising=False)
    monkeypatch.setattr(settings, "pending_embedding_dimensions", 1536, raising=False)
    monkeypatch.setattr(settings, "google_api_key", "AIzaSySecretValue123")

    response = TestClient(api_main.app).get("/admin/model-settings")

    assert response.status_code == 200
    body = response.json()
    assert body["embedding"]["active"] == {
        "provider": "local",
        "model": "all-MiniLM-L6-v2",
        "dimensions": 384,
    }
    assert body["embedding"]["pending"] == {
        "provider": "openai",
        "model": "text-embedding-3-small",
        "dimensions": 1536,
        "requires_rebuild": True,
    }
    assert body["api_keys"]["google_api_key"]["configured"] is True
    assert body["api_keys"]["google_api_key"]["masked"] != "AIzaSySecretValue123"
    assert "llm" in body
    assert "provider" in body["llm"]
    assert "model" in body["llm"]
    assert "llm_models" in body["catalogs"]
    assert "groq" in body["catalogs"]["llm_models"]
    assert "AIzaSySecretValue123" not in response.text


def test_model_settings_prefers_live_supported_models_for_configured_keys(monkeypatch):
    monkeypatch.setattr(settings, "google_api_key", "configured-google-key", raising=False)
    monkeypatch.setattr(settings, "groq_api_key", "configured-groq-key", raising=False)
    monkeypatch.setattr(
        api_main,
        "_available_gemini_llm_models",
        lambda: {
            "gemini-3.1-pro-preview": {
                "description": "Available via configured Gemini API key."
            }
        },
        raising=False,
    )
    monkeypatch.setattr(
        api_main,
        "_available_groq_llm_models",
        lambda: {
            "openai/gpt-oss-120b": {
                "description": "Available via configured Groq API key."
            }
        },
        raising=False,
    )

    response = TestClient(api_main.app).get("/admin/model-settings")

    assert response.status_code == 200
    models = response.json()["catalogs"]["llm_models"]
    assert "gemini-3.1-pro-preview" in models["gemini"]
    assert "gemini-2.5-flash-lite" not in models["gemini"]
    assert "openai/gpt-oss-120b" in models["groq"]
    assert "llama-3.3-70b-versatile" not in models["groq"]


def test_model_settings_falls_back_to_curated_models_when_live_list_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "google_api_key", "configured-google-key", raising=False)
    monkeypatch.setattr(api_main, "_available_gemini_llm_models", lambda: {}, raising=False)

    response = TestClient(api_main.app).get("/admin/model-settings")

    assert response.status_code == 200
    assert "gemini-2.5-flash-lite" in response.json()["catalogs"]["llm_models"]["gemini"]


def test_gemini_catalog_filter_excludes_specialty_non_planner_models():
    assert api_main._is_gemini_llm_model("gemini-3.5-flash") is True
    assert api_main._is_gemini_llm_model("gemini-2.5-computer-use-preview-10-2025") is False
    assert api_main._is_gemini_llm_model("deep-research-preview-04-2026") is False
    assert api_main._is_gemini_llm_model("antigravity-preview-05-2026") is False


def test_gemini_live_catalog_exposes_thinking_level_support(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "models": [
                    {
                        "name": "models/gemini-2.5-flash",
                        "supportedGenerationMethods": ["generateContent"],
                        "thinking": True,
                    },
                    {
                        "name": "models/gemini-3.5-flash",
                        "supportedGenerationMethods": ["generateContent"],
                        "thinking": True,
                    },
                    {
                        "name": "models/gemini-2.0-flash",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                ]
            }

    def fake_get(*args, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(settings, "google_api_key", "configured-google-key", raising=False)

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)

    models = api_main._available_gemini_llm_models()

    assert models["gemini-2.5-flash"]["supports_thinking"] is False
    assert models["gemini-3.5-flash"]["supports_thinking"] is True
    assert models["gemini-2.0-flash"]["supports_thinking"] is False


def test_model_settings_save_stages_embedding_and_writes_runtime_safe_env(
    monkeypatch,
    tmp_path,
):
    env_path = tmp_path / ".env"
    env_path.write_text("EMBEDDING_PROVIDER=local\nEMBEDDING_MODEL=all-MiniLM-L6-v2\n", encoding="utf-8")
    monkeypatch.setattr(api_main, "MODEL_ENV_FILE", env_path)
    monkeypatch.setattr(settings, "embedding_provider", "local")
    monkeypatch.setattr(settings, "embedding_model", "all-MiniLM-L6-v2")
    monkeypatch.setattr(settings, "embedding_dimensions", 384)

    payload = {
        "pending_embedding_provider": "openai",
        "pending_embedding_model": "text-embedding-3-small",
        "pending_embedding_dimensions": 1536,
        "reranker_model": "local-balanced",
        "api_keys": {"google_api_key": "AIzaSyNewSecretValue456"},
    }
    response = TestClient(api_main.app).post("/admin/model-settings", json=payload)

    assert response.status_code == 200
    assert "AIzaSyNewSecretValue456" not in response.text
    assert settings.embedding_provider == "local"
    assert settings.embedding_model == "all-MiniLM-L6-v2"
    assert settings.reranker_model == "local-balanced"
    assert settings.pending_embedding_provider == "openai"

    env_text = env_path.read_text(encoding="utf-8")
    assert "EMBEDDING_PROVIDER=local" in env_text
    assert "PENDING_EMBEDDING_PROVIDER=openai" in env_text
    assert "PENDING_EMBEDDING_MODEL=text-embedding-3-small" in env_text
    assert "PENDING_EMBEDDING_DIMENSIONS=1536" in env_text
    assert "LLM_MODEL" not in env_text
    assert "GOOGLE_API_KEY=AIzaSyNewSecretValue456" in env_text


def test_reranker_cache_reuses_loaded_instance(monkeypatch):
    calls = []
    reranker = object()
    api_main.app.state.reranker_cache = {}

    def fake_get_reranker(choice):
        calls.append(choice)
        return reranker

    monkeypatch.setattr(api_main, "get_reranker", fake_get_reranker)

    first = api_main._get_cached_reranker(api_main.app, "local-fast")
    second = api_main._get_cached_reranker(api_main.app, "local-fast")

    assert first is reranker
    assert second is reranker
    assert calls == ["local-fast"]


def test_warmup_skips_llm_validation(monkeypatch):
    async def fake_embedder_warmup(app):
        return {"status": "ok", "model": "embedder"}

    async def fake_reranker_warmup(app, choice):
        return {"status": "ok", "model": choice}

    async def fail_if_llm_validation_runs():
        raise AssertionError("Smart LLM validation should be disconnected")

    monkeypatch.setattr(api_main, "_warmup_active_embedder", fake_embedder_warmup)
    monkeypatch.setattr(api_main, "_warmup_reranker", fake_reranker_warmup)
    monkeypatch.setattr(api_main, "_validate_llm_model", fail_if_llm_validation_runs, raising=False)
    monkeypatch.setattr(settings, "reranker_model", "local-fast", raising=False)

    response = TestClient(api_main.app).post("/admin/model-settings/warmup")

    assert response.status_code == 200
    assert set(response.json()["warmups"]) == {"embedder", "reranker"}


def test_llm_validation_accepts_row_specific_provider_and_model(monkeypatch):
    captured = {}

    async def fake_validate(provider=None, model=None, thinking_level=None):
        captured.update(
            provider=provider,
            model=model,
            thinking_level=thinking_level,
        )
        return {
            "status": "ok",
            "provider": provider,
            "model": model,
            "thinking_level": thinking_level,
            "latency_ms": 1.2,
        }

    monkeypatch.setattr(api_main, "_validate_llm_model", fake_validate)

    response = TestClient(api_main.app).post(
        "/admin/model-settings/validate-llm",
        json={
            "provider": "gemini",
            "model": "gemini-3.1-pro",
            "thinking_level": "low",
        },
    )

    assert response.status_code == 200
    assert captured == {
        "provider": "gemini",
        "model": "gemini-3.1-pro",
        "thinking_level": "low",
    }
    assert response.json()["status"] == "ok"


def test_gemini_validation_uses_enough_tokens_for_newer_flash_models(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "candidates": [
                    {"content": {"parts": [{"text": "OK"}]}}
                ]
            }

    def fake_post(url, params, json, timeout):
        captured.update(url=url, params=params, json=json, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr(settings, "google_api_key", "configured-google-key", raising=False)
    monkeypatch.setattr(
        api_main,
        "_llm_models_for_provider",
        lambda provider: {"gemini-3.5-flash": {"supports_thinking": True}},
        raising=False,
    )

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)

    result = asyncio.run(
        api_main._validate_llm_model(
            provider="gemini",
            model="gemini-3.5-flash",
            thinking_level="low",
        )
    )

    assert result["status"] == "ok"
    assert captured["json"]["generationConfig"]["maxOutputTokens"] >= 32


def test_gemini_validation_does_not_send_thinking_level_to_25_models(monkeypatch):
    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "candidates": [
                    {"content": {"parts": [{"text": "OK"}]}}
                ]
            }

    def fake_post(url, params, json, timeout):
        captured.update(url=url, params=params, json=json, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr(settings, "google_api_key", "configured-google-key", raising=False)
    monkeypatch.setattr(api_main, "_available_gemini_llm_models", lambda: {}, raising=False)

    import httpx
    monkeypatch.setattr(httpx, "post", fake_post)

    result = asyncio.run(
        api_main._validate_llm_model(
            provider="gemini",
            model="gemini-2.5-flash-lite",
            thinking_level="low",
        )
    )

    assert result["status"] == "ok"
    assert "thinkingConfig" not in captured["json"]["generationConfig"]
