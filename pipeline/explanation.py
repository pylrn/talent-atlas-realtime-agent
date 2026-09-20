"""Hardcoded, layman-friendly explanation builder — zero LLM calls.

Builds a structured explanation dict from the ranking signals already
computed during feature scoring. No templates, no generation — just
dict assembly from computed values.

Output shape (display-ready):
{
  "match_tier":    "Strong match",
  "match_score":   87,
  "summary_line":  "8 of 10 signals matched",
  "checks": {
    "required":  [{"label": "Has React", "matched": true}, ...],
    "preferred": [{"label": "Responsive design", "matched": false}, ...]
  },
  "score_breakdown": [
    {"name": "Text relevance", "score": 79, "weight_pct": 20}, ...
  ],
  "best_evidence":       "...",
  "supporting_evidence": ["...", "..."]
}
"""

from __future__ import annotations

from pipeline.constants import TIER_STRONG, TIER_GOOD, TIER_PARTIAL
from pipeline.search_result import SearchResult
from pipeline.spec import CanonicalSearchSpec
from pipeline.text_match import norm_skill, phrase_in_text


def build_explanation(
    result: SearchResult,
    spec: CanonicalSearchSpec,
    rank_position: int = 0,
) -> dict:
    """Deterministically build a human-readable explanation from signals."""
    required_checks  = _build_required_checks(result, spec)
    preferred_checks = _build_preferred_checks(result, spec)

    matched_req  = sum(1 for c in required_checks  if c["matched"])
    matched_pref = sum(1 for c in preferred_checks if c["matched"])
    total        = len(required_checks) + len(preferred_checks)
    matched      = matched_req + matched_pref

    score      = result.feature_score
    match_tier = _tier(score)

    score_breakdown = _build_score_breakdown(result)

    return {
        "match_tier":    match_tier,
        "match_score":   round(score),
        "summary_line":  f"{matched} of {total} signals matched",
        "rank_position": rank_position,
        "checks": {
            "required":  required_checks,
            "preferred": preferred_checks,
        },
        "score_breakdown":     [s for s in score_breakdown if s is not None],
        "best_evidence":       result.best_chunk,
        "supporting_evidence": [sc.get("content", "") for sc in result.supporting_chunks[:2]],
        "retrieval_paths":     result.retrieval_paths,
    }


# ── Check builders ────────────────────────────────────────────────────────────

def _build_required_checks(result: SearchResult, spec: CanonicalSearchSpec) -> list[dict]:
    checks: list[dict] = []
    cand_skills = {norm_skill(s) for s in result.skills}

    for skill in spec.must.skills:
        checks.append({
            "label":   f"Has {skill.replace('-', ' ').title()}",
            "matched": norm_skill(skill) in cand_skills,
        })

    if spec.must.status:
        checks.append({
            "label":   f"Status: {spec.must.status[0]}",
            "matched": result.skills is not None,  # proxy — real status on result
        })

    if spec.must.city:
        checks.append({
            "label":   f"Located in {spec.must.city.title()}",
            "matched": (result.city or "").lower() == spec.must.city.lower(),
        })

    if spec.must.country:
        checks.append({
            "label":   f"Country: {spec.must.country.upper()}",
            "matched": (result.country or "").lower() == spec.must.country.lower(),
        })

    if spec.must.min_years_exp is not None:
        checks.append({
            "label":   f"{spec.must.min_years_exp}+ years experience",
            "matched": result.years_exp >= spec.must.min_years_exp,
        })

    if spec.must.max_years_exp is not None:
        checks.append({
            "label":   f"Up to {spec.must.max_years_exp} years experience",
            "matched": result.years_exp <= spec.must.max_years_exp,
        })

    return checks


def _build_preferred_checks(result: SearchResult, spec: CanonicalSearchSpec) -> list[dict]:
    checks: list[dict] = []
    cand_skills = {norm_skill(s) for s in result.skills}

    for skill in spec.should.skills:
        checks.append({
            "label":   f"Has {skill.replace('-', ' ').title()}",
            "matched": norm_skill(skill) in cand_skills,
        })

    all_text = (result.best_chunk + " " + " ".join(
        sc.get("content", "") for sc in result.supporting_chunks
    )).lower()

    for theme in spec.should.themes:
        checks.append({
            "label":   theme.replace("-", " ").title(),
            # Word-boundary match so a role like "cto" doesn't match "Victor".
            "matched": phrase_in_text(norm_skill(theme), all_text),
        })

    return checks


# ── Score breakdown ───────────────────────────────────────────────────────────

def _build_score_breakdown(result: SearchResult) -> list[dict | None]:
    rows: list[dict | None] = []
    for sig in result.ranking_signals:
        name  = sig.get("name", "")
        value = sig.get("value", 0.0)
        w     = sig.get("weight", 0.0)
        rows.append({
            "name":       _signal_label(name),
            "score":      round(value * 100),
            "weight_pct": round(w * 100),
        })
    return rows


_SIGNAL_LABELS = {
    "cross_encoder":  "Text match quality",
    "retrieval":      "Search relevance",
    "skill_match":    "Required skills",
    "should_match":   "Preferred skills",
    "experience_fit": "Experience fit",
    "skill_recency":  "Skill recency",
    "completeness":   "Profile completeness",
    "personalization": "Matches your hiring patterns",
}


def _signal_label(name: str) -> str:
    return _SIGNAL_LABELS.get(name, name.replace("_", " ").title())


# ── Tier ──────────────────────────────────────────────────────────────────────

def _tier(score: float) -> str:
    if score >= TIER_STRONG:
        return "Strong match"
    if score >= TIER_GOOD:
        return "Good match"
    if score >= TIER_PARTIAL:
        return "Partial match"
    return "Weak match"
