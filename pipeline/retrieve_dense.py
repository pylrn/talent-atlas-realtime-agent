"""Dense ANN retrieval via pgvector HNSW.

Embeds the spec's hyde_profile (JD mode) or semantic_query (query mode),
then fetches the nearest chunks restricted to candidates that match the
filter clause. The filter is inlined as a CTE so the candidate set never
leaves Postgres — at large corpora this avoids shipping hundreds of
thousands of UUIDs to the application.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import asyncpg

from pipeline import settings
from pipeline import cache as _cache
from pipeline.db_timing import finalize_db_timing
from pipeline.embedder import EmbeddingProvider
from pipeline.spec import CanonicalSearchSpec

logger = logging.getLogger(__name__)


async def retrieve_dense(
    spec: CanonicalSearchSpec,
    candidate_filter_sql: str,
    candidate_filter_params: list[Any],
    pool: asyncpg.Pool,
    embedder: EmbeddingProvider,
    top_k: int = 200,
    doc_type_filter: list[str] | None = None,
    include_content: bool = True,
    use_retrieval_cache: bool = False,
    timings: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return up to top_k chunk rows ordered by cosine distance."""
    total_started = time.perf_counter()
    timings = timings if timings is not None else {}
    embed_text = spec.embed_text()
    if not embed_text:
        logger.debug("Dense retrieval skipped: no embed text")
        timings.update({"skipped": True, "reason": "no_embed_text", "total_ms": 0.0})
        return []

    # Embedding cache
    cache_started = time.perf_counter()
    ek = _cache.embed_key(
        embed_text,
        provider=embedder.name,
        dimensions=embedder.dimensions,
    )
    timings["embedding_cache_namespace"] = _cache.namespace_for_key(ek)
    timings["embedding_cache_key_hash"] = _cache.hash_payload(ek)
    embedding: list[float] | None = await _cache.get(ek)
    timings["embedding_cache_lookup_ms"] = round((time.perf_counter() - cache_started) * 1000, 2)
    produced_embedding = False
    if embedding is None:
        async def _produce_embedding() -> list[float]:
            nonlocal produced_embedding
            produced_embedding = True
            embed_started = time.perf_counter()
            vector = (await embedder.embed([embed_text]))[0]
            timings["embedding_ms"] = round((time.perf_counter() - embed_started) * 1000, 2)
            return vector

        embedding = await _cache.get_or_set(ek, _produce_embedding)
    else:
        timings["embedding_ms"] = 0.0
    timings["embedding_cache_hit"] = not produced_embedding
    if not produced_embedding:
        timings.setdefault("embedding_ms", 0.0)
        from pipeline import metrics as _metrics
        _metrics.incr("embedding_cache_hits")

    embedding_str = str(embedding)

    top_k = max(1, int(top_k or 1))
    row_cache_key = None
    if use_retrieval_cache and not include_content:
        row_cache_key = _cache.retrieval_key("dense", {
            "embed_key": ek,
            "filter_sql": candidate_filter_sql,
            "filter_params": candidate_filter_params,
            "doc_type_filter": doc_type_filter or [],
            "top_k": top_k,
            "hnsw_ef_search": settings.hnsw_ef_search,
        })
        timings["cache_namespace"] = _cache.namespace_for_key(row_cache_key)
        timings["cache_key_hash"] = _cache.hash_payload(row_cache_key)
        row_cache_started = time.perf_counter()
        cached_rows = await _cache.get(row_cache_key)
        timings["rowset_cache_lookup_ms"] = round((time.perf_counter() - row_cache_started) * 1000, 2)
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
    embedding_idx = len(params) + 1
    params.append(embedding_str)
    idx = embedding_idx + 1

    doc_clause = ""
    if doc_type_filter:
        doc_clause = f"AND cd.doc_type = ANY(${idx}::text[])"
        params.append(doc_type_filter)
        idx += 1

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
            dc.embedding <=> ${embedding_idx}::vector  AS distance,
            NULL::float            AS bm25_score,
            NULL::float            AS skill_score
        FROM document_chunks dc
        JOIN filtered_candidates fc ON fc.id = dc.candidate_id
        JOIN candidate_documents cd ON cd.id = dc.document_id
        WHERE TRUE
          {doc_clause}
        ORDER BY dc.embedding <=> ${embedding_idx}::vector, dc.candidate_id, dc.id
        LIMIT {top_k}
    """

    try:
        connection_ef_search = max(int(settings.hnsw_ef_search), 60)
        ef_search = max(connection_ef_search, int(top_k or 0), 1)
        timings["hnsw_ef_search"] = ef_search
        timings["connection_hnsw_ef_search"] = connection_ef_search
        acquire_started = time.perf_counter()
        async with pool.acquire() as conn:
            timings["pool_acquire_ms"] = round((time.perf_counter() - acquire_started) * 1000, 2)
            if ef_search > connection_ef_search:
                async with conn.transaction():
                    set_started = time.perf_counter()
                    await conn.execute(f"SET LOCAL hnsw.ef_search = {ef_search}")
                    timings["set_local_ms"] = round((time.perf_counter() - set_started) * 1000, 2)
                    fetch_started = time.perf_counter()
                    rows = await conn.fetch(sql, *params)
                    timings["fetch_roundtrip_ms"] = round((time.perf_counter() - fetch_started) * 1000, 2)
            else:
                timings["set_local_ms"] = 0.0
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
        logger.error("Dense retrieval error: %s", exc)
        timings["error"] = str(exc)
        timings["total_ms"] = round((time.perf_counter() - total_started) * 1000, 2)
        finalize_db_timing(timings)
        return []
