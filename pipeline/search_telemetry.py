"""Readable Langfuse payload builders for the search pipeline.

The goal is to make traces useful without dumping full resume text into
Langfuse. These helpers keep every span payload JSON-shaped, compact, and
stable enough to compare across benchmark runs.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Iterable

from pipeline import settings


def _get(row: Any, key: str, default: Any = None) -> Any:
    if hasattr(row, "get"):
        return row.get(key, default)
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def json_safe(value: Any, *, max_items: int = 50) -> Any:
    """Return a JSON-friendly, compact representation for trace payloads."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if dataclasses.is_dataclass(value):
        return json_safe(dataclasses.asdict(value), max_items=max_items)
    if isinstance(value, dict):
        return {
            str(k): json_safe(v, max_items=max_items)
            for k, v in list(value.items())[:max_items]
        }
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v, max_items=max_items) for v in list(value)[:max_items]]
    return str(value)


def spec_payload(spec: Any) -> dict[str, Any]:
    """Planner/spec fields that explain why retrieval behaves a certain way."""
    return {
        "input_type": getattr(spec, "input_type", None),
        "intent": getattr(spec, "intent", None),
        "semantic_query": getattr(spec, "semantic_query", "") or "",
        "lexical_terms": list(getattr(spec, "lexical_terms", []) or []),
        "search_targets": list(getattr(spec, "search_targets", []) or []),
        "must": json_safe(getattr(spec, "must", None)),
        "should": json_safe(getattr(spec, "should", None)),
        "must_not": json_safe(getattr(spec, "must_not", None)),
        "confidence": round(float(getattr(spec, "confidence", 0.0) or 0.0), 3),
        "used_fallback": bool(getattr(spec, "used_fallback", False)),
        "dropped_items": json_safe(getattr(spec, "dropped_items", []) or []),
    }


def filter_payload(
    filter_sql: str,
    filter_params: list[Any],
    doc_types: list[str],
) -> dict[str, Any]:
    """The hard-filter contract sent to retrieval SQL."""
    return {
        "candidate_where_sql": filter_sql,
        "candidate_params": json_safe(filter_params),
        "doc_type_filter": list(doc_types or []),
        "param_count": len(filter_params or []),
    }


def retrieval_config_payload(
    cfg: dict[str, Any],
    *,
    top_k: int,
    search_pool_mode: str,
) -> dict[str, Any]:
    """Config knobs that materially change retrieval latency or recall."""
    keys = (
        "use_dense",
        "use_bm25",
        "use_skill_exact",
        "use_rrf",
        "defer_chunk_content",
        "dense_top_k",
        "bm25_top_k",
        "skill_top_k",
        "chunk_hydration_top_k",
        "candidate_enrichment_top_k",
        "visible_enrichment_top_k",
        "rrf_k",
        "keyword_policy",
        "keyword_timeout_ms",
        "keyword_narrow_count",
        "bm25_backend",
        "bm25_overfetch_factor",
        "bm25_overfetch_min",
        "use_cache",
        "use_retrieval_cache",
    )
    payload = {key: json_safe(cfg.get(key)) for key in keys if key in cfg}
    payload["final_top_k"] = top_k
    payload["search_pool"] = search_pool_mode
    payload["hnsw_ef_search_min"] = max(int(settings.hnsw_ef_search), 60)
    return payload


def ranking_config_payload(cfg: dict[str, Any], *, top_k: int) -> dict[str, Any]:
    keys = (
        "use_cross_encoder",
        "cross_encoder_top_k",
        "use_feature_ranker",
        "use_mmr",
        "mmr_lambda",
    )
    payload = {key: json_safe(cfg.get(key)) for key in keys if key in cfg}
    payload["final_top_k"] = top_k
    return payload


