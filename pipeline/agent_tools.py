from __future__ import annotations

import datetime as _dt
import json
import logging
import time
import uuid as _uuid
from decimal import Decimal
from typing import Any
from unittest.mock import Mock as _Mock

import asyncpg

from pipeline.agent_session import AgentSession, StackEntry, push_to_stack
from pipeline.db_timing import finalize_db_timing
from pipeline.search import HybridSearchEngine as SearchEngine

logger = logging.getLogger(__name__)


def _jsonable(value: Any) -> Any:
    """Coerce asyncpg/DB values into JSON-serializable Python types."""
    if isinstance(value, _uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", "replace")
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _jsonable_row(row: Any) -> dict[str, Any]:
    return {k: _jsonable(v) for k, v in dict(row).items()}


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)


async def _fetch_with_db_timing(
    pool: asyncpg.Pool,
    sql: str,
    *args: Any,
) -> tuple[list[Any], dict[str, Any]]:
    timings: dict[str, Any] = {}
    total_start = time.perf_counter()
    if isinstance(pool, _Mock):
        timings["pool_acquire_ms"] = 0.0
        fetch_start = time.perf_counter()
        rows = await pool.fetch(sql, *args)
        timings["fetch_roundtrip_ms"] = _elapsed_ms(fetch_start)
        timings["total_ms"] = _elapsed_ms(total_start)
        finalize_db_timing(timings)
        return list(rows), timings

    acquire_start = time.perf_counter()
    async with pool.acquire() as conn:
        timings["pool_acquire_ms"] = _elapsed_ms(acquire_start)
        fetch_start = time.perf_counter()
        rows = await conn.fetch(sql, *args)
        timings["fetch_roundtrip_ms"] = _elapsed_ms(fetch_start)
    timings["total_ms"] = _elapsed_ms(total_start)
    finalize_db_timing(timings)
    return list(rows), timings

# ── Result enrichment helpers ─────────────────────────────────────────────────

def _enrich_result(r: Any, rank: int) -> dict[str, Any]:
    """Build a rich candidate dict that includes all scoring, retrieval, and
    explanation data the agent needs to reason about why a candidate ranked."""
    exp = r.explanation or {}
    checks = exp.get("checks", {})
    breakdown = exp.get("score_breakdown", [])

    compact_breakdown = [
        {
            "signal": s.get("name", ""),
            "score": s.get("score", 0),
            "weight_pct": s.get("weight_pct", s.get("weight_percent", 0)),
        }
        for s in breakdown
        if s.get("weight_pct", s.get("weight_percent", 0)) > 0
    ]

    sal_min = getattr(r, "salary_min", None)
    sal_max = getattr(r, "salary_max", None)
    salary_str = f"{sal_min}–{sal_max}" if (sal_min or sal_max) else None

    return {
        # Identity
        "id": r.candidate_id,
        "rank": rank + 1,
        "name": getattr(r, "full_name", "") or "",
        "city": getattr(r, "city", "") or "",
        "country": getattr(r, "country", "") or "",
        "years_exp": getattr(r, "years_exp", 0),
        "skills": list(getattr(r, "skills", []))[:8],
        "salary_range": salary_str,
        "doc_type": getattr(r, "doc_type", "") or "",
        "document_title": getattr(r, "document_title", "") or "",
        # Final score + tier
        "feature_score": round(getattr(r, "feature_score", 0.0), 1),
        "match_tier": exp.get("match_tier", ""),
        "match_score": exp.get("match_score"),
        "summary_line": exp.get("summary_line", ""),
        # Sub-scores (what the final score is built from)
        "rerank_score": round(r.rerank_score, 3) if getattr(r, "rerank_score", None) is not None else None,
        "fused_rrf_score": round(getattr(r, "fused_rrf_score", 0.0), 4),
        "similarity_score": round(getattr(r, "similarity_score", 0.0), 3),
        "sort_basis": getattr(r, "sort_basis", "fused_rrf_score"),
        # Which retrieval paths found this candidate
        "retrieval_paths": list(getattr(r, "retrieval_paths", [])),
        # Hard + soft constraint checks
        "required_checks": checks.get("required", []),
        "preferred_checks": checks.get("preferred", []),
        # Per-signal score contributions
        "score_breakdown": compact_breakdown,
        # Best matching resume excerpt
        "best_evidence": (exp.get("best_evidence") or getattr(r, "best_chunk", "") or "")[:250],
        "supporting_evidence": list(exp.get("supporting_evidence", []) or [])[:2],
        "ranking_explanation": exp,
    }


def _spec_summary(spec: Any) -> dict[str, Any]:
    """Extract the key planner fields the agent needs to reason about query quality."""
    if spec is None:
        return {}
    must = getattr(spec, "must", None)
    should = getattr(spec, "should", None)
    return {
        "semantic_query": getattr(spec, "semantic_query", ""),
        "input_type": getattr(spec, "input_type", ""),
        "confidence": getattr(spec, "confidence", None),
        "used_fallback": getattr(spec, "used_fallback", False),
        "dropped_items": list(getattr(spec, "dropped_items", []) or []),
        "clarify": getattr(spec, "clarify", None),
        "must_skills": list(getattr(must, "skills", None) or []),
        "should_skills": list(getattr(should, "skills", None) or []),
        "should_themes": list(getattr(should, "themes", None) or []),
        "should_roles": list(getattr(should, "roles", None) or []),
        "should_locations": list(getattr(should, "locations", None) or []),
        "must_location": {
            "city": getattr(must, "city", None),
            "country": getattr(must, "country", None),
        },
        "experience_range": {
            "min_years": getattr(must, "min_years_exp", None),
            "max_years": getattr(must, "max_years_exp", None),
        },
    }


# ── Tier 1: Pipeline tools ────────────────────────────────────────────────────

