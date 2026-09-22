"""Feature ranker — weighted final score combining all retrieval signals.

Two weight sets:
  WITH cross-encoder:    CE dominates (0.45), retrieval supports
  WITHOUT cross-encoder: retrieval + skills drive the score

Output: result.feature_score in [0, 100], result.ranking_signals filled.
"""

from __future__ import annotations

import math
from typing import Any

from pipeline.search_result import SearchResult
from pipeline.spec import CanonicalSearchSpec
from pipeline.text_match import norm_skill, phrase_in_text

# ── Weight sets ───────────────────────────────────────────────────────────────
_WEIGHTS_WITH_CE: dict[str, float] = {
    "cross_encoder":      0.45,
    "retrieval":          0.20,
    "skill_match":        0.12,
    "should_match":       0.10,
    "experience_fit":     0.05,
    "skill_recency":      0.03,
    "completeness":       0.02,
    "personalization":    0.03,
}

_WEIGHTS_WITHOUT_CE: dict[str, float] = {
    "cross_encoder":      0.00,
    "retrieval":          0.40,
    "skill_match":        0.20,
    "should_match":       0.10,
    "experience_fit":     0.15,
    "skill_recency":      0.05,
    "completeness":       0.05,
    "personalization":    0.05,
}


def score_results(
    results: list[SearchResult],
    spec: CanonicalSearchSpec,
    use_cross_encoder: bool = True,
    recruiter_prefs: dict[str, Any] | None = None,
    recruiter_profile: Any | None = None,
) -> list[SearchResult]:
    """Compute feature_score and ranking_signals for each result in place.

    `recruiter_prefs` is the row from `recruiter_preferences` (or None for
    defaults). Recognised keys:
      - prioritize_recent_experience (bool) — boosts skill_recency by 1.5×
      - prioritize_exact_skill_match (bool) — boosts skill_match by 1.5×
      - weight_overrides_json (dict)        — raw per-signal weight overrides
    Weights are re-normalised after any adjustment so they sum to 1.0.
    """
    base = _WEIGHTS_WITH_CE if use_cross_encoder else _WEIGHTS_WITHOUT_CE
    weights = _apply_recruiter_prefs(base, recruiter_prefs)

    all_skills = set(spec.must.skills + spec.should.skills)
    must_count  = max(len(spec.must.skills), 1)
    # should.locations is populated when relax.py drops a city/country —
    # it lets ranking still favour candidates who match the original
    # constraint instead of treating it as if it never existed.
    should_count = max(
        len(spec.should.skills + spec.should.themes + spec.should.locations),
        1,
    )

    for r in results:
        signals = _compute_signals(r, spec, all_skills, must_count, should_count, recruiter_profile)
        result_weights = _applicable_weights(weights, spec, r)
        score   = sum(signals[k] * result_weights[k] for k in result_weights)
        # Normalise to 0-100
        r.feature_score   = round(min(100.0, max(0.0, score * 100)), 2)
        r.rank_score      = r.feature_score
        r.similarity_score = 1.0 - r.best_chunk_distance
        r.ranking_signals  = _build_signal_list(signals, result_weights)
        if r.feature_score > 0:
            r.sort_basis = "feature_score"

    results.sort(key=lambda r: (
        -float(r.feature_score or 0.0),
        str(r.candidate_id or ""),
    ))
    return results


# ── Signal computation ────────────────────────────────────────────────────────

def _compute_signals(
    r: SearchResult,
    spec: CanonicalSearchSpec,
    all_skills: set[str],
    must_count: int,
    should_count: int,
    recruiter_profile: Any | None = None,
) -> dict[str, float]:
    ce = r.rerank_score
    if ce is not None:
        # Normalise cross-encoder scores (can be negative raw logits)
        ce_norm = 1.0 / (1.0 + math.exp(-float(ce))) if ce < 0 or ce > 1 else float(ce)
    else:
        ce_norm = 0.0

    retrieval = min(1.0, r.fused_rrf_score / 0.035) if r.fused_rrf_score else 0.0

    cand_skills = {norm_skill(s) for s in r.skills}
    must_matched   = len({norm_skill(s) for s in spec.must.skills} & cand_skills)
    should_matched = len({norm_skill(s) for s in spec.should.skills} & cand_skills)
    theme_matched  = _theme_match(r, spec.should.themes)
    location_matched = _location_match(r, spec.should.locations)

    skill_match  = must_matched   / must_count
    should_score = (should_matched + theme_matched * 0.5 + location_matched) / should_count

    exp_fit      = _experience_fit(r.years_exp, spec.must.min_years_exp, spec.must.max_years_exp)
    recency      = _skill_recency_score(r.matched_skill_recency_days)
    completeness = _completeness(r)
    
    from pipeline.personalization import personalization_score
    personalization = personalization_score(r, recruiter_profile) if recruiter_profile else 0.5

    return {
        "cross_encoder":  ce_norm,
        "retrieval":      retrieval,
        "skill_match":    skill_match,
        "should_match":   should_score,
        "experience_fit": exp_fit,
        "skill_recency":  recency,
        "completeness":   completeness,
        "personalization": personalization,
        # Store raw counts for explanation
        "_must_matched":   must_matched,
        "_must_total":     len(spec.must.skills),
        "_should_matched": should_matched,
        "_should_total":   len(spec.should.skills),
    }


