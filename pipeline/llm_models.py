"""Shared catalog for text-generation LLM model choices."""

from __future__ import annotations

from typing import Any

DEFAULT_LLM_MODELS = {
    "openai":   "gpt-4.1-nano",
    "gemini":   "gemini-2.5-flash-lite",
    "groq":     "meta-llama/llama-4-scout-17b-16e-instruct",
    "deepseek": "deepseek-chat",
}

LLM_MODEL_CATALOG = {
    "groq": {
        "meta-llama/llama-4-scout-17b-16e-instruct": {
            "description": "★ Best overall — ~988ms avg, perfect score on all query types.",
        },
        "llama-3.3-70b-versatile": {
            "description": "Slightly faster avg (~878ms), near-perfect score. Good fallback.",
        },
        "llama-3.1-8b-instant": {
            "description": "Tiny and fast on simple queries, but slow on complex ones (thinking mode).",
        },
        "qwen/qwen3-32b": {
            "description": "High accuracy but enables thinking on hard queries — can hit 20s+.",
        },
    },
    "gemini": {
        "gemini-3.6-flash": {
            "description": "Current Flash model used by the typed copilot.",
            "supports_thinking": True,
        },
        "gemini-3.5-flash": {
            "description": "Newer Flash generation — ~2s avg, strong reasoning, tuned for agentic/coding.",
            "supports_thinking": True,
        },
        "gemini-flash-latest": {
            "description": "Alias for the latest Flash model.",
            "supports_thinking": False,
        },
        "gemini-2.5-flash-lite": {
            "description": "Fastest Gemini — ~1.3s avg, solid JSON output. Good for fast mode.",
            "supports_thinking": False,
        },
        "gemini-flash-lite-latest": {
            "description": "Alias for the latest Flash-Lite. Mirrors gemini-2.5-flash-lite.",
            "supports_thinking": False,
        },
    },
    "deepseek": {
        "deepseek-chat": {
            "description": "DeepSeek-V3 chat — strong JSON output, OpenAI-compatible. Default planner model.",
        },
        "deepseek-reasoner": {
            "description": "DeepSeek-R1 reasoning — higher accuracy on ambiguous JDs, slower.",
        },
    },
    "openai": {
        "gpt-4.1-nano": {
            "description": "Fastest and cheapest OpenAI model — great for query planning.",
        },
        "gpt-4.1-mini": {
            "description": "OpenAI 4.1 mini — stronger reasoning than nano.",
        },
        "gpt-4.1": {
            "description": "Full GPT-4.1 — high accuracy for complex JDs.",
        },
        "gpt-5.4-nano": {
            "description": "GPT-5.4 nano — newest low-latency OpenAI option.",
        },
        "gpt-5.4-mini": {
            "description": "GPT-5.4 mini — latest compact GPT-5 series model.",
        },
        "gpt-4o-mini": {
            "description": "GPT-4o mini — older but battle-tested for structured JSON output.",
        },
    },
}


def default_llm_model_for_provider(provider: str) -> str:
    """Return the repo default text-generation model for an LLM provider."""
    return DEFAULT_LLM_MODELS.get(provider, "")


def gemini_model_supports_thinking(model: str) -> bool:
    """Best-effort static check for Gemini thinking_level support."""
    blocked = ("audio", "tts", "image", "robotics", "computer-use")
    return (
        model.startswith("gemini-3")
        and not any(part in model for part in blocked)
    )


def list_llm_models() -> dict[str, dict[str, dict[str, Any]]]:
    """Return a copy of the supported text-generation LLM model catalog."""
    return {
        provider: {model: dict(info) for model, info in models.items()}
        for provider, models in LLM_MODEL_CATALOG.items()
    }