async def do_run_search(
    pool: asyncpg.Pool,
    session: AgentSession,
    recruiter_id: str,
    query: str,
    filters: dict[str, Any],
    weights: dict[str, Any],
    should: dict[str, Any] | None = None,
    retrieval: dict[str, Any] | None = None,
    mode: str = "no-llm",
    top_k: int = 7,
    search_engine: Any | None = None,
) -> dict[str, Any]:
    """Run a full hybrid search and push the iteration onto the session stack."""
    engine = search_engine or SearchEngine(pool)
    search_mode = mode if mode in {"no-llm", "fast", "quality", "agent-quality"} else "no-llm"
    explicit_filters = dict(filters or {})
    if should:
        explicit_filters["should"] = dict(should)
        # If only soft skills are provided, make that explicit so fallback_plan
        # does not promote those query terms into must.skills.
        explicit_filters.setdefault("skills", [])

    config_overrides = dict(weights or {})
    if retrieval:
        for key in ("keyword_policy", "keyword_timeout_ms"):
            if key in retrieval:
                config_overrides[key] = retrieval[key]

    resp = await engine.smart_search(
        query=query,
        explicit_filters=explicit_filters or None,
        mode=search_mode,
        top_k=max(1, int(top_k or 7)),
        recruiter_id=recruiter_id,
        config_overrides=config_overrides,
    )

    preview = [_enrich_result(r, i) for i, r in enumerate(resp.results[:10])]
    spec_sum = _spec_summary(getattr(resp, "spec", None))
    retrieval_policy = getattr(resp, "retrieval_policy", None)
    if not isinstance(retrieval_policy, dict):
        retrieval_policy = {}
    candidate_ids = list(getattr(resp, "candidate_ids", []) or [r.candidate_id for r in resp.results])
    deferred_candidate_ids = list(getattr(resp, "deferred_candidate_ids", []) or [])

    entry = StackEntry(
        query=query,
        filters=explicit_filters,
        results_preview=preview,
        agent_reasoning="",
        spec_summary=spec_sum,
        total_scanned=getattr(resp, "total_candidates_scanned", 0),
        latency_ms=round(sum((getattr(resp, "phase_timings", {}) or {}).values()), 2),
        timings_ms={},
        phase_timings=dict(getattr(resp, "phase_timings", {}) or {}),
        mode=search_mode,
        candidate_ids=candidate_ids,
        deferred_candidate_ids=deferred_candidate_ids,
    )
    push_to_stack(session, entry)

    result = {
        "results": preview,
        "total": max(len(resp.results), len(candidate_ids)),
        "total_scanned": getattr(resp, "total_candidates_scanned", 0),
        "candidate_ids": candidate_ids,
        "deferred_candidate_ids": deferred_candidate_ids,
        "iteration_id": entry.iteration_id,
        "query": query,
        "filters": explicit_filters,
        "mode": search_mode,
        "top_k": max(1, int(top_k or 7)),
        "spec_summary": spec_sum,
        "retrieval_policy": dict(retrieval_policy),
        "relaxations_applied": list(getattr(resp, "relaxations_applied", []) or []),
        "clarify_notice": getattr(resp, "clarify", None),
        "latency_ms": entry.latency_ms,
        "timings_ms": entry.timings_ms,
        "phase_timings": entry.phase_timings,
    }

    # ── Auto-recovery ─────────────────────────────────────────────────────────
    # When a search comes back weak (empty, low average score, the planner asked
    # to clarify, or low planner confidence), attach the full diagnostic inline so
    # the agent reasons from real ranking signals instead of guessing or
    # hallucinating over a thin result set. This is deterministic and cheap — the
    # diagnostic runs in-memory over the preview we just built.
    #
    # NOTE: `used_fallback` is deliberately NOT a trigger on its own. Regex
    # fallback often parses simple queries perfectly; only flag it when the
    # *results* are also weak (handled inside do_explain_poor_results).
    avg_score = (
        sum(r.get("feature_score", 0) for r in preview) / len(preview)
        if preview else 0.0
    )
    conf = spec_sum.get("confidence")
    triggers: list[str] = []
    if not preview:
        triggers.append("zero results")
    if preview and avg_score < 55:
        triggers.append(f"low average score ({avg_score:.0f}/100)")
    if result["clarify_notice"]:
        triggers.append("planner requested clarification")
    if isinstance(conf, (int, float)) and conf < 0.6:
        triggers.append(f"low planner confidence ({conf:.0%})")

    if triggers:
        diagnostic = await do_explain_poor_results(pool, session)
        result["recovery"] = {
            "triggered": True,
            "why": triggers,
            "avg_feature_score": round(avg_score, 1),
            "diagnostic": diagnostic,
            "instruction": (
                "Results are weak. Diagnose before responding: read `diagnostic.issues`, "
                "distinguish 'wrong query' (low confidence / used_fallback) from 'filters "
                "too strict' (required checks failing) from 'nothing exists' (verify with "
                "keyword_search before concluding). Propose ONE specific fix; never invent "
                "candidates or numbers."
            ),
        }

    return result


async def do_modify_and_search(
    pool: asyncpg.Pool,
    session: AgentSession,
    recruiter_id: str,
    changes: dict[str, Any],
    search_engine: Any | None = None,
) -> dict[str, Any]:
    """Apply changes to the current stack top and run a new search."""
    base = session.search_stack[-1] if session.search_stack else None
    base_query = changes.get("query") or (base.query if base else "")
    base_filters = {**(base.filters if base else {}), **(changes.get("add_filters") or {})}
    if changes.get("should"):
        base_filters["should"] = dict(changes["should"])
        base_filters.setdefault("skills", [])
    if changes.get("add_should"):
        merged_should = dict(base_filters.get("should") or {})
        for key, value in dict(changes["add_should"]).items():
            if isinstance(value, list):
                existing = list(merged_should.get(key) or [])
                merged_should[key] = list(dict.fromkeys(existing + value))
            else:
                merged_should[key] = value
        base_filters["should"] = merged_should
        base_filters.setdefault("skills", [])
    for key in (changes.get("remove_should") or []):
        if isinstance(base_filters.get("should"), dict):
            base_filters["should"].pop(key, None)
    mode = changes.get("mode") or (base.mode if base else "no-llm")
    for key in (changes.get("remove_filters") or []):
        base_filters.pop(key, None)
    if base_filters.get("should"):
        base_filters.setdefault("skills", [])

    return await do_run_search(
        pool,
        session,
        recruiter_id,
        base_query,
        base_filters,
        changes.get("weights") or {},
        retrieval=changes.get("retrieval"),
        mode=mode,
        top_k=max(1, int(changes.get("top_k") or 7)),
        search_engine=search_engine,
    )


