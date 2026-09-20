"""Implicit recruiter personalization from outcome history.

Queries search_outcomes joined with candidate profiles to build a
preference vector per recruiter, then scores current results against it.
Zero LLM calls — pure SQL aggregation + Python scoring.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from dataclasses import dataclass
from typing import Any

from pipeline import cache as _cache
from pipeline.search_result import SearchResult

logger = logging.getLogger(__name__)

# Outcome weights map actions to affinity impacts.
OUTCOME_WEIGHTS = {
    "shortlisted": 1.0,
    "contacted": 0.8,
    "saved": 0.6,
    "viewed": 0.2,
    "archived": -0.3,
    "rejected": -0.5,
    "flagged_hallucination": -0.8,
}


@dataclass
class RecruiterProfile:
    skill_affinities: dict[str, float]
    location_affinities: dict[str, float]
    preferred_exp_range: tuple[float, float]
    total_outcomes: int


async def load_recruiter_profile(
    pool: Any,
    recruiter_id: str,
    lookback_days: int = 90,
    use_cache: bool = False,
) -> RecruiterProfile | None:
    """Fetch and aggregate the recruiter's outcome history into a profile."""
    if not recruiter_id:
        return None
    cache_key = _cache.recruiter_profile_key(recruiter_id, lookback_days)
    if use_cache:
        cached = await _cache.get(cache_key)
        if cached is not None:
            return RecruiterProfile(
                skill_affinities=dict(cached.get("skill_affinities") or {}),
                location_affinities=dict(cached.get("location_affinities") or {}),
                preferred_exp_range=tuple(cached.get("preferred_exp_range") or (0.0, 100.0)),
                total_outcomes=int(cached.get("total_outcomes") or 0),
            )

    try:
        # Note: We group by impression to avoid double-counting if a recruiter
        # performs multiple actions on the same candidate impression (e.g. view then save).
        # We take the max weight action per impression.
        rows = await pool.fetch(
            f"""
            WITH ranked_actions AS (
                SELECT
                    i.id AS impression_id,
                    o.action,
                    c.skills,
                    c.city,
                    c.country,
                    c.years_exp,
                    ROW_NUMBER() OVER (
                        PARTITION BY i.id
                        ORDER BY
                            CASE o.action
                                WHEN 'shortlisted' THEN 1
                                WHEN 'contacted' THEN 2
                                WHEN 'saved' THEN 3
                                WHEN 'viewed' THEN 4
                                WHEN 'archived' THEN 5
                                WHEN 'rejected' THEN 6
                                WHEN 'flagged_hallucination' THEN 7
                                ELSE 8
                            END
                    ) as rn
                FROM search_outcomes o
                JOIN search_impressions i ON o.impression_id = i.id
                JOIN candidates c ON i.candidate_id = c.id
                WHERE i.recruiter_id = $1::uuid
                  AND o.occurred_at >= NOW() - INTERVAL '{lookback_days} days'
            )
            SELECT * FROM ranked_actions WHERE rn = 1
            """,
            recruiter_id,
        )
    except Exception as exc:
        logger.debug("Failed to load recruiter profile for %s: %s", recruiter_id, exc)
        return None

    if not rows:
        return None

    total_outcomes = len(rows)
    skill_affinities: dict[str, float] = {}
    location_affinities: dict[str, float] = {}
    exp_values: list[float] = []

    for row in rows:
        action = row["action"]
        weight = OUTCOME_WEIGHTS.get(action, 0.0)
        
        # Track skills
        skills = row["skills"] or []
        for s in skills:
            s_lower = s.lower()
            skill_affinities[s_lower] = skill_affinities.get(s_lower, 0.0) + weight
            
        # Track locations
        city = (row["city"] or "").lower().strip()
        country = (row["country"] or "").lower().strip()
        if city and country:
            loc_key = f"{city}|{country}"
            location_affinities[loc_key] = location_affinities.get(loc_key, 0.0) + weight
        elif country:
            loc_key = country
            location_affinities[loc_key] = location_affinities.get(loc_key, 0.0) + weight
            
        # Track experience (only for positive signals to avoid skewing)
        if weight > 0 and row["years_exp"] is not None:
            exp_values.append(float(row["years_exp"]))

    # Normalize affinities to [-1, 1] range to avoid runaway scores
    def normalize_dict(d: dict[str, float]) -> dict[str, float]:
        if not d:
            return d
        max_val = max((abs(v) for v in d.values()), default=1.0)
        if max_val == 0:
            return d
        return {k: v / max_val for k, v in d.items()}

    skill_affinities = normalize_dict(skill_affinities)
    location_affinities = normalize_dict(location_affinities)

    # Calculate preferred experience range
    if exp_values:
        avg_exp = sum(exp_values) / len(exp_values)
        # Give a +/- 3 year buffer around the average
        exp_range = (max(0.0, avg_exp - 3.0), avg_exp + 3.0)
    else:
        exp_range = (0.0, 100.0)

    profile = RecruiterProfile(
        skill_affinities=skill_affinities,
        location_affinities=location_affinities,
        preferred_exp_range=exp_range,
        total_outcomes=total_outcomes,
    )
    if use_cache:
        await _cache.set(cache_key, asdict(profile))
    return profile


def personalization_score(
    candidate: SearchResult,
    profile: RecruiterProfile | None,
) -> float:
    """Score a candidate against the recruiter's profile [0.0, 1.0]."""
    if not profile or profile.total_outcomes < 5:
        # Cold start: neutral score
        return 0.5

    # 1. Skill affinity (-1.0 to 1.0)
    skill_score = 0.0
    cand_skills = [s.lower() for s in candidate.skills]
    if cand_skills:
        scores = [profile.skill_affinities.get(s, 0.0) for s in cand_skills]
        # Average the scores of the candidate's skills
        skill_score = sum(scores) / len(scores)

    # 2. Location affinity (0.0 to 1.0)
    loc_score = 0.0
    city = (candidate.city or "").lower().strip()
    country = (candidate.country or "").lower().strip()
    loc_key = f"{city}|{country}" if city and country else country
    if loc_key and loc_key in profile.location_affinities:
        # Map affinity [-1, 1] to [0, 1]
        loc_score = (profile.location_affinities[loc_key] + 1.0) / 2.0
    else:
        # Neutral if unknown location
        loc_score = 0.5

    # 3. Experience fit (0.0 to 1.0)
    exp_score = 0.0
    exp = float(candidate.years_exp)
    min_exp, max_exp = profile.preferred_exp_range
    if min_exp <= exp <= max_exp:
        exp_score = 1.0
    else:
        # Decay as it moves away from the range
        dist = min(abs(exp - min_exp), abs(exp - max_exp))
        # 1 year off = 0.8, 5 years off = 0
        exp_score = max(0.0, 1.0 - (dist / 5.0))

    # Combine signals (skill is most important, then exp, then loc)
    # Map skill_score [-1, 1] to [0, 1]
    norm_skill = (skill_score + 1.0) / 2.0
    
    final_score = (norm_skill * 0.5) + (exp_score * 0.3) + (loc_score * 0.2)
    
    # Clip to [0, 1]
    return max(0.0, min(1.0, final_score))
