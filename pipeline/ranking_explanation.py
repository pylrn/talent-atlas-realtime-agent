"""Public API for the explanation layer.

Wraps explanation.py so api/main.py and other callers have a stable import.
No LLM calls — all explanations are deterministic from ranking signals.
"""

from __future__ import annotations

from typing import Any

from pipeline.explanation import build_explanation, _tier
from pipeline.search_result import SearchResult
from pipeline.spec import CanonicalSearchSpec


def build_ranking_explanation(
    result: SearchResult,
    rank_position: int,
    spec: CanonicalSearchSpec | None = None,
) -> dict[str, Any]:
    """Full structured explanation for a ranked candidate."""
    if spec is None:
        return _legacy_explanation(result, rank_position)
    return build_explanation(result, spec, rank_position)


def build_filter_only_ranking_explanation(
    result: SearchResult,
    rank_position: int,
) -> dict[str, Any]:
    """Minimal explanation for filter-only results (no semantic score)."""
    return {
        "match_tier":    "Filter match",
        "match_score":   0,
        "summary_line":  "Candidate passed all structured filters",
        "rank_position": rank_position,
        "checks": {
            "required":  [{"label": "Passed structured filters", "matched": True}],
            "preferred": [],
        },
        "score_breakdown":     [],
        "best_evidence":       result.best_chunk,
        "supporting_evidence": [sc.get("content", "") for sc in result.supporting_chunks[:2]],
        "retrieval_paths":     result.retrieval_paths,
    }


# ── Legacy shim for old callers that pass the old SearchResult ────────────────

def _legacy_explanation(result: Any, rank_position: int) -> dict[str, Any]:
    """Backwards-compatible explanation — works with both old and new SearchResult."""
    feature_score = float(getattr(result, "feature_score", 0) or
                          getattr(result, "rank_score", 0) or 0)
    # feature_score ≤ 1.0 means old 0-1 scale; > 1 means new 0-100 scale
    score_100 = feature_score if feature_score > 1.0 else feature_score * 100
    score_01  = score_100 / 100

    rerank_score = getattr(result, "rerank_score", None)
    has_rerank   = rerank_score is not None

    if has_rerank:
        raw = float(rerank_score)
        if raw > 1.0:
            confidence = 95
            formula    = f"raw score {raw:.2f} clamped to 95"
        else:
            confidence = round(raw * 100)
            formula    = "cross-encoder score × 100"
        sort_basis  = "rerank_score desc"
        score_basis = "rerank_score"
        final_score = raw
        retrieval_score = score_01
    else:
        confidence  = round(score_100)
        sort_basis  = "rank_score desc"
        score_basis = "rank_score"
        final_score = score_01
        retrieval_score = score_01
        formula = ""

    if confidence <= 40:
        confidence_label = "low"
    elif confidence <= 70:
        confidence_label = "medium"
    else:
        confidence_label = "high"

    signals   = list(getattr(result, "ranking_signals", []) or [])
    breakdown = []
    for sig in signals:
        breakdown.append({
            "name":                sig.get("name", ""),
            "score":               round(float(sig.get("value", 0)) * 100),
            "weight_percent":      round(float(sig.get("weight", 0)) * 100),
            "weight_pct":          round(float(sig.get("weight", 0)) * 100),
            "contribution_percent": round(float(sig.get("contribution", 0)) * 100),
        })

    reranking_entry: dict[str, Any] = {
        "name":                "Reranking",
        "applied":             has_rerank,
        "weight_percent":      100 if has_rerank else 0,
        "contribution_percent": confidence if has_rerank else 0,
        "formula":             formula,
    }
    if has_rerank:
        breakdown = [reranking_entry] + breakdown
    else:
        breakdown.append(reranking_entry)

    return {
        "match_tier":        _tier(score_100),
        "match_score":       round(score_100),
        "pipeline_confidence": confidence,
        "confidence_label":  confidence_label,
        "rank_score":        score_01,
        "final_score":       final_score,
        "retrieval_score":   retrieval_score,
        "sort_basis":        sort_basis,
        "score_basis":       score_basis,
        "summary_line":      f"Ranked #{rank_position}",
        "rank_position":     rank_position,
        "checks":            {"required": [], "preferred": []},
        "signals":           signals,
        "score_breakdown":   breakdown,
        "best_evidence":     getattr(result, "best_chunk", ""),
        "supporting_evidence": [
            sc.get("content", "") for sc in getattr(result, "supporting_chunks", [])[:2]
        ],
        "retrieval_paths":   getattr(result, "retrieval_paths", []),
    }