async def do_explain_poor_results(
    pool: asyncpg.Pool,
    session: AgentSession,
) -> dict[str, Any]:
    """Return a rich diagnostic summary using actual ranking signals and spec quality."""
    if not session.search_stack:
        return {"explanation": "No search has been run yet in this session."}

    top = session.search_stack[-1]
    results = top.results_preview
    spec = top.spec_summary

    if not results:
        return {
            "explanation": "The last search returned no results.",
            "spec_summary": spec,
            "suggested_actions": [
                "Check if must-have skills are too rare in the DB",
                "Verify the location filter — try removing it to test",
                "Try keyword_search to see if candidates exist at all for this role",
            ],
        }

    scores = [r.get("feature_score", 0) for r in results]
    avg_score = sum(scores) / len(scores) if scores else 0

    # Retrieval path analysis
    all_paths = [tuple(sorted(r.get("retrieval_paths", []))) for r in results]
    path_dist: dict[str, int] = {}
    for p in all_paths:
        key = "+".join(p) if p else "none"
        path_dist[key] = path_dist.get(key, 0) + 1

    dense_only = sum(1 for p in all_paths if p == ("dense",))
    no_skill_match = sum(1 for p in all_paths if "skill" not in p)

    # Required check failures across top 5
    failed_required: dict[str, int] = {}
    for r in results[:5]:
        for check in r.get("required_checks", []):
            if not check.get("matched"):
                lbl = check.get("label", "unknown")
                failed_required[lbl] = failed_required.get(lbl, 0) + 1

    # Tier distribution
    tier_counts = {"Strong match": 0, "Good": 0, "Partial": 0}
    for r in results:
        t = r.get("match_tier", "")
        if t in tier_counts:
            tier_counts[t] += 1

    # Weak high-weight signals on #1 candidate
    weak_signals = []
    if results:
        for s in results[0].get("score_breakdown", []):
            if s.get("score", 100) < 30 and s.get("weight_pct", 0) >= 10:
                weak_signals.append(f"{s['signal']} ({s['score']:.0f}/100, {s['weight_pct']}% weight)")

    issues: list[str] = []
    suggestions: list[str] = []

    if avg_score < 35:
        issues.append(f"Very low avg score ({avg_score:.0f}/100) — candidates don't match the domain.")
        suggestions.append("Try a completely different query or broaden skills to just the core technology")
    elif avg_score < 55:
        issues.append(f"Below-average scores ({avg_score:.0f}/100) — only partial matches.")
        suggestions.append("Relax or remove the most restrictive filter via modify_and_search")

    if failed_required:
        top_fails = sorted(failed_required.items(), key=lambda x: -x[1])[:3]
        for lbl, cnt in top_fails:
            issues.append(f"Required check '{lbl}' failed in {cnt}/5 top candidates.")
        suggestions.append(f"Move '{top_fails[0][0]}' from required to preferred, or remove it")

    if dense_only > len(results) * 0.6:
        issues.append(
            f"{dense_only}/{len(results)} candidates matched via semantic embedding only "
            f"(no keyword or skill path). Results may be tangentially related."
        )
        suggestions.append("Add explicit skill names to the query — keyword and skill signals are absent")

    if no_skill_match == len(results) and spec.get("must_skills"):
        issues.append(
            f"None of the top results matched via the skill path despite requiring "
            f"{spec['must_skills']}. These skills may be rare or stored differently."
        )
        suggestions.append("Try keyword_search with the skill name to check if it exists in resumes")

    if spec.get("used_fallback"):
        issues.append("Planner fell back to regex parsing (LLM call failed). Filters may be inaccurate.")
        suggestions.append("Rephrase as a simple sentence: 'senior React engineer in London with 5+ years'")

    if isinstance(spec.get("confidence"), (int, float)) and spec["confidence"] < 0.6:
        issues.append(f"Low planner confidence ({spec['confidence']:.0%}) — query is ambiguous.")
        suggestions.append("Name the role, core skill, and location explicitly")

    if spec.get("dropped_items"):
        issues.append(f"Planner rejected these fields (not applied): {spec['dropped_items']}")

    if weak_signals:
        issues.append(f"Top candidate has weak high-weight signals: {weak_signals}")

    return {
        "query": top.query,
        "filters": top.filters,
        "spec_summary": spec,
        "result_count": len(results),
        "total_scanned": top.total_scanned,
        "avg_feature_score": round(avg_score, 1),
        "tier_distribution": tier_counts,
        "retrieval_path_distribution": path_dist,
        "top_required_check_failures": [
            {"check": k, "failed_in_top_5": v}
            for k, v in sorted(failed_required.items(), key=lambda x: -x[1])
        ],
        "issues": issues or ["No obvious issues — scores look reasonable."],
        "suggested_actions": suggestions or ["Try modify_and_search with a slightly different query"],
    }


async def do_compare_iterations(
    entry_a: StackEntry,
    entry_b: StackEntry,
) -> dict[str, Any]:
    """Compare two search iterations — scores, tiers, paths, and spec changes."""
    ids_a = {r["id"] for r in entry_a.results_preview}
    ids_b = {r["id"] for r in entry_b.results_preview}
    kept = ids_a & ids_b

    scores_a = {r["id"]: r.get("feature_score", 0) for r in entry_a.results_preview}
    scores_b = {r["id"]: r.get("feature_score", 0) for r in entry_b.results_preview}
    avg_a = sum(scores_a.values()) / len(scores_a) if scores_a else 0
    avg_b = sum(scores_b.values()) / len(scores_b) if scores_b else 0

    score_deltas = {
        cid: round(scores_b[cid] - scores_a[cid], 1)
        for cid in kept
        if abs(scores_b.get(cid, 0) - scores_a.get(cid, 0)) > 0.5
    }

    def _tiers(entry: StackEntry) -> dict[str, int]:
        t: dict[str, int] = {"Strong match": 0, "Good": 0, "Partial": 0}
        for r in entry.results_preview:
            tier = r.get("match_tier", "")
            if tier in t:
                t[tier] += 1
        return t

    def _path_dist(entry: StackEntry) -> dict[str, int]:
        d: dict[str, int] = {}
        for r in entry.results_preview:
            key = "+".join(sorted(r.get("retrieval_paths", []))) or "none"
            d[key] = d.get(key, 0) + 1
        return d

    return {
        "query": {"before": entry_a.query, "after": entry_b.query},
        "filters": {"before": entry_a.filters, "after": entry_b.filters},
        "spec_changes": {
            "before": entry_a.spec_summary,
            "after": entry_b.spec_summary,
        },
        "results_gained": list(ids_b - ids_a),
        "results_lost": list(ids_a - ids_b),
        "results_kept": list(kept),
        "avg_score": {
            "before": round(avg_a, 1),
            "after": round(avg_b, 1),
            "delta": round(avg_b - avg_a, 1),
        },
        "score_changes_for_kept": score_deltas,
        "tier_shift": {"before": _tiers(entry_a), "after": _tiers(entry_b)},
        "retrieval_path_shift": {"before": _path_dist(entry_a), "after": _path_dist(entry_b)},
    }


