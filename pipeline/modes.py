"""Named search-mode presets.

Usage:
    from pipeline.modes import get_config
    cfg = get_config("no-llm")
    cfg = get_config("quality")
    cfg = get_config("fast", overrides={"final_top_k": 10})
"""

from __future__ import annotations

from typing import Any

from pipeline.config import SEARCH_CONFIG

# ── Preset definitions (only override what differs from SEARCH_CONFIG) ────────

FAST_MODE: dict[str, Any] = {
    "use_cross_encoder":  False,
    "use_mmr":            True,
    "dense_top_k":        80,
    "bm25_top_k":         60,
    "skill_top_k":         40,
    "cross_encoder_top_k": 0,
    "final_top_k":         7,
}

NO_LLM_MODE: dict[str, Any] = {
    "use_llm_planner":     False,
    "use_planner_fallback": True,
    "use_fallback_repair": False,
    "use_cross_encoder":   False,
    "use_mmr":             True,
    "dense_top_k":         80,
    "bm25_top_k":          60,
    "skill_top_k":          40,
    "cross_encoder_top_k":   0,
    "final_top_k":          7,
}

AGENT_QUALITY_MODE: dict[str, Any] = {
    # The agent has already planned the query into structured must/should fields.
    # Keep backend LLM planning off, but spend more compute on final ranking.
    "use_llm_planner":      False,
    "use_planner_fallback": True,
    "use_fallback_repair":  False,
    "use_cross_encoder":    True,
    "use_mmr":              True,
    "dense_top_k":          80,
    "bm25_top_k":           50,
    "skill_top_k":           40,
    "cross_encoder_top_k":   7,
    "final_top_k":           7,
    "keyword_policy":       "auto",
    "keyword_timeout_ms":    800,
}

QUALITY_MODE: dict[str, Any] = {
    "use_cross_encoder":  True,
    "use_mmr":            True,
    "dense_top_k":        120,
    "bm25_top_k":         100,
    "skill_top_k":        60,
    "cross_encoder_top_k": 10,
    "final_top_k":         7,
}

_PRESETS: dict[str, dict[str, Any]] = {
    "no-llm":        NO_LLM_MODE,
    "fast":          FAST_MODE,
    "quality":       QUALITY_MODE,
    "agent-quality": AGENT_QUALITY_MODE,
}


def _resolve_llm_for_mode(mode: str) -> tuple[str, str, str]:
    """Read per-mode LLM provider/model/thinking_level from settings."""
    from pipeline import settings as _s  # lazy import to avoid circulars at module load

    global_provider = _s.llm_provider
    global_model    = _s.llm_model
    if mode == "fast":
        return (
            _s.fast_llm_provider or global_provider,
            _s.fast_llm_model    or global_model,
            _s.fast_thinking_level,
        )
    if mode == "quality":
        return (
            _s.quality_llm_provider or global_provider,
            _s.quality_llm_model    or global_model,
            _s.quality_thinking_level,
        )
    return (global_provider, global_model, "medium")


def get_config(
    mode: str = "quality",
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a merged config dict for the given mode.

    Merges: SEARCH_CONFIG (base) → preset → per-mode LLM from settings → caller overrides.
    """
    preset = _PRESETS.get(mode, QUALITY_MODE)
    cfg = {**SEARCH_CONFIG, **preset}
    provider, model, thinking_level = _resolve_llm_for_mode(mode)
    cfg["llm_provider"]   = provider
    cfg["llm_model"]      = model
    cfg["thinking_level"] = thinking_level
    
    from pipeline import settings as _s
    cfg["use_personalization"] = getattr(_s, "use_personalization", False)
    cfg["bm25_backend"] = getattr(_s, "bm25_backend", "fts")
    cfg["bm25_overfetch_factor"] = getattr(_s, "bm25_overfetch_factor", 4)
    cfg["bm25_overfetch_min"] = getattr(_s, "bm25_overfetch_min", 60)
    cfg["use_retrieval_cache"] = bool(getattr(_s, "search_use_retrieval_cache", False))
    
    if overrides:
        cfg.update(overrides)
    return cfg
