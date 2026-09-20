"""Keyword retrieval via Postgres FTS or ParadeDB pg_search.

The legacy backend uses a generated tsvector + ts_rank_cd. That path is kept as
the default fallback because it works on plain Postgres/Supabase. The optional
ParadeDB backend uses pg_search's BM25 index and `pdb.score(...)`.

Builds a keyword query from spec.lexical_terms for maximum recall.
The candidate filter is inlined as a CTE so Postgres can plan a single
join rather than receiving a giant UUID array from the application.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import asyncpg

from pipeline import cache as _cache
from pipeline.db_timing import finalize_db_timing
from pipeline.spec import CanonicalSearchSpec

logger = logging.getLogger(__name__)

_FALLBACK_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "for", "find", "has", "have",
    "in", "is", "me", "of", "or", "show", "the", "to", "with", "who",
}

_FALLBACK_BROAD_SUPPORT_TERMS = {
    "candidate", "candidates", "experience", "experienced", "role", "roles",
    "system", "systems",
}

_FALLBACK_BROAD_ANCHOR_TERMS = _FALLBACK_BROAD_SUPPORT_TERMS | {
    "developer", "engineer", "manager", "operations", "staff",
}


def _safe_tsquery_term(term: str) -> str | None:
    cleaned = term.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9+#\-]*", cleaned):
        return None
    if cleaned in _FALLBACK_STOPWORDS:
        return None
    return cleaned


def _semantic_fallback_terms(text: str) -> list[str]:
    terms = [
        t for raw in re.findall(r"[a-z0-9][a-z0-9+#\-]*", text.lower())
        if (t := _safe_tsquery_term(raw))
    ]
    return list(dict.fromkeys(terms))


def _build_tsquery(terms: list[str], *, derived_from_semantic_query: bool) -> str:
    safe_terms = [t for term in terms if (t := _safe_tsquery_term(term))]
    if not safe_terms:
        return ""

    if not derived_from_semantic_query:
        return " | ".join(safe_terms[:8])

    # The fallback planner can leave lexical_terms empty for non-technical role
    # phrases. A plain OR over semantic tokens makes broad words like "manager"
    # match most of the corpus, so anchor on the first specific term and use the
    # rest as supporting recall terms.
    anchor = next(
        (t for t in safe_terms if t not in _FALLBACK_BROAD_ANCHOR_TERMS),
        safe_terms[0],
    )
    support = [
        t for t in safe_terms
        if t != anchor and t not in _FALLBACK_BROAD_SUPPORT_TERMS
    ][:6]
    if not support:
        return anchor
    return f"{anchor} & ({' | '.join(support)})"


def _build_paradedb_match_query(
    terms: list[str],
    *,
    derived_from_semantic_query: bool,
) -> str:
    """Build a pg_search match string.

    ParadeDB's match operators tokenize text with the index tokenizer, so this
    returns plain text rather than Postgres tsquery syntax. For semantic
    fallback terms, put the best anchor first; BM25 scoring then handles IDF and
    multi-term agreement without the old ts_rank_cd scan/sort tail.
    """
    safe_terms = [t for term in terms if (t := _safe_tsquery_term(term))]
    if not safe_terms:
        return ""
    if not derived_from_semantic_query:
        return " ".join(safe_terms[:8])

    anchor = next(
        (t for t in safe_terms if t not in _FALLBACK_BROAD_ANCHOR_TERMS),
        safe_terms[0],
    )
    support = [
        t for t in safe_terms
        if t != anchor and t not in _FALLBACK_BROAD_SUPPORT_TERMS
    ][:6]
    return " ".join([anchor, *support])


def _should_use_fts_first(spec: CanonicalSearchSpec) -> bool:
    """Use an FTS-first plan when hard filters are too broad to drive BM25.

    Candidate-first is good for narrow filters such as skill+city+years. With
    broad filters like country-only, Postgres can otherwise do thousands of
    per-candidate chunk index probes and apply the full-text predicate one row
    at a time.
    """
    must = spec.must
    return not (
        must.skills
        or must.city
        or must.min_years_exp is not None
        or must.max_years_exp is not None
        or must.min_salary is not None
        or must.max_salary is not None
    )


def _normalize_backend(backend: str | None) -> str:
    backend = (backend or "fts").strip().lower()
    return backend if backend in {"fts", "paradedb"} else "fts"


async def _fetch_rows(
    pool: asyncpg.Pool,
    sql: str,
    params: list[Any],
    *,
    statement_timeout_ms: int | None,
    timings: dict[str, Any] | None = None,
) -> list[asyncpg.Record]:
    timings = timings if timings is not None else {}
    if not hasattr(pool, "acquire"):
        fetch_started = time.perf_counter()
        rows = await pool.fetch(sql, *params)
        timings["fetch_roundtrip_ms"] = round((time.perf_counter() - fetch_started) * 1000, 2)
        timings["rows"] = len(rows)
        finalize_db_timing(timings)
        return rows

    acquire_started = time.perf_counter()
    async with pool.acquire() as conn:
        timings["pool_acquire_ms"] = round((time.perf_counter() - acquire_started) * 1000, 2)
        if statement_timeout_ms:
            timeout_ms = max(100, int(statement_timeout_ms))
            timings["statement_timeout_ms"] = timeout_ms
            async with conn.transaction():
                set_started = time.perf_counter()
                await conn.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
                timings["set_statement_timeout_ms"] = round((time.perf_counter() - set_started) * 1000, 2)
                fetch_started = time.perf_counter()
                rows = await conn.fetch(sql, *params)
                timings["fetch_roundtrip_ms"] = round((time.perf_counter() - fetch_started) * 1000, 2)
                timings["rows"] = len(rows)
                finalize_db_timing(timings)
                return rows
        fetch_started = time.perf_counter()
        rows = await conn.fetch(sql, *params)
        timings["fetch_roundtrip_ms"] = round((time.perf_counter() - fetch_started) * 1000, 2)
        timings["rows"] = len(rows)
        finalize_db_timing(timings)
        return rows


async def retrieve_bm25(
    spec: CanonicalSearchSpec,
    candidate_filter_sql: str,
    candidate_filter_params: list[Any],
    pool: asyncpg.Pool,
    top_k: int = 200,
    doc_type_filter: list[str] | None = None,
    include_content: bool = True,
    statement_timeout_ms: int | None = None,
    backend: str = "fts",
    overfetch_factor: int = 4,
    overfetch_min: int = 60,
    use_retrieval_cache: bool = False,
    timings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return up to top_k chunk rows ordered by keyword relevance descending."""
    total_started = time.perf_counter()
    timings = timings if timings is not None else {}
    terms = spec.lexical_terms
    derived_from_semantic_query = False
    if not terms:
        # fall back to semantic_query tokens
        terms = _semantic_fallback_terms(spec.semantic_query)
        derived_from_semantic_query = True
    if not terms:
        logger.debug("BM25 retrieval skipped: no lexical terms")
        timings.update({"skipped": True, "reason": "no_terms", "total_ms": 0.0})
        return []

    backend = _normalize_backend(backend)
    timings["backend"] = backend
    timings["derived_from_semantic_query"] = derived_from_semantic_query
    keyword_query = (
        _build_paradedb_match_query(
            terms,
            derived_from_semantic_query=derived_from_semantic_query,
        )
        if backend == "paradedb"
        else _build_tsquery(
            terms,
            derived_from_semantic_query=derived_from_semantic_query,
        )
    )
    if not keyword_query:
        logger.debug("BM25 retrieval skipped: no usable lexical terms")
        timings.update({"skipped": True, "reason": "empty_keyword_query", "total_ms": 0.0})
        return []

    top_k = max(1, int(top_k or 1))
    row_cache_key = None
    negative_cache_key = None
    if use_retrieval_cache and not include_content:
        cache_payload = {
            "terms": terms,
            "keyword_query": keyword_query,
            "derived_from_semantic_query": derived_from_semantic_query,
            "backend": backend,
            "filter_sql": candidate_filter_sql,
            "filter_params": candidate_filter_params,
            "doc_type_filter": doc_type_filter or [],
            "top_k": top_k,
            "statement_timeout_ms": statement_timeout_ms,
            "overfetch_factor": overfetch_factor,
            "overfetch_min": overfetch_min,
            "include_content": include_content,
        }
        row_cache_key = _cache.retrieval_key("bm25", cache_payload)
        negative_cache_key = _cache.bm25_negative_key(cache_payload)
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
        negative_started = time.perf_counter()
        cached_negative = await _cache.get(negative_cache_key)
        timings["negative_cache_lookup_ms"] = round((time.perf_counter() - negative_started) * 1000, 2)
        if cached_negative is not None:
            timings["negative_cache_hit"] = True
            timings["negative_cache_reason"] = dict(cached_negative).get("reason", "negative_cached")
            timings["rows"] = 0
            timings["total_ms"] = round((time.perf_counter() - total_started) * 1000, 2)
            finalize_db_timing(timings)
            return []
    elif use_retrieval_cache and include_content:
        timings["rowset_cache_skipped_reason"] = "include_content_true"
    timings["rowset_cache_hit"] = False
    timings["negative_cache_hit"] = False
    params: list[Any] = list(candidate_filter_params)
    query_idx = len(params) + 1
    params.append(keyword_query)
    idx = query_idx + 1

    doc_clause = ""
    if doc_type_filter:
        doc_clause = f"AND cd.doc_type = ANY(${idx}::text[])"
        params.append(doc_type_filter)
        idx += 1

    if backend == "paradedb":
        overfetch = max(
            top_k,
            top_k * max(1, int(overfetch_factor or 1)),
            max(1, int(overfetch_min or 1)),
        )
        if _should_use_fts_first(spec):
            content_select = "dc.content" if include_content else "''::text AS content"
            sql = f"""
        WITH bm25_hits AS MATERIALIZED (
            SELECT
                dc.id,
                dc.candidate_id,
                dc.document_id,
                {content_select},
                pdb.score(dc.id)::float AS bm25_score
            FROM document_chunks dc
            WHERE dc.content ||| ${query_idx}
            ORDER BY pdb.score(dc.id) DESC, dc.candidate_id, dc.id
            LIMIT {overfetch}
        ),
        filtered_candidates AS (
            SELECT id FROM candidates WHERE {candidate_filter_sql}
        )
        SELECT
            h.id::text            AS chunk_id,
            h.candidate_id::text  AS candidate_id,
            h.content             AS content,
            h.document_id::text   AS document_id,
            cd.doc_type           AS doc_type,
            cd.title              AS document_title,
            NULL::float           AS distance,
            h.bm25_score          AS bm25_score,
            NULL::float           AS skill_score
        FROM bm25_hits h
        JOIN filtered_candidates fc ON fc.id = h.candidate_id
        JOIN candidate_documents cd ON cd.id = h.document_id
        WHERE TRUE
          {doc_clause}
        ORDER BY h.bm25_score DESC, h.candidate_id, h.id
        LIMIT {top_k}
    """
        else:
            content_expr = "dc.content" if include_content else "''::text"
            sql = f"""
        WITH filtered_candidates AS (
            SELECT id FROM candidates WHERE {candidate_filter_sql}
        )
        SELECT
            dc.id::text            AS chunk_id,
            dc.candidate_id::text  AS candidate_id,
            {content_expr}         AS content,
            dc.document_id::text   AS document_id,
            cd.doc_type            AS doc_type,
            cd.title               AS document_title,
            NULL::float            AS distance,
            pdb.score(dc.id)::float AS bm25_score,
            NULL::float            AS skill_score
        FROM document_chunks dc
        JOIN filtered_candidates fc ON fc.id = dc.candidate_id
        JOIN candidate_documents cd ON cd.id = dc.document_id
        WHERE dc.content ||| ${query_idx}
          {doc_clause}
        ORDER BY pdb.score(dc.id) DESC, dc.candidate_id, dc.id
        LIMIT {top_k}
    """
    elif _should_use_fts_first(spec):
        content_select = "content" if include_content else "''::text AS content"
        sql = f"""
        WITH matched_chunks AS MATERIALIZED (
            SELECT id, candidate_id, document_id, content_tsv, {content_select}
            FROM document_chunks
            WHERE content_tsv @@ to_tsquery('english', ${query_idx})
        ),
        filtered_candidates AS (
            SELECT id FROM candidates WHERE {candidate_filter_sql}
        )
        SELECT
            dc.id::text            AS chunk_id,
            dc.candidate_id::text  AS candidate_id,
            dc.content             AS content,
            dc.document_id::text   AS document_id,
            cd.doc_type            AS doc_type,
            cd.title               AS document_title,
            NULL::float            AS distance,
            ts_rank_cd(dc.content_tsv, to_tsquery('english', ${query_idx}))  AS bm25_score,
            NULL::float            AS skill_score
        FROM matched_chunks dc
        JOIN filtered_candidates fc ON fc.id = dc.candidate_id
        JOIN candidate_documents cd ON cd.id = dc.document_id
        WHERE TRUE
          {doc_clause}
        ORDER BY ts_rank_cd(dc.content_tsv, to_tsquery('english', ${query_idx})) DESC,
                 dc.candidate_id,
                 dc.id
        LIMIT {top_k}
    """
    else:
        content_expr = "dc.content" if include_content else "''::text"
        sql = f"""
        WITH filtered_candidates AS (
            SELECT id FROM candidates WHERE {candidate_filter_sql}
        )
        SELECT
            dc.id::text            AS chunk_id,
            dc.candidate_id::text  AS candidate_id,
            {content_expr}         AS content,
            dc.document_id::text   AS document_id,
            cd.doc_type            AS doc_type,
            cd.title               AS document_title,
            NULL::float            AS distance,
            ts_rank_cd(dc.content_tsv, to_tsquery('english', ${query_idx}))  AS bm25_score,
            NULL::float            AS skill_score
        FROM document_chunks dc
        JOIN filtered_candidates fc ON fc.id = dc.candidate_id
        JOIN candidate_documents cd ON cd.id = dc.document_id
        WHERE dc.content_tsv @@ to_tsquery('english', ${query_idx})
          {doc_clause}
        ORDER BY ts_rank_cd(dc.content_tsv, to_tsquery('english', ${query_idx})) DESC,
                 dc.candidate_id,
                 dc.id
        LIMIT {top_k}
    """

    try:
        rows = await _fetch_rows(
            pool,
            sql,
            params,
            statement_timeout_ms=statement_timeout_ms,
            timings=timings,
        )
        timings["total_ms"] = round((time.perf_counter() - total_started) * 1000, 2)
        finalize_db_timing(timings)
        out = [dict(r) for r in rows]
        if row_cache_key is not None:
            await _cache.set(row_cache_key, out)
        return out
    except asyncpg.exceptions.QueryCanceledError:
        logger.warning("BM25 retrieval timed out after %sms", statement_timeout_ms)
        timings["timed_out"] = True
        if negative_cache_key is not None:
            await _cache.set(negative_cache_key, {"reason": "timeout"})
        timings["total_ms"] = round((time.perf_counter() - total_started) * 1000, 2)
        finalize_db_timing(timings)
        raise
    except Exception as exc:
        logger.error("BM25 retrieval error: %s", exc)
        timings["error"] = str(exc)
        timings["total_ms"] = round((time.perf_counter() - total_started) * 1000, 2)
        finalize_db_timing(timings)
        return []