async def do_get_candidate_details(
    pool: asyncpg.Pool,
    candidate_ids: list[str],
    limit: int = 20,
) -> dict[str, Any]:
    """Fetch full profiles for several candidates in one DB round trip."""
    ids: list[str] = []
    seen: set[str] = set()
    for candidate_id in candidate_ids or []:
        if not _looks_like_uuid(candidate_id):
            continue
        cid = str(candidate_id)
        if cid not in seen:
            seen.add(cid)
            ids.append(cid)
        if len(ids) >= max(1, min(int(limit or 20), 50)):
            break

    if not ids:
        return {"candidates": [], "count": 0, "missing_ids": list(candidate_ids or [])}

    rows, db_timing = await _fetch_with_db_timing(
        pool,
        """
        WITH requested AS (
            SELECT id::uuid AS id, ord
            FROM unnest($1::uuid[]) WITH ORDINALITY AS t(id, ord)
        )
        SELECT
            c.id::text AS id,
            c.full_name,
            c.email,
            c.city,
            c.country,
            c.years_exp,
            c.salary_min,
            c.salary_max,
            COALESCE(skill_rows.skills, c.skills, '{}'::text[]) AS skills,
            best.content AS best_chunk,
            best.doc_type,
            best.title AS document_title
        FROM requested req
        JOIN candidates c ON c.id = req.id
        LEFT JOIN LATERAL (
            SELECT array_agg(cs.skill ORDER BY cs.skill) AS skills
            FROM candidate_skills cs
            WHERE cs.candidate_id = c.id
        ) skill_rows ON TRUE
        LEFT JOIN LATERAL (
            SELECT dc.content, cd.doc_type, cd.title
            FROM document_chunks dc
            JOIN candidate_documents cd ON cd.id = dc.document_id
            WHERE dc.candidate_id = c.id
            ORDER BY dc.created_at
            LIMIT 1
        ) best ON TRUE
        ORDER BY req.ord
        """,
        ids,
    )

    candidates = [
        {
            "id": str(row["id"]),
            "candidate_id": str(row["id"]),
            "full_name": row["full_name"],
            "name": row["full_name"],
            "email": row["email"],
            "city": row["city"],
            "country": row["country"],
            "years_exp": row["years_exp"],
            "salary_min": row["salary_min"],
            "salary_max": row["salary_max"],
            "skills": list(row["skills"] or []),
            "best_chunk": (row["best_chunk"] or "")[:400],
            "best_evidence": (row["best_chunk"] or "")[:400],
            "doc_type": row["doc_type"] or "",
            "document_title": row["document_title"] or "",
        }
        for row in rows
    ]
    found = {c["id"] for c in candidates}
    return {
        "candidates": candidates,
        "count": len(candidates),
        "missing_ids": [cid for cid in ids if cid not in found],
        "db_timing": db_timing,
    }


async def do_get_candidate_detail(
    pool: asyncpg.Pool,
    candidate_id: str,
) -> dict[str, Any]:
    """Fetch full candidate profile from the DB."""
    result = await do_get_candidate_details(pool, [candidate_id], limit=1)
    candidates = result.get("candidates") or []
    if not candidates:
        return {"error": f"Candidate {candidate_id} not found"}
    detail = dict(candidates[0])
    detail.pop("candidate_id", None)
    detail.pop("name", None)
    detail.pop("best_evidence", None)
    if result.get("db_timing"):
        detail["db_timing"] = result["db_timing"]
    return detail


async def do_save_hint(
    pool: asyncpg.Pool,
    recruiter_id: str,
    hint_text: str,
) -> dict[str, Any]:
    """Persist a confirmed recruiter preference as a memory fact."""
    hint_text = hint_text.strip()[:200]
    if not hint_text:
        return {"error": "Hint text is empty"}
    n = await pool.fetchval(
        """SELECT count(*) FROM recruiter_memory
           WHERE recruiter_id = $1::uuid AND kind='fact' AND status='active'""",
        recruiter_id)
    if n >= 20:
        return {"error": "Preference cap reached (20). Ask the recruiter to remove one first."}
    await pool.execute(
        """INSERT INTO recruiter_memory
             (recruiter_id, kind, category, content, content_key, source)
           VALUES ($1::uuid, 'fact', 'other', $2, 'agent:' || MD5(LOWER($2)), 'agent')
           ON CONFLICT (recruiter_id, kind, content_key) WHERE status='active'
           DO NOTHING""",
        recruiter_id, hint_text)
    return {"saved": hint_text}


_VALID_OBS_CATEGORIES = frozenset({
    "skill", "location", "seniority", "company_stage", "work_style", "salary", "other"
})


async def do_add_observation(
    pool: asyncpg.Pool,
    recruiter_id: str,
    content: str,
    category: str,
) -> dict[str, Any]:
    """Agent-created observation. Goes into the normal confirm flow."""
    category = category.lower().strip()
    if category not in _VALID_OBS_CATEGORIES:
        category = "other"
    import hashlib
    content_key = "agent:" + hashlib.md5(content.lower().encode()).hexdigest()[:12]
    import json as _json
    evidence = _json.dumps([{"type": "agent_observation", "detail": content}])
    row = await pool.fetchrow(
        """INSERT INTO recruiter_memory
             (recruiter_id, kind, category, content, content_key,
              confidence, evidence_count, evidence, source, status)
           VALUES ($1::uuid, 'observation', $2, $3, $4,
                   0.75, 1, $5::jsonb, 'agent', 'active')
           ON CONFLICT (recruiter_id, kind, content_key) WHERE status='active'
           DO UPDATE SET
             evidence_count = recruiter_memory.evidence_count + 1,
             confidence = LEAST(0.95, recruiter_memory.confidence + 0.05),
             last_evidence_at = NOW()
           RETURNING id, content, confidence""",
        recruiter_id, category, content, content_key, evidence)
    return {"observation_id": str(row["id"]), "content": row["content"],
            "confidence": float(row["confidence"])}


