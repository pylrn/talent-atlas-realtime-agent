"""Deterministic behavioral profiler: search/outcome history → observations.

No LLM. Each detector returns candidate observations as plain dicts:
    {category, content, content_key, consistency, evidence_count, evidence}
profile_recruiter() aggregates, computes confidence, and upserts into
recruiter_memory — skipping any content_key the recruiter ever dismissed.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone

import asyncpg

from pipeline.observability import (
    start_span as _obs_start_span,
    update_current_span as _obs_update_current_span,
)

logger = logging.getLogger(__name__)


LOOKBACK_SEARCHES = 20
MIN_SEARCHES = 5
FILTER_RATIO = 0.6
MIN_POSITIVE_OUTCOMES = 6
SKILL_LIFT = 1.8
MAX_ACTIVE_OBSERVATIONS = 10
EXPIRE_AFTER_DAYS = 30
POSITIVE_ACTIONS = ("saved", "contacted", "shortlisted")

# Only these words count as "taste" query terms — keeps junk out.
TASTE_LEXICON = {
    "startup", "scaleup", "scale-up", "enterprise", "agency", "fintech",
    "intern", "internship", "junior", "senior", "staff", "lead", "principal",
    "remote", "hybrid", "onsite", "contract", "freelance",
    "passionate", "hands-on", "research", "open-source", "side-project",
}
_TERM_CATEGORY = {
    "startup": "company_stage", "scaleup": "company_stage",
    "scale-up": "company_stage", "enterprise": "company_stage",
    "agency": "company_stage", "fintech": "company_stage",
    "intern": "seniority", "internship": "seniority", "junior": "seniority",
    "senior": "seniority", "staff": "seniority", "lead": "seniority",
    "principal": "seniority",
    "remote": "work_style", "hybrid": "work_style", "onsite": "work_style",
    "contract": "work_style", "freelance": "work_style",
}


def observation_confidence(consistency: float, evidence_count: int) -> float:
    """Monotone in both inputs, capped at 0.95 so observations never look certain."""
    return round(min(0.95, consistency * evidence_count / (evidence_count + 3)), 3)


def detect_filter_repetition(search_rows: list[dict]) -> list[dict]:
    """Same city/country/min_years_exp filter in >=60% of recent searches."""
    rows = search_rows[:LOOKBACK_SEARCHES]
    if len(rows) < MIN_SEARCHES:
        return []
    counts: Counter[tuple[str, str]] = Counter()
    for r in rows:
        f = r.get("filters_json") or {}
        if isinstance(f, str):
            try:
                f = json.loads(f)
            except json.JSONDecodeError:
                f = {}
        for key in ("city", "country", "min_years_exp"):
            v = f.get(key)
            if v not in (None, "", 0):
                counts[(key, str(v).lower())] += 1
    out = []
    for (key, val), n in counts.items():
        ratio = n / len(rows)
        if ratio >= FILTER_RATIO:
            label = {"city": "location", "country": "location",
                     "min_years_exp": "seniority"}[key]
            content = (f"Usually filters to {key.replace('_', ' ')} = {val} "
                       f"({n} of last {len(rows)} searches)")
            out.append({"category": label, "content": content,
                        "content_key": f"filter:{key}={val}",
                        "consistency": ratio, "evidence_count": n,
                        "evidence": [{"type": "filter_repetition",
                                      "detail": f"{key}={val} in {n}/{len(rows)}"}]})
    return out


def detect_query_terms(search_rows: list[dict]) -> list[dict]:
    """Taste-lexicon words recurring across recent queries (>=40%, min 4)."""
    rows = search_rows[:LOOKBACK_SEARCHES]
    if len(rows) < MIN_SEARCHES:
        return []
    hits: Counter[str] = Counter()
    for r in rows:
        words = {w.strip(".,()").lower() for w in str(r.get("query") or "").split()}
        for t in words & TASTE_LEXICON:
            hits[t] += 1
    out = []
    for term, n in hits.items():
        ratio = n / len(rows)
        if n >= 4 and ratio >= 0.4:
            out.append({"category": _TERM_CATEGORY.get(term, "work_style"),
                        "content": (f"Often searches for '{term}' "
                                    f"({n} of last {len(rows)} queries)"),
                        "content_key": f"term:{term}",
                        "consistency": ratio, "evidence_count": n,
                        "evidence": [{"type": "query_term",
                                      "detail": f"'{term}' in {n}/{len(rows)}"}]})
    return out


def detect_outcome_skew(shown: list[dict], outcomes: list[dict]) -> list[dict]:
    """Accepted-vs-shown skew on skills and years_exp."""
    accepted_ids = {o["candidate_id"] for o in outcomes
                    if o.get("action") in POSITIVE_ACTIONS}
    if len([o for o in outcomes if o.get("action") in POSITIVE_ACTIONS]) \
            < MIN_POSITIVE_OUTCOMES:
        return []
    by_id = {s["candidate_id"]: s for s in shown}
    accepted = [by_id[i] for i in accepted_ids if i in by_id]
    if not accepted or not shown:
        return []
    out = []

    # Skill lift: skill share among accepted vs among all shown.
    shown_skill = Counter(s.lower() for c in shown for s in (c.get("skills") or []))
    acc_skill = Counter(s.lower() for c in accepted for s in (c.get("skills") or []))
    for skill, k in acc_skill.items():
        share_acc = k / len(accepted)
        share_shown = shown_skill[skill] / len(shown)
        if k >= 3 and share_acc >= 0.6 and share_shown > 0 \
                and share_acc / share_shown >= SKILL_LIFT:
            out.append({"category": "skill",
                        "content": (f"Tends to accept candidates with {skill} "
                                    f"({k} of {len(accepted)} accepted)"),
                        "content_key": f"skill:{skill}",
                        "consistency": share_acc, "evidence_count": k,
                        "evidence": [{"type": "outcome_skew",
                                      "detail": f"{skill}: {share_acc:.0%} accepted "
                                                f"vs {share_shown:.0%} shown"}]})

    # Experience skew.
    yrs = [c.get("years_exp") for c in shown if c.get("years_exp") is not None]
    yrs_acc = [c.get("years_exp") for c in accepted if c.get("years_exp") is not None]
    if len(yrs) >= 10 and len(yrs_acc) >= 4:
        avg_shown, avg_acc = sum(yrs) / len(yrs), sum(yrs_acc) / len(yrs_acc)
        if abs(avg_acc - avg_shown) >= 2.5:
            direction = "junior" if avg_acc < avg_shown else "senior"
            out.append({"category": "seniority",
                        "content": (f"Tends to accept more {direction} candidates "
                                    f"(avg {avg_acc:.1f}y accepted vs "
                                    f"{avg_shown:.1f}y shown)"),
                        "content_key": f"seniority:prefers_{direction}",
                        "consistency": min(1.0, abs(avg_acc - avg_shown) / 5),
                        "evidence_count": len(yrs_acc),
                        "evidence": [{"type": "outcome_skew",
                                      "detail": f"avg exp {avg_acc:.1f} vs {avg_shown:.1f}"}]})
    return out


async def profile_recruiter(pool: asyncpg.Pool, recruiter_id: str) -> int:
    """Run all detectors and upsert observations. Returns count written."""
    with _obs_start_span("agent.profile_recruiter", input={"recruiter_id": recruiter_id}):
        search_rows = [dict(r) for r in await pool.fetch(
            """SELECT query, filters_json FROM search_history
               WHERE recruiter_id = $1 ORDER BY timestamp DESC LIMIT $2""",
            recruiter_id, LOOKBACK_SEARCHES)]
        shown = [dict(r) for r in await pool.fetch(
            """SELECT DISTINCT ON (c.id) c.id::text AS candidate_id,
                      c.skills, c.years_exp
               FROM search_impressions si JOIN candidates c ON c.id = si.candidate_id
               WHERE si.recruiter_id::text = $1
                 AND si.shown_at > NOW() - INTERVAL '90 days'""",
            recruiter_id)]
        outcomes = [dict(r) for r in await pool.fetch(
            """SELECT si.candidate_id::text AS candidate_id, so.action
               FROM search_outcomes so
               JOIN search_impressions si ON si.id = so.impression_id
               WHERE si.recruiter_id::text = $1
                 AND so.occurred_at > NOW() - INTERVAL '90 days'""",
            recruiter_id)]

        candidates = (detect_filter_repetition(search_rows)
                      + detect_query_terms(search_rows)
                      + detect_outcome_skew(shown, outcomes))

        dismissed = {r["content_key"] for r in await pool.fetch(
            """SELECT content_key FROM recruiter_memory
               WHERE recruiter_id = $1::uuid AND status = 'dismissed'""",
            recruiter_id)}

        written = 0
        dismissed_skipped = 0
        for obs in candidates:
            if obs["content_key"] in dismissed:
                dismissed_skipped += 1
                continue
            conf = observation_confidence(obs["consistency"], obs["evidence_count"])
            now = datetime.now(timezone.utc).isoformat()
            for e in obs["evidence"]:
                e["at"] = now
            await pool.execute(
                """INSERT INTO recruiter_memory
                     (recruiter_id, kind, category, content, content_key,
                      confidence, evidence_count, evidence, source)
                   VALUES ($1::uuid, 'observation', $2, $3, $4, $5, $6, $7::jsonb,
                           'profiler')
                   ON CONFLICT (recruiter_id, kind, content_key) WHERE status='active'
                   DO UPDATE SET
                     content = EXCLUDED.content,
                     confidence = EXCLUDED.confidence,
                     evidence_count = EXCLUDED.evidence_count,
                     evidence = (
                       SELECT jsonb_agg(e) FROM (
                         SELECT e FROM jsonb_array_elements(
                           recruiter_memory.evidence || EXCLUDED.evidence) e
                         ORDER BY e->>'at' DESC LIMIT 10) s),
                     last_evidence_at = NOW()""",
                recruiter_id, obs["category"], obs["content"], obs["content_key"],
                conf, obs["evidence_count"], json.dumps(obs["evidence"]))
            written += 1

        # Expire stale observations; evict beyond the cap (lowest confidence first).
        await pool.execute(
            """UPDATE recruiter_memory SET status='expired'
               WHERE recruiter_id = $1::uuid AND kind='observation' AND status='active'
                 AND last_evidence_at < NOW() - INTERVAL '30 days'""",
            recruiter_id)
        await pool.execute(
            """UPDATE recruiter_memory SET status='expired'
               WHERE id IN (
                 SELECT id FROM recruiter_memory
                 WHERE recruiter_id = $1::uuid AND kind='observation' AND status='active'
                 ORDER BY confidence DESC, last_evidence_at DESC
                 OFFSET $2)""",
            recruiter_id, MAX_ACTIVE_OBSERVATIONS)
        await pool.execute(
            """INSERT INTO recruiter_preferences (recruiter_id, last_profiled_at)
               VALUES ($1::uuid, NOW())
               ON CONFLICT (recruiter_id) DO UPDATE SET last_profiled_at = NOW()""",
            recruiter_id)

        _obs_update_current_span(output={
            "observations_written": written,
            "candidates_detected": len(candidates),
            "dismissed_skipped": dismissed_skipped,
            "search_rows": len(search_rows),
            "shown_candidates": len(shown),
            "positive_outcomes": len([o for o in outcomes if o.get("action") in POSITIVE_ACTIONS]),
        })
        return written


async def profile_recruiter_if_stale(pool: asyncpg.Pool, recruiter_id: str,
                                     max_age_hours: int = 1) -> None:
    """Background-safe wrapper: run only if last run is older than max_age_hours."""
    try:
        row = await pool.fetchrow(
            """SELECT last_profiled_at FROM recruiter_preferences
               WHERE recruiter_id = $1::uuid""", recruiter_id)
        if row and row["last_profiled_at"] is not None:
            age = datetime.now(timezone.utc) - row["last_profiled_at"]
            if age.total_seconds() < max_age_hours * 3600:
                return
        n = await profile_recruiter(pool, recruiter_id)
        logger.info("profiler: %s observations for %s", n, recruiter_id)
    except Exception as exc:
        logger.warning("profiler failed for %s: %s", recruiter_id, exc)
