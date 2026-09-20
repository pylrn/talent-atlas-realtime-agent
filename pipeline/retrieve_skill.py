"""Exact-skill structured retrieval.

Searches the `candidates.skills` array directly — highest precision for
technical queries where exact token matches matter most.
Fetches at most one synthetic "row" per candidate (from their best chunk).
The candidate filter is inlined as a CTE.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import asyncpg

from pipeline import cache as _cache
from pipeline.db_timing import finalize_db_timing
from pipeline.spec import CanonicalSearchSpec

logger = logging.getLogger(__name__)


async def retrieve_skill(
    spec: CanonicalSearchSpec,
    candidate_filter_sql: str,
    candidate_filter_params: list[Any],
    pool: asyncpg.Pool,
    top_k: int = 100,
    include_content: bool = True,
    use_retrieval_cache: bool = False,
    timings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return candidates whose skills array overlaps must+should skills."""
    total_started = time.perf_counter()
    timings = timings if timings is not None else {}
    all_skills = list({*spec.must.skills, *spec.should.skills})
    if not all_skills:
        logger.debug("Skill retrieval skipped: no skills in spec")
        timings.update({"skipped": True, "reason": "no_skills", "total_ms": 0.0})
        return []

    # Score = (2 × must_matches) + (1 × should_matches), with optional per-skill weights.
    must_skills   = spec.must.skills or []
    should_skills = spec.should.skills or []
    weights = getattr(spec, "skill_weights", None) or {}

    def _weights_for(skills: list[str]) -> list[float]:
        # Default weight is 1.0 so unweighted callers behave exactly as before.
        return [float(weights.get(s.lower(), 1.0)) for s in skills]

    must_weights   = _weights_for(must_skills)
    should_weights = _weights_for(should_skills)
    top_k = max(1, int(top_k or 1))

    row_cache_key = None
    if use_retrieval_cache and not include_content:
        row_cache_key = _cache.retrieval_key("skill", {
            "must_skills": must_skills,
            "should_skills": should_skills,
            "weights": weights,
            "filter_sql": candidate_filter_sql,
            "filter_params": candidate_filter_params,
            "top_k": top_k,
        })
        timings["cache_namespace"] = _cache.namespace_for_key(row_cache_key)
        timings["cache_key_hash"] = _cache.hash_payload(row_cache_key)
        cache_started = time.perf_counter()
        cached_rows = await _cache.get(row_cache_key)
        timings["rowset_cache_lookup_ms"] = round((time.perf_counter() - cache_started) * 1000, 2)
        if cached_rows is not None:
            timings["rowset_cache_hit"] = True
            timings["rows"] = len(cached_rows)
            timings["total_ms"] = round((time.perf_counter() - total_started) * 1000, 2)
            finalize_db_timing(timings)
            return cached_rows
    elif use_retrieval_cache and include_content:
        timings["rowset_cache_skipped_reason"] = "include_content_true"
    timings["rowset_cache_hit"] = False

    params: list[Any] = list(candidate_filter_params)
    must_idx   = len(params) + 1
    params.append(must_skills or ["__none__"])
    should_idx = len(params) + 1
    params.append(should_skills or ["__none__"])
    must_w_idx = len(params) + 1
    params.append(must_weights or [0.0])
    should_w_idx = len(params) + 1
    params.append(should_weights or [0.0])

    content_expr = "dc.content" if include_content else "''::text"
    lateral_content_column = "content," if include_content else ""

    sql = f"""
        WITH filtered_candidates AS (
            SELECT id, full_name, skills
            FROM candidates
            WHERE {candidate_filter_sql}
        ),
        must_kv AS (
            SELECT skill, w
            FROM unnest(${must_idx}::text[], ${must_w_idx}::float8[]) AS t(skill, w)
        ),
        should_kv AS (
            SELECT skill, w
            FROM unnest(${should_idx}::text[], ${should_w_idx}::float8[]) AS t(skill, w)
        ),
        scored AS (
            SELECT
                c.id         AS candidate_uuid,
                c.id::text   AS candidate_id,
                c.full_name,
                (
                    COALESCE((
                        SELECT SUM(w * 2.0)
                        FROM must_kv
                        WHERE skill = ANY(c.skills)
                    ), 0)
                    +
                    COALESCE((
                        SELECT SUM(w)
                        FROM should_kv
                        WHERE skill = ANY(c.skills)
                    ), 0)
                )::float AS skill_score
            FROM filtered_candidates c
            WHERE (c.skills && ${must_idx}::text[] OR c.skills && ${should_idx}::text[])
            ORDER BY skill_score DESC, c.id
            LIMIT {top_k}
        )
        SELECT
            dc.id::text          AS chunk_id,
            s.candidate_id       AS candidate_id,
            {content_expr}       AS content,
            dc.document_id::text AS document_id,
            cd.doc_type          AS doc_type,
            cd.title             AS document_title,
            NULL::float AS distance,
            NULL::float AS bm25_score,
            s.skill_score        AS skill_score
        FROM scored s
        JOIN LATERAL (
            SELECT id, {lateral_content_column} document_id
            FROM document_chunks dc
            WHERE dc.candidate_id = s.candidate_uuid
            ORDER BY dc.id
            LIMIT 1
        ) dc ON TRUE
        JOIN candidate_documents cd ON cd.id = dc.document_id
        ORDER BY s.skill_score DESC, s.candidate_uuid, dc.id
    """

    try:
        acquire_started = time.perf_counter()
        async with pool.acquire() as conn:
            timings["pool_acquire_ms"] = round((time.perf_counter() - acquire_started) * 1000, 2)
            fetch_started = time.perf_counter()
            rows = await conn.fetch(sql, *params)
            timings["fetch_roundtrip_ms"] = round((time.perf_counter() - fetch_started) * 1000, 2)
        timings["rows"] = len(rows)
        timings["total_ms"] = round((time.perf_counter() - total_started) * 1000, 2)
        finalize_db_timing(timings)
        out = [dict(r) for r in rows]
        if row_cache_key is not None:
            await _cache.set(row_cache_key, out)
        return out
    except Exception as exc:
        logger.error("Skill retrieval error: %s", exc)
        timings["error"] = str(exc)
        timings["total_ms"] = round((time.perf_counter() - total_started) * 1000, 2)
        finalize_db_timing(timings)
        return []