async def do_confirm_observation(
    pool: asyncpg.Pool,
    recruiter_id: str,
    observation_id: str,
    accept: bool,
) -> dict[str, Any]:
    """Recruiter answered an observation question. yes → promote to fact;
    no → dismiss permanently (the profiler will never recreate that key)."""
    row = await pool.fetchrow(
        """SELECT id, category, content, content_key FROM recruiter_memory
           WHERE id = $1::uuid AND recruiter_id = $2::uuid
             AND kind = 'observation' AND status = 'active'""",
        observation_id, recruiter_id)
    if not row:
        return {"error": f"Observation {observation_id} not found or not active"}
    if not accept:
        await pool.execute(
            "UPDATE recruiter_memory SET status='dismissed' WHERE id = $1::uuid",
            observation_id)
        _record_observation_score(observation_id, row["content"], accepted=False)
        return {"dismissed": row["content"]}
    await pool.execute(
        "UPDATE recruiter_memory SET status='promoted' WHERE id = $1::uuid",
        observation_id)
    await pool.execute(
        """INSERT INTO recruiter_memory
             (recruiter_id, kind, category, content, content_key, source)
           VALUES ($1::uuid, 'fact', $2, $3, $4, 'profiler')
           ON CONFLICT (recruiter_id, kind, content_key) WHERE status='active'
           DO NOTHING""",
        recruiter_id, row["category"], row["content"], row["content_key"])
    _record_observation_score(observation_id, row["content"], accepted=True)
    return {"promoted_to_fact": row["content"]}


def _record_observation_score(observation_id: str, content: str, *, accepted: bool) -> None:
    """Record a Langfuse score when the recruiter confirms/dismisses an observation."""
    try:
        from pipeline.observability import record_score, ScoreName, get_current_trace_id
        trace_id = get_current_trace_id()
        if trace_id:
            record_score(
                trace_id=trace_id,
                name=ScoreName.PERSONALIZATION_ACCEPTED,
                value=1.0 if accepted else 0.0,
                comment=f"{'Accepted' if accepted else 'Dismissed'} observation: {content}",
            )
    except Exception:
        pass  # observability never breaks the happy path


async def do_view_main_results(
    pool: asyncpg.Pool,
    candidate_ids: list[str],
    limit: int = 20,
    session: AgentSession | None = None,
) -> list[dict[str, Any]]:
    """Hydrate the recruiter's on-screen result IDs into candidate cards.

    Used when the agent has not run its own search but the recruiter has results
    on screen. Preserves the on-screen ordering of `candidate_ids`.
    """
    ids = [cid for cid in candidate_ids[:limit] if cid]
    if not ids:
        return []

    ranked_by_id: dict[str, dict[str, Any]] = {}
    if session is not None:
        for entry in reversed(session.search_stack):
            for item in entry.results_preview or []:
                cid = str(item.get("id") or item.get("candidate_id") or "")
                if cid and cid not in ranked_by_id:
                    ranked_by_id[cid] = dict(item)

    out: list[dict[str, Any]] = []
    missing_ids: list[str] = []
    for cid in ids:
        ranked = ranked_by_id.get(str(cid))
        if ranked:
            ranked.setdefault("candidate_id", ranked.get("id"))
            ranked.setdefault("score_available", True)
            out.append(ranked)
        else:
            missing_ids.append(cid)

    if not missing_ids:
        return out

    rows = await pool.fetch(
        """
        SELECT
            c.id,
            c.full_name,
            c.email,
            c.city,
            c.country,
            c.years_exp,
            c.salary_min,
            c.salary_max,
            c.skills,
            best.content AS best_chunk,
            best.doc_type,
            best.title AS document_title
        FROM candidates c
        LEFT JOIN LATERAL (
            SELECT dc.content, cd.doc_type, cd.title
            FROM document_chunks dc
            JOIN candidate_documents cd ON cd.id = dc.document_id
            WHERE dc.candidate_id = c.id
            ORDER BY dc.created_at DESC
            LIMIT 1
        ) best ON TRUE
        WHERE c.id = ANY($1::uuid[])
        """,
        missing_ids,
    )
    by_id = {str(r["id"]): r for r in rows}
    for cid in ids:
        if str(cid) in ranked_by_id:
            continue
        r = by_id.get(str(cid))
        if not r:
            continue
        best_chunk = (r["best_chunk"] or "")[:400]
        out.append({
            "id": str(r["id"]),
            "candidate_id": str(r["id"]),
            "name": r["full_name"],
            "email": r["email"],
            "city": r["city"],
            "country": r["country"],
            "years_exp": r["years_exp"],
            "salary_min": r["salary_min"],
            "salary_max": r["salary_max"],
            "skills": list(r["skills"] or [])[:5],
            "feature_score": None,
            "score_available": False,
            "match_tier": "Selected",
            "best_evidence": best_chunk,
            "doc_type": r["doc_type"] or "",
            "document_title": r["document_title"] or "",
            "retrieval_paths": ["agent_selected"],
            "ranking_explanation": _selected_candidate_explanation(
                full_name=r["full_name"],
                rank_position=len(out) + 1,
                best_evidence=best_chunk,
            ),
        })
    return out


def _selected_candidate_explanation(
    *,
    full_name: str,
    rank_position: int,
    best_evidence: str,
) -> dict[str, Any]:
    return {
        "match_tier": "Selected by agent",
        "match_score": None,
        "pipeline_confidence": None,
        "confidence_label": "selected",
        "score_basis": "agent_selected",
        "sort_basis": "agent selected order",
        "rank_position": rank_position,
        "summary_line": (
            f"{full_name} was surfaced by the agent from a database/profile lookup, "
            "not from a scored hybrid ranking. Run or rerun search to compute a "
            "feature_score for this candidate."
        ),
        "checks": {
            "required": [{"label": "Selected by agent from database lookup", "matched": True}],
            "preferred": [],
        },
        "score_breakdown": [],
        "best_evidence": best_evidence,
        "supporting_evidence": [best_evidence] if best_evidence else [],
        "retrieval_paths": ["agent_selected"],
    }


# ── Tier 2: Lightweight DB / pool tools ───────────────────────────────────────