def _experience_fit(years: int, min_exp: int | None, max_exp: int | None) -> float:
    if min_exp is None and max_exp is None:
        return 0.7  # no constraint — neutral
    if min_exp is not None and years < min_exp:
        gap = min_exp - years
        return max(0.0, 1.0 - gap * 0.2)
    if max_exp is not None and years > max_exp:
        gap = years - max_exp
        return max(0.0, 1.0 - gap * 0.2)
    return 1.0


def _skill_recency_score(days_since_last_used: float | None) -> float:
    """Convert recency into a bounded 0-1 score.

    Missing data stays neutral at 0.7. Recent usage decays gradually over the
    first year and bottoms out around two years.
    """
    if days_since_last_used is None:
        return 0.7
    days = max(0.0, float(days_since_last_used))
    return max(0.0, 1.0 - ((days / 365.0) * 0.5))


def _completeness(r: SearchResult) -> float:
    filled = sum([
        bool(r.full_name),
        bool(r.email),
        bool(r.city),
        bool(r.country),
        bool(r.skills),
        bool(r.years_exp),
        bool(r.best_chunk),
    ])
    return filled / 7.0


def _location_match(r: SearchResult, locations: list[str]) -> int:
    """Number of soft-preferred locations the candidate matches.

    `locations` comes from spec.should.locations — populated when relax.py
    drops a city/country hard filter. Matching either field counts once.
    Comparison treats spaces and hyphens as equivalent so the LLM-normalised
    "new-york" still matches a DB value of "New York".
    """
    if not locations:
        return 0

    def _norm(s: str | None) -> str:
        if not s:
            return ""
        return s.strip().lower().replace("-", " ").replace("_", " ")

    cand_values = {_norm(r.city), _norm(r.country)}
    cand_values.discard("")
    if not cand_values:
        return 0
    targets = {_norm(loc) for loc in locations if loc}
    return sum(1 for v in cand_values if v in targets)


def _theme_match(r: SearchResult, themes: list[str]) -> int:
    if not themes:
        return 0
    all_text = (r.best_chunk + " " + " ".join(
        sc.get("content", "") for sc in r.supporting_chunks
    )).lower()
    # Word-boundary match so "cto" doesn't match inside "Victor".
    return sum(1 for t in themes if phrase_in_text(norm_skill(t), all_text))


def _apply_recruiter_prefs(
    base: dict[str, float],
    prefs: dict[str, Any] | None,
) -> dict[str, float]:
    """Apply recruiter-preference adjustments and renormalise weights."""
    if not prefs:
        return base
    w = dict(base)
    if prefs.get("prioritize_recent_experience"):
        w["skill_recency"] = w.get("skill_recency", 0.0) * 1.5
    if prefs.get("prioritize_exact_skill_match"):
        w["skill_match"]   = w.get("skill_match",   0.0) * 1.5
    overrides = prefs.get("weight_overrides_json") or {}
    if isinstance(overrides, dict):
        for k, v in overrides.items():
            if k in w:
                try:
                    w[k] = max(0.0, float(v))
                except (TypeError, ValueError):
                    continue
    total = sum(w.values()) or 1.0
    return {k: v / total for k, v in w.items()}


def _applicable_weights(
    weights: dict[str, float],
    spec: CanonicalSearchSpec,
    result: SearchResult,
) -> dict[str, float]:
    """Remove unavailable signals and renormalize the remaining evidence.

    An absent preference category is not a failed preference. Likewise, the
    candidates outside a bounded cross-encoder window were never reviewed by
    that model and must not receive a zero text-quality score.
    """
    active = {name: weight for name, weight in weights.items() if weight > 0}
    if not (spec.should.skills or spec.should.themes or spec.should.locations):
        active.pop("should_match", None)
    if result.rerank_score is None:
        active.pop("cross_encoder", None)
    total = sum(active.values()) or 1.0
    return {name: weight / total for name, weight in active.items()}


def _build_signal_list(signals: dict[str, float], weights: dict[str, float]) -> list[dict]:
    out = []
    for name, w in weights.items():
        v = signals.get(name, 0.0)
        out.append({
            "name":         name,
            "value":        round(v, 4),
            "weight":       round(w, 4),
            "contribution": round(v * w, 4),
        })
    return out