def row_preview(
    rows: Iterable[dict[str, Any]],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Compact retrieval-row preview. Never includes chunk content."""
    preview: list[dict[str, Any]] = []
    for row in list(rows or [])[:limit]:
        scores: dict[str, float] = {}
        for key in (
            "distance",
            "bm25_score",
            "skill_score",
            "rrf_score",
            "semantic_score",
        ):
            value = _get(row, key)
            if value is not None:
                try:
                    scores[key] = round(float(value), 4)
                except (TypeError, ValueError):
                    pass
        preview.append({
            "candidate_id": _get(row, "candidate_id"),
            "chunk_id": _get(row, "chunk_id"),
            "doc_type": _get(row, "doc_type"),
            "retrieval_path": _get(row, "retrieval_path"),
            "scores": scores,
        })
    return preview


def result_preview(results: Iterable[Any], *, limit: int = 5) -> list[dict[str, Any]]:
    """Compact final-candidate preview. Keeps IDs and scores, not resume text."""
    preview: list[dict[str, Any]] = []
    for item in list(results or [])[:limit]:
        score_fields = (
            "feature_score",
            "rerank_score",
            "rrf_score",
            "fused_rrf_score",
            "similarity_score",
            "best_chunk_distance",
        )
        scores: dict[str, float] = {}
        for key in score_fields:
            value = getattr(item, key, None)
            if value is not None:
                try:
                    numeric = float(value)
                    if key != "best_chunk_distance" and abs(numeric) < 1e-12:
                        continue
                    if key == "best_chunk_distance" and abs(numeric - 1.0) < 1e-12:
                        continue
                    scores[key] = round(numeric, 4)
                except (TypeError, ValueError):
                    pass
        preview.append({
            "candidate_id": getattr(item, "candidate_id", None),
            "match_tier": getattr(item, "match_tier", None),
            "retrieval_paths": list(getattr(item, "retrieval_paths", []) or []),
            "scores": scores,
        })
    return preview


def db_timing_payload(timing: dict[str, Any] | None) -> dict[str, Any]:
    """Readable DB timing breakdown for Langfuse span outputs.

    Client-side code can reliably measure connection-pool wait and asyncpg call
    round-trip. The round-trip includes Postgres execution, network travel, and
    row decoding; use Postgres-side tooling to split those further.
    """
    timing = dict(timing or {})
    payload: dict[str, Any] = {}
    keys = (
        "total_ms",
        "pool_wait_ms",
        "pool_acquire_ms",
        "db_roundtrip_ms",
        "fetch_roundtrip_ms",
        "app_overhead_ms",
        "embedding_ms",
        "set_local_ms",
        "set_statement_timeout_ms",
        "rows",
        "requested_chunk_ids",
        "skipped",
        "reason",
        "timed_out",
        "error",
        "negative_cache_hit",
        "negative_cache_lookup_ms",
        "negative_cache_reason",
        "tail_risk",
        "skill_recency_cache_hit",
        "skill_recency_cache_lookup_ms",
        "skill_recency_cache_namespace",
        "skill_recency_fetch_roundtrip_ms",
        "skill_recency_error",
        "timing_version",
    )
    for key in keys:
        if key in timing:
            payload[key] = json_safe(timing[key])

    components = {
        "pool_wait": _numeric(payload.get("pool_wait_ms", payload.get("pool_acquire_ms"))),
        "db_roundtrip": _numeric(payload.get("db_roundtrip_ms", payload.get("fetch_roundtrip_ms"))),
        "app_overhead": _numeric(payload.get("app_overhead_ms")),
        "embedding": _numeric(payload.get("embedding_ms")),
    }
    non_null = {k: v for k, v in components.items() if v is not None}
    if non_null:
        payload["dominant_component"] = max(non_null, key=non_null.get)
        payload["components_ms"] = {k: round(v, 2) for k, v in non_null.items()}
    payload["roundtrip_note"] = (
        "db_roundtrip_ms is the asyncpg fetch duration after pool acquire; "
        "it includes Postgres execution, network, and row decoding."
    )
    return payload


def cache_timing_payload(timing: dict[str, Any] | None) -> dict[str, Any]:
    """Readable cache summary for a single pipeline branch/span."""
    timing = dict(timing or {})
    payload: dict[str, Any] = {}
    bool_keys = (
        "rowset_cache_hit",
        "embedding_cache_hit",
        "cache_hit",
        "all_cache_hit",
    )
    number_keys = (
        "cache_hits",
        "cache_misses",
        "rowset_cache_lookup_ms",
        "embedding_cache_lookup_ms",
    )
    for key in bool_keys:
        if key in timing:
            payload[key] = bool(timing[key])
    for key in number_keys:
        if key in timing:
            payload[key] = json_safe(timing[key])
    if "cache_namespace" in timing:
        payload["namespace"] = str(timing["cache_namespace"])
    elif "embedding_cache_namespace" in timing:
        payload["namespace"] = str(timing["embedding_cache_namespace"])
    if "cache_key_hash" in timing:
        payload["key_hash"] = str(timing["cache_key_hash"])
    elif "embedding_cache_key_hash" in timing:
        payload["key_hash"] = str(timing["embedding_cache_key_hash"])
    hit = (
        payload.get("rowset_cache_hit")
        or payload.get("embedding_cache_hit")
        or payload.get("cache_hit")
        or payload.get("all_cache_hit")
        or bool(payload.get("cache_hits"))
    )
    if payload:
        payload["summary"] = "hit" if hit else "miss"
    return payload


def retrieval_timing_summary(
    branch_timings_ms: dict[str, Any] | None,
    db_timings: dict[str, Any] | None,
    row_counts: dict[str, Any] | None,
) -> dict[str, Any]:
    """Make retrieval latency understandable at a glance in Langfuse."""
    branch_timings_ms = dict(branch_timings_ms or {})
    db_timings = dict(db_timings or {})
    row_counts = dict(row_counts or {})

    branches: dict[str, dict[str, Any]] = {}
    mapping = {
        "count": ("count_ms", "filtered_candidates", "parallel"),
        "semantic": ("dense_ms", "dense_rows", "parallel"),
        "keyword": ("keyword_ms", "keyword_rows", "parallel"),
        "skill_match": ("skill_ms", "skill_rows", "parallel"),
        "fuse": ("fusion_ms", "fused_rows", "sequential"),
        "hydrate_initial": ("hydrate_initial_ms", None, "sequential"),
        "hydrate_expanded": ("hydrate_expanded_ms", None, "sequential"),
        "enrichment": ("enrichment_ms", "enriched_candidates", "sequential"),
    }
    for name, (timing_key, row_key, phase) in mapping.items():
        has_elapsed = timing_key in branch_timings_ms
        has_db = name in db_timings or name.replace("semantic", "dense") in db_timings
        if not has_elapsed and not has_db:
            continue
        db_key = name.replace("semantic", "dense")
        db_payload = db_timing_payload(db_timings.get(db_key) or db_timings.get(name) or {})
        raw_timing = db_timings.get(db_key) or db_timings.get(name) or {}
        branch: dict[str, Any] = {
            "elapsed_ms": _numeric(branch_timings_ms.get(timing_key)) or 0.0,
            "phase": phase,
            "runs_parallel_with": (
                ["count", "semantic", "keyword", "skill_match"]
                if phase == "parallel"
                else []
            ),
            "db": db_payload,
            "cache": cache_timing_payload(raw_timing),
        }
        if row_key and row_key in row_counts:
            branch["rows"] = row_counts[row_key]
        branches[name] = branch

    def _branch_value(branch: dict[str, Any], *keys: str) -> float:
        for key in keys:
            value = _numeric((branch.get("db") or {}).get(key))
            if value is not None:
                return value
        return 0.0

    slowest_branch = None
    if branches:
        slowest_branch = max(branches, key=lambda name: _numeric(branches[name].get("elapsed_ms")) or 0.0)
    largest_pool_wait = None
    if branches:
        largest_pool_wait = max(branches, key=lambda name: _branch_value(branches[name], "pool_wait_ms", "pool_acquire_ms"))
    largest_db_roundtrip = None
    if branches:
        largest_db_roundtrip = max(branches, key=lambda name: _branch_value(branches[name], "db_roundtrip_ms", "fetch_roundtrip_ms"))

    summary = {
        "parallel_wall_ms": _numeric(branch_timings_ms.get("total_ms")) or 0.0,
        "slowest_branch": slowest_branch,
        "slowest_branch_elapsed_ms": (
            _numeric(branches.get(slowest_branch, {}).get("elapsed_ms")) if slowest_branch else None
        ),
        "largest_pool_wait_branch": largest_pool_wait,
        "largest_pool_wait_ms": (
            _branch_value(branches.get(largest_pool_wait, {}), "pool_wait_ms", "pool_acquire_ms")
            if largest_pool_wait else 0.0
        ),
        "largest_db_roundtrip_branch": largest_db_roundtrip,
        "largest_db_roundtrip_ms": (
            _branch_value(branches.get(largest_db_roundtrip, {}), "db_roundtrip_ms", "fetch_roundtrip_ms")
            if largest_db_roundtrip else 0.0
        ),
        "total_pool_wait_ms": round(
            sum(_branch_value(branch, "pool_wait_ms", "pool_acquire_ms") for branch in branches.values()),
            2,
        ),
        "total_db_roundtrip_ms": round(
            sum(_branch_value(branch, "db_roundtrip_ms", "fetch_roundtrip_ms") for branch in branches.values()),
            2,
        ),
        "total_app_overhead_ms": round(
            sum(_branch_value(branch, "app_overhead_ms") for branch in branches.values()),
            2,
        ),
        "branches": branches,
        "how_to_read": (
            "count, semantic, keyword, and skill_match run in parallel; wall time is "
            "not their sum. fuse, hydrate, group, enrich, and rank run after retrieval."
        ),
    }
    return summary


def ms_since(start: float, perf_counter) -> float:
    return round((perf_counter() - start) * 1000, 2)


def _numeric(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    try:
        if value is not None:
            return round(float(value), 2)
    except (TypeError, ValueError):
        pass
    return None