async def do_keyword_search(
    pool: asyncpg.Pool,
    query: str,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """BM25-style FTS keyword search over candidate document chunks.

    Resume/document text lives in document_chunks.content (with a generated
    content_tsv column + GIN index); the candidates table has no text body.

    SEMANTICS: all words in `query` are AND-ed (plainto_tsquery). So
    "quantum computing qiskit" matches only docs containing *all three* —
    usually far too strict. Pass ONE concept per call ("quantum computing", or
    "qiskit") and call it multiple times to probe distinct concepts. That keeps
    each probe precise: a 0 for "qiskit" definitively means no qiskit résumés,
    whereas OR-ing words would falsely match anyone with the word "computing".
    """
    rows = await pool.fetch(
        """SELECT c.id, c.full_name, c.city, c.country, c.years_exp,
                  COALESCE(
                      MAX(ts_rank(dc.content_tsv, plainto_tsquery('english', $1))),
                      0
                  ) AS score
           FROM candidates c
           LEFT JOIN document_chunks dc
                  ON dc.candidate_id = c.id
                 AND dc.content_tsv @@ plainto_tsquery('english', $1)
           WHERE c.status = 'active'
             AND (dc.id IS NOT NULL OR c.full_name ILIKE '%' || $1 || '%')
           GROUP BY c.id, c.full_name, c.city, c.country, c.years_exp
           ORDER BY score DESC
           LIMIT $2""",
        query, limit,
    )
    return [
        {
            "id": str(r["id"]),
            "full_name": r["full_name"],
            "city": r["city"],
            "country": r["country"],
            "years_exp": r["years_exp"],
            "score": float(r["score"]),
        }
        for r in rows
    ]


async def do_keyword_search_batch(
    pool: asyncpg.Pool,
    queries: list[str],
    limit_per_query: int = 10,
) -> dict[str, Any]:
    """Run several precise keyword probes in one SQL call."""
    clean_queries: list[str] = []
    seen: set[str] = set()
    for item in queries or []:
        q = str(item or "").strip()
        q_key = q.lower()
        if not q or q_key in seen:
            continue
        seen.add(q_key)
        clean_queries.append(q)
        if len(clean_queries) >= 12:
            break
    if not clean_queries:
        return {"queries": [], "results": {}, "count": 0}

    limit_per_query = max(1, min(int(limit_per_query or 10), 50))
    rows, db_timing = await _fetch_with_db_timing(
        pool,
        """
        WITH q AS (
            SELECT term, ord
            FROM unnest($1::text[]) WITH ORDINALITY AS t(term, ord)
        ),
        scored AS (
            SELECT
                q.term,
                q.ord,
                c.id,
                c.full_name,
                c.city,
                c.country,
                c.years_exp,
                COALESCE(
                    MAX(ts_rank(dc.content_tsv, plainto_tsquery('english', q.term))),
                    0
                ) AS score
            FROM q
            JOIN candidates c ON c.status = 'active'
            LEFT JOIN document_chunks dc
                   ON dc.candidate_id = c.id
                  AND dc.content_tsv @@ plainto_tsquery('english', q.term)
            WHERE dc.id IS NOT NULL OR c.full_name ILIKE '%' || q.term || '%'
            GROUP BY q.term, q.ord, c.id, c.full_name, c.city, c.country, c.years_exp
        ),
        ranked AS (
            SELECT *,
                   ROW_NUMBER() OVER (PARTITION BY term ORDER BY score DESC, full_name ASC) AS rn
            FROM scored
        )
        SELECT term, id, full_name, city, country, years_exp, score
        FROM ranked
        WHERE rn <= $2
        ORDER BY ord, rn
        """,
        clean_queries,
        limit_per_query,
    )
    grouped: dict[str, list[dict[str, Any]]] = {q: [] for q in clean_queries}
    for row in rows:
        grouped.setdefault(row["term"], []).append({
            "id": str(row["id"]),
            "full_name": row["full_name"],
            "city": row["city"],
            "country": row["country"],
            "years_exp": row["years_exp"],
            "score": float(row["score"]),
        })
    return {
        "queries": clean_queries,
        "results": grouped,
        "count": sum(len(v) for v in grouped.values()),
        "db_timing": db_timing,
    }


async def do_list_skills(
    pool: asyncpg.Pool,
    query: str,
    limit: int = 60,
) -> dict[str, Any]:
    """Return the canonical skill vocabulary stored in the DB (with candidate counts).

    Skills are filtered with `candidates.skills @> [...]` — EXACT match against
    these stored values. They are stored in a specific canonical form (often
    hyphenated, e.g. 'machine-learning', 'deep-learning'), so 'ML' or
    'machine learning' will match nothing. Use this to find the exact name to
    re-search with. `query` substring-filters the list; '' returns the most common.
    """
    q = (query or "").strip()
    if q:
        rows, db_timing = await _fetch_with_db_timing(
            pool,
            """SELECT skill, count(*) AS n FROM candidate_skills
               WHERE skill ILIKE '%' || $1 || '%'
               GROUP BY skill ORDER BY n DESC LIMIT $2""",
            q, limit,
        )
    else:
        rows, db_timing = await _fetch_with_db_timing(
            pool,
            """SELECT skill, count(*) AS n FROM candidate_skills
               GROUP BY skill ORDER BY n DESC LIMIT $1""",
            max(limit, 200),
        )
    return {
        "query": q,
        "match_count": len(rows),
        "skills": [{"skill": r["skill"], "candidates": r["n"]} for r in rows],
        "db_timing": db_timing,
    }


async def do_list_skills_batch(
    pool: asyncpg.Pool,
    queries: list[str],
    limit_per_query: int = 20,
) -> dict[str, Any]:
    """Return canonical skill matches for several query terms in one call."""
    clean_queries: list[str] = []
    seen: set[str] = set()
    for item in queries or []:
        q = str(item or "").strip().lower()
        if q in seen:
            continue
        seen.add(q)
        clean_queries.append(q)
        if len(clean_queries) >= 12:
            break

    if not clean_queries:
        return {"queries": [], "results": {}, "count": 0}

    limit_per_query = max(1, min(int(limit_per_query or 20), 60))
    rows, db_timing = await _fetch_with_db_timing(
        pool,
        """
        WITH q AS (
            SELECT term, ord
            FROM unnest($1::text[]) WITH ORDINALITY AS t(term, ord)
        ),
        ranked AS (
            SELECT
                q.term,
                cs.skill,
                COUNT(*) AS n,
                ROW_NUMBER() OVER (
                    PARTITION BY q.term
                    ORDER BY COUNT(*) DESC, cs.skill ASC
                ) AS rn
            FROM q
            JOIN candidate_skills cs
              ON q.term = '' OR cs.skill ILIKE '%' || q.term || '%'
            GROUP BY q.term, cs.skill
        )
        SELECT term, skill, n
        FROM ranked
        WHERE rn <= $2
        ORDER BY term, n DESC, skill ASC
        """,
        clean_queries,
        limit_per_query,
    )
    grouped: dict[str, list[dict[str, Any]]] = {q: [] for q in clean_queries}
    for row in rows:
        grouped.setdefault(row["term"], []).append({
            "skill": row["skill"],
            "candidates": row["n"],
        })
    return {
        "queries": clean_queries,
        "results": grouped,
        "count": sum(len(v) for v in grouped.values()),
        "db_timing": db_timing,
    }


async def do_load_candidate_pool(
    pool: asyncpg.Pool,
    session: AgentSession,
    criteria: dict[str, Any],
    limit: int = 100,
) -> dict[str, Any]:
    """Load up to `limit` candidates matching simple criteria into the session pool."""
    clauses = ["c.status = 'active'"]
    params: list[Any] = []
    idx = 1

    if criteria.get("city"):
        clauses.append(f"c.city ILIKE '%' || ${idx} || '%'")
        params.append(criteria["city"])
        idx += 1
    if criteria.get("min_years_exp"):
        clauses.append(f"c.years_exp >= ${idx}")
        params.append(criteria["min_years_exp"])
        idx += 1
    if criteria.get("max_salary"):
        clauses.append(f"c.salary_max <= ${idx}")
        params.append(criteria["max_salary"])
        idx += 1

    params.append(limit)
    where = " AND ".join(clauses)

    rows = await pool.fetch(
        f"""SELECT c.id, c.full_name, c.city, c.country, c.years_exp,
                   c.salary_min, c.salary_max
            FROM candidates c
            WHERE {where}
            LIMIT ${idx}""",
        *params,
    )
    pool_data = [
        {
            "id": str(r["id"]),
            "full_name": r["full_name"],
            "city": r["city"],
            "country": r["country"],
            "years_exp": r["years_exp"],
            "salary_min": r["salary_min"],
            "salary_max": r["salary_max"],
        }
        for r in rows
    ]
    session.candidate_pool = pool_data
    return {"pool": pool_data, "count": len(pool_data)}


def do_filter_from_pool(
    session: AgentSession,
    criteria: dict[str, Any],
) -> list[dict[str, Any]]:
    """Filter the session's candidate pool in-memory by simple key=value criteria."""
    results = session.candidate_pool
    for key, value in criteria.items():
        if isinstance(value, str):
            results = [c for c in results if str(c.get(key, "")).lower() == value.lower()]
        elif isinstance(value, (int, float)):
            results = [c for c in results if c.get(key) == value]
    return results


def do_aggregate_pool(
    session: AgentSession,
    dimension: str,
) -> dict[str, int]:
    """Count pool candidates grouped by a dimension field (e.g. 'city', 'country')."""
    counts: dict[str, int] = {}
    for c in session.candidate_pool:
        val = str(c.get(dimension, "unknown"))
        counts[val] = counts.get(val, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))


