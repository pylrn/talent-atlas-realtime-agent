from pipeline import settings
from pipeline.ai_insights import AIInsightService
from pipeline.query_planner import LLMQueryPlanner


def test_llm_catalog_includes_current_models():
    from pipeline.llm_models import (
        DEFAULT_LLM_MODELS,
        LLM_MODEL_CATALOG,
        default_llm_model_for_provider,
    )

    # Groq: llama-4-scout is now the default (benchmarked winner)
    assert DEFAULT_LLM_MODELS["groq"] == "meta-llama/llama-4-scout-17b-16e-instruct"
    assert default_llm_model_for_provider("groq") == "meta-llama/llama-4-scout-17b-16e-instruct"
    assert "meta-llama/llama-4-scout-17b-16e-instruct" in LLM_MODEL_CATALOG["groq"]
    assert "llama-3.3-70b-versatile" in LLM_MODEL_CATALOG["groq"]

    # Gemini: real IDs verified against API
    assert "gemini-2.5-flash-lite" in LLM_MODEL_CATALOG["gemini"]
    assert "gemini-3.5-flash" in LLM_MODEL_CATALOG["gemini"]
    # Removed models (404 or shutting down)
    assert "gemini-3.1-pro" not in LLM_MODEL_CATALOG["gemini"]
    assert "gemini-2.0-flash-lite" not in LLM_MODEL_CATALOG["gemini"]
    assert "gemini-3-flash" not in LLM_MODEL_CATALOG["gemini"]

    # OpenAI catalog
    assert "gpt-4.1-nano" in LLM_MODEL_CATALOG["openai"]
    assert "gpt-4.1-mini" in LLM_MODEL_CATALOG["openai"]


def test_llm_callers_share_the_gemini_default_model(monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "gemini", raising=False)
    monkeypatch.setattr(settings, "llm_model", "", raising=False)

    assert AIInsightService(provider="gemini").model == "gemini-2.5-flash-lite"
    assert LLMQueryPlanner(provider="gemini").model == "gemini-2.5-flash-lite"


def test_gemini_thinking_level_support_excludes_25_budget_models():
    from pipeline.llm_models import gemini_model_supports_thinking

    assert gemini_model_supports_thinking("gemini-3.5-flash") is True
    assert gemini_model_supports_thinking("gemini-2.5-flash") is False
    assert gemini_model_supports_thinking("gemini-2.5-flash-lite") is False