async def do_list_recent_sessions(
    pool: asyncpg.Pool,
    recruiter_id: str,
    limit: int = 8,
) -> dict[str, Any]:
    """Return compact summaries for this recruiter's recent copilot sessions."""
    safe_limit = max(1, min(int(limit or 8), 20))
    rows = await pool.fetch(
        """
        SELECT session_id, title, summary, updated_at
        FROM agent_chat_sessions
        WHERE recruiter_id = $1::uuid
        ORDER BY updated_at DESC
        LIMIT $2
        """,
        recruiter_id,
        safe_limit,
    )
    sessions = [
        {
            "session_id": str(row["session_id"]),
            "title": row["title"] or "Untitled session",
            "summary": row["summary"] or "",
            "updated_at": _jsonable(row["updated_at"]),
        }
        for row in rows
    ]
    return {"sessions": sessions, "count": len(sessions)}


# ── Tier 3: Ad-hoc read-only SQL ─────────────────────────────────────────────

import asyncio
import re

_BLOCKED_KEYWORDS = {"UPDATE", "DELETE", "INSERT", "DROP", "TRUNCATE", "ALTER", "CREATE", "GRANT", "REVOKE"}
_ALLOWED_TABLES = {
    "candidates",
    "candidate_skills",
    "candidate_documents",
    "document_chunks",
}


def _check_sql_safety(sql: str) -> str | None:
    """Return an error message if SQL is not safe, else None."""
    upper = sql.upper()
    tokens = set(upper.split())
    for kw in _BLOCKED_KEYWORDS:
        if kw in tokens:
            return f"Query contains '{kw}' which is not allowed. Only SELECT is permitted."
    tables_in_query = {t.lower() for t in re.findall(r"FROM\s+(\w+)", upper)}
    tables_in_query |= {t.lower() for t in re.findall(r"JOIN\s+(\w+)", upper)}
    disallowed = tables_in_query - _ALLOWED_TABLES
    if disallowed:
        return f"Tables not allowed: {disallowed}. Allowed: {_ALLOWED_TABLES}"
    return None


def _inject_limit(sql: str, limit: int = 50) -> str:
    """Append LIMIT if not already present."""
    if "LIMIT" not in sql.upper():
        return sql.rstrip("; \n") + f" LIMIT {limit}"
    return sql


def _looks_like_uuid(value: Any) -> bool:
    try:
        _uuid.UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False


def _extract_candidate_ids_from_rows(rows: list[dict[str, Any]], sql: str) -> list[str]:
    """Best-effort extraction for display hydration from ad-hoc SELECT results.

    Explicit `candidate_id` wins. A bare `id` is treated as a candidate id only
    when the row also contains candidate-like fields, which avoids accidentally
    rendering document/chunk ids from joins.
    """
    candidate_like_fields = {"full_name", "name", "city", "country", "years_exp", "skills"}
    ids: list[str] = []
    seen: set[str] = set()
    for row in rows:
        candidate_id = row.get("candidate_id") or row.get("candidateId")
        if not candidate_id and "id" in row and (candidate_like_fields & set(row)):
            candidate_id = row.get("id")
        if not _looks_like_uuid(candidate_id):
            continue
        cid = str(candidate_id)
        if cid not in seen:
            seen.add(cid)
            ids.append(cid)
    return ids


async def do_query_candidates_db(
    pool: asyncpg.Pool,
    sql: str,
) -> dict[str, Any]:
    """Execute an ad-hoc read-only SELECT against the candidates schema."""
    error = _check_sql_safety(sql)
    if error:
        return {"error": error}

    safe_sql = _inject_limit(sql)

    try:
        rows = await asyncio.wait_for(pool.fetch(safe_sql), timeout=5.0)
        json_rows = [_jsonable_row(r) for r in rows]
        result: dict[str, Any] = {"rows": json_rows, "count": len(json_rows)}
        candidate_ids = _extract_candidate_ids_from_rows(json_rows, safe_sql)
        if candidate_ids:
            display_results = await do_view_main_results(pool, candidate_ids[:20])
            result["candidate_ids"] = candidate_ids
            result["display_results"] = display_results
            result["display"] = {
                "result_kind": "selected",
                "source": "query_candidates_db",
                "query": "Candidates from database query",
                "count": len(display_results),
            }
        return result
    except asyncio.TimeoutError:
        return {"error": "Query timed out after 5 seconds"}
    except Exception as exc:
        logger.warning("Agent SQL query failed: %s | sql=%s", exc, safe_sql[:200])
        return {"error": f"Query failed: {exc}"}


# ── New UX and Intelligence Tools ──────────────────────────────────────────
import os as _os
from pydantic_ai import Agent as _Agent


def _get_llm_agent() -> _Agent:
    """Pick the best available LLM provider, mirroring the main agent fallback order."""
    if _os.environ.get("DEEPSEEK_API_KEY"):
        return _Agent("deepseek:deepseek-chat")
    if _os.environ.get("GOOGLE_API_KEY") or _os.environ.get("GEMINI_API_KEY"):
        return _Agent("google:gemini-2.0-flash")
    if _os.environ.get("GROQ_API_KEY"):
        return _Agent("groq:llama-3.3-70b-versatile")
    raise RuntimeError("No LLM API key found (set DEEPSEEK_API_KEY, GOOGLE_API_KEY, or GROQ_API_KEY)")

async def do_update_shortlist(session, candidate_id: str, status: str) -> dict:
    """Status should be 'accepted', 'rejected', or 'held'."""
    for lst in session.shortlist.values():
        if candidate_id in lst:
            lst.remove(candidate_id)
    if status in session.shortlist:
        session.shortlist[status].append(candidate_id)
    return {"shortlist": session.shortlist}

async def do_update_working_spec(session, updates: dict) -> dict:
    session.working_spec.update(updates)
    return {"working_spec": session.working_spec}

async def do_rerank_pool(pool, session, query: str) -> dict:
    from pipeline.reranker import get_reranker
    from pipeline.search_result import SearchResult
    reranker = get_reranker()
    candidate_ids = [c["id"] for c in session.candidate_pool if c.get("id")]
    chunk_rows = await pool.fetch(
        """
        SELECT DISTINCT ON (candidate_id)
               candidate_id::text AS candidate_id,
               content
        FROM document_chunks
        WHERE candidate_id = ANY($1::uuid[])
        ORDER BY candidate_id, created_at
        """,
        candidate_ids,
    ) if candidate_ids else []
    chunk_by_candidate = {row["candidate_id"]: row["content"] for row in chunk_rows}
    results = []
    for c in session.candidate_pool:
        sr = SearchResult(candidate_id=c["id"])
        sr.best_chunk = chunk_by_candidate.get(c["id"], "")
        results.append(sr)
    
    reranked = await reranker.rerank(query, results)
    
    # Update pool with scores
    score_map = {r.candidate_id: r.rerank_score for r in reranked}
    for c in session.candidate_pool:
        c["rerank_score"] = score_map.get(c["id"])
        
    session.candidate_pool.sort(key=lambda x: x.get("rerank_score") or 0, reverse=True)
    return {"reranked": len(session.candidate_pool)}

async def do_analyze_jd(jd_text: str) -> dict:
    agent = _get_llm_agent()
    prompt = (
        "Extract structured requirements from this job description. "
        "Return ONLY a JSON object with keys: role (string), must_skills (list), "
        "nice_skills (list), location (string or null), min_years_exp (int or null).\n\n"
        f"{jd_text}"
    )
    res = await agent.run(prompt)
    try:
        raw = res.data.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"analysis": res.data}


async def do_draft_outreach(pool: asyncpg.Pool, candidate_id: str, role_context: str) -> dict:
    detail = await do_get_candidate_detail(pool, candidate_id)
    if "error" in detail:
        return detail
    agent = _get_llm_agent()
    prompt = (
        f"Draft a short, personalized recruiter outreach email (3–4 sentences) "
        f"to {detail['full_name']} for this role: {role_context}.\n"
        f"Candidate skills: {detail['skills']}\n"
        f"Resume snippet: {detail['best_chunk'][:400]}"
    )
    res = await agent.run(prompt)
    return {"draft": res.data, "candidate_name": detail["full_name"]}


async def do_generate_interview_questions(pool: asyncpg.Pool, candidate_id: str, role_context: str) -> dict:
    detail = await do_get_candidate_detail(pool, candidate_id)
    if "error" in detail:
        return detail
    agent = _get_llm_agent()
    prompt = (
        f"Generate 3 targeted technical interview questions for {detail['full_name']} "
        f"based on this role: {role_context}.\n"
        f"Their skills: {detail['skills']}\n"
        f"Resume: {detail['best_chunk'][:400]}\n"
        "Focus on verifying the most important claims and probing likely gaps."
    )
    res = await agent.run(prompt)
    return {"questions": res.data, "candidate_name": detail["full_name"]}


async def do_save_search(
    pool: asyncpg.Pool, recruiter_id: str,
    query: str, filters: dict, results: list,
) -> dict:
    await pool.execute(
        """INSERT INTO search_history (recruiter_id, query, filters_json, results_json)
           VALUES ($1, $2, $3::jsonb, $4::jsonb)""",
        recruiter_id, query, json.dumps(filters), json.dumps(results),
    )
    return {"status": "saved", "query": query}


async def do_compare_candidates(pool: asyncpg.Pool, id_a: str, id_b: str) -> dict:
    details = await do_get_candidate_details(pool, [id_a, id_b], limit=2)
    by_id = {d["id"]: d for d in details.get("candidates", [])}
    detail_a = by_id.get(str(id_a))
    detail_b = by_id.get(str(id_b))
    if not detail_a or not detail_b:
        return {"error": "One or both candidates not found"}
    agent = _get_llm_agent()
    prompt = (
        f"Compare these two candidates for a technical role.\n\n"
        f"Candidate A — {detail_a['full_name']} ({detail_a['years_exp']} yrs):\n"
        f"Skills: {detail_a['skills']}\n"
        f"Resume: {detail_a['best_chunk'][:400]}\n\n"
        f"Candidate B — {detail_b['full_name']} ({detail_b['years_exp']} yrs):\n"
        f"Skills: {detail_b['skills']}\n"
        f"Resume: {detail_b['best_chunk'][:400]}\n\n"
        "Write one short paragraph on their relative strengths, weaknesses, and which "
        "profile is stronger for a typical engineering role."
    )
    res = await agent.run(prompt)
    return {
        "candidate_a": detail_a,
        "candidate_b": detail_b,
        "comparison": res.data,
    }

async def do_export_shortlist(pool, session) -> dict:
    accepted = session.shortlist.get('accepted', [])
    if not accepted:
        return {'error': 'No candidates accepted yet.'}
    result = await do_get_candidate_details(pool, accepted, limit=50)
    details = result.get("candidates", [])
    return {'shortlist_details': details}
