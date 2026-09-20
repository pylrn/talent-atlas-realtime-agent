"""Main search orchestrator — the full 20-step pipeline.

Every step is individually toggleable via the cfg dict (see config.py).
Import surface kept stable so api/main.py needs minimal changes.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import logging
import re
import time as _time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import asyncpg

from pipeline import settings
from pipeline import metrics as _metrics
from pipeline.config import SEARCH_CONFIG
from pipeline.db_timing import finalize_db_timing
from pipeline.modes import get_config
from pipeline.embedder import EmbeddingProvider, get_embedder
from pipeline.spec import CanonicalSearchSpec
from pipeline.search_result import SearchResult

# ── Stage imports ─────────────────────────────────────────────────────────────
from pipeline.router import detect_route
from pipeline.sanitize import sanitize_input
from pipeline import cache as _cache
from pipeline.planner import plan as _plan
from pipeline.planner_fallback import fallback_plan
from pipeline.intent_router import should_short_circuit
from pipeline.sql_filter import build_filter_sql, build_must_not_sql, build_candidate_filter
from pipeline.retrieve_dense import retrieve_dense
from pipeline.retrieve_bm25 import retrieve_bm25
from pipeline.retrieve_skill import retrieve_skill
from pipeline.keyword_policy import KeywordPolicyDecision, decide_keyword_policy
from pipeline.fusion import rrf_fuse
from pipeline.group import group_by_candidate
from pipeline.reranker import get_reranker, Reranker
from pipeline.feature_ranker import score_results
from pipeline.diversity import mmr_select, mmr_select_async
from pipeline.explanation import build_explanation
from pipeline.constants import SEARCH_TARGET_DOC_TYPES
from pipeline.observability import is_enabled as _obs_enabled, _get_client as _obs_client, update_current_span as _obs_update_current_span
from pipeline.search_telemetry import (
    db_timing_payload,
    filter_payload,
    ms_since,
    ranking_config_payload,
    result_preview,
    retrieval_config_payload,
    retrieval_timing_summary,
    row_preview,
    spec_payload,
)

class _NullContext:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def update(self, **kw): pass

def _obs_span(name: str, **kwargs):
    if _obs_enabled() and _obs_client() is not None:
        try:
            return _obs_client().start_as_current_observation(as_type="span", name=name, **kwargs)
        except Exception as exc:
            logger.debug("Langfuse _obs_span failed (%s): %s", name, exc)
    return _NullContext()

logger = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9.+#-]*")
_QUERY_STOPWORDS = {
    "a", "about", "also", "an", "and", "are", "as", "at", "be", "been",
    "candidate", "candidates", "engineer", "experience", "experienced",
    "find", "for", "has", "have", "having", "in", "is", "me", "need",
    "of", "on", "or", "person", "the", "to", "with",
}


# ── Kept for backward-compat with api/main.py and query_planner.py ───────────
# The old SearchFilters had many more fields than MustFilters; keep the full
# definition here so legacy code (query_planner, benchmark scripts) still works.
from dataclasses import field as _field
from typing import Literal as _Literal, List as _List


@dataclass
class SearchFilters:
    location:        Optional[str]                        = None
    country:         Optional[str]                        = None
    city:            Optional[str]                        = None
    min_age:         Optional[int]                        = None
    max_age:         Optional[int]                        = None
    skills:          Optional[list[str]]                  = None
    interests:       Optional[list[str]]                  = None
    min_years_exp:   Optional[int]                        = None
    max_years_exp:   Optional[int]                        = None
    min_salary:      Optional[int]                        = None
    max_salary:      Optional[int]                        = None
    status:          list[str]                            = _field(default_factory=lambda: ["active"])
    doc_types:       Optional[list[str]]                  = None
    skills_match:    _Literal["and", "or"]                = "or"
    interests_match: _Literal["and", "or"]                = "or"


@dataclass
class SearchResponse:
    results:             list[SearchResult]
    spec:                Optional[CanonicalSearchSpec] = None
    clarify:             Optional[str]                 = None
    relaxations_applied: list[dict]                    = field(default_factory=list)
    total_candidates_scanned: int                      = 0
    search_id:           str                           = field(default_factory=lambda: str(uuid.uuid4()))
    phase_timings:       dict[str, float]              = field(default_factory=dict)
    retrieval_policy:    dict[str, Any]                = field(default_factory=dict)
    spec_dict:           Optional[dict]                = None
    personalization_applied: bool                      = False
    candidate_ids:       list[str]                     = field(default_factory=list)
    deferred_candidate_ids: list[str]                  = field(default_factory=list)


class HybridSearchEngine:
    """Full pipeline entry point.  Backward-compatible with the old API."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        embedder: Optional[EmbeddingProvider] = None,
        reranker: Optional[Reranker] = None,
        reranker_cache: Optional[dict[str, Reranker]] = None,
        search_pool: asyncpg.Pool | None = None,
    ):
        self.pool        = pool
        self.search_pool = search_pool or pool
        self.embedder    = embedder or get_embedder()
        self._reranker_cache: dict[str, Reranker] = reranker_cache if reranker_cache is not None else {}
        if reranker:
            self._reranker_cache["default"] = reranker

    # ── Main entry ────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str = "",
        jd: str | None = None,
        explicit_filters: dict[str, Any] | None = None,
        mode: str = "quality",
        top_k: int | None = None,
        # Legacy keyword args (kept for backward compat with old API callers)
        filters: Any = None,
        use_rrf: bool = True,
        max_chunks_per_candidate: int = 3,
    ) -> list[SearchResult]:
        """Run the full pipeline. Returns list[SearchResult]."""
        resp = await self.smart_search(
            query=query,
            jd=jd,
            explicit_filters=explicit_filters,
            mode=mode,
            top_k=top_k,
        )
        return resp.results

    async def smart_search(
        self,
        query: str = "",
        jd: str | None = None,
        explicit_filters: dict[str, Any] | None = None,
        mode: str = "quality",
        top_k: int | None = None,
        recruiter_id: str | None = None,
        config_overrides: dict[str, Any] | None = None,
    ) -> SearchResponse:
        """Full pipeline returning SearchResponse (results + meta)."""
        override_keys = set((config_overrides or {}).keys())
        cfg     = get_config(mode, overrides=config_overrides or {})
        top_k   = top_k or cfg["final_top_k"]
        cfg     = _apply_dynamic_retrieval_limits(cfg, top_k, override_keys)
        raw_text = jd or query or ""
        _metrics.incr("searches_total")
        cache_enabled = bool(cfg.get("use_cache", True))
        recruiter_prefs = await _call_with_optional_use_cache(
            self._load_recruiter_prefs,
            recruiter_id,
            use_cache=cache_enabled,
        )
        with _obs_span("search.load_personalization", input={"recruiter_id": recruiter_id}):
            hints_enabled, recruiter_hints = await _call_with_optional_use_cache(
                self._load_recruiter_hints,
                recruiter_id,
                use_cache=cache_enabled,
            )
            cfg["personalization_enabled"] = hints_enabled
            cfg["personalization_hints"]   = recruiter_hints
            
            recruiter_profile = None
            if cfg.get("use_personalization") and recruiter_id:
                from pipeline.personalization import load_recruiter_profile
                recruiter_profile = await load_recruiter_profile(
                    self.pool,
                    recruiter_id,
                    use_cache=cfg.get("use_cache", True),
                )
                if recruiter_profile:
                    if recruiter_profile.total_outcomes >= 5:
                        _metrics.incr("personalization_applied")
                    else:
                        _metrics.incr("personalization_cold_start")
            _obs_update_current_span(output={
                "hints_enabled": hints_enabled,
                "hints_count": len(recruiter_hints),
                "profile_loaded": recruiter_profile is not None,
            })

        # ── Phase 1: Query understanding (steps 1-7) ─────────────────────────
        _t_phase1 = _time.perf_counter()

        # ── Filter-only short-circuit ────────────────────────────────────────
        # No query text but the recruiter set chips/filters → skip LLM entirely
        # and build the spec straight from the explicit filters. The clarify
        # gate inside the LLM planner used to swallow this case.
        if not raw_text.strip() and _has_filter_values(explicit_filters):
            spec = _spec_from_explicit_filters(explicit_filters or {})
            phase1_ms = (_time.perf_counter() - _t_phase1) * 1000
            spec_dict = dataclasses.asdict(spec)
            results = await self._filter_only_search(spec, top_k)
            return SearchResponse(
                results=results, spec=spec,
                phase_timings={"query_understanding_ms": round(phase1_ms, 2)},
                spec_dict=spec_dict,
            )

        # ── Step 1: Route detection ───────────────────────────────────────────
        if cfg["use_route_detector"]:
            route = detect_route(query=query, jd=jd, explicit_filters=explicit_filters)
        else:
            route = "query_mode"

        # ── Step 2: Sanitize ──────────────────────────────────────────────────
        # (sanitize_input is called inside planner.plan when use_sanitizer=True)

        # ── Steps 3-5: Plan → validate → normalize ────────────────────────────
        spec = await _plan(
            raw_input=raw_text,
            explicit_filters=explicit_filters,
            route=route,
            cfg=cfg,
        )

        # ── Step 6: Confidence gate ───────────────────────────────────────────
        action = should_short_circuit(spec, cfg)

        phase1_ms = (_time.perf_counter() - _t_phase1) * 1000
        spec_dict = dataclasses.asdict(spec)

        # Partial-extraction fallback: when the LLM raises clarify but it
        # also pulled real structured filters out of the input, the search
        # has enough to run. Demote the clarify to a soft notice instead of
        # blocking the user behind an empty result page.
        preserved_clarify: str | None = None
        if action == "clarify" and _has_meaningful_must_values(spec.must):
            preserved_clarify = spec.clarify
            spec.clarify = None
            spec_dict["clarify"] = None
            _metrics.incr("clarify_demoted_to_notice")
            action = "filter_only" if spec.is_filter_only() else None
            logger.info(
                "Demoted clarify to soft notice — extracted filters: country=%s city=%s skills=%s",
                spec.must.country, spec.must.city, spec.must.skills,
            )

        if action == "clarify":
            _metrics.incr("clarify_returned")
            return SearchResponse(results=[], spec=spec, clarify=spec.clarify,
                                  phase_timings={"query_understanding_ms": round(phase1_ms, 2)},
                                  spec_dict=spec_dict)

        if action == "lookup":
            results = await self._lookup_by_name(query, top_k)
            return SearchResponse(results=results, spec=spec, clarify=preserved_clarify,
                                  phase_timings={"query_understanding_ms": round(phase1_ms, 2)},
                                  spec_dict=spec_dict)

        if action == "filter_only":
            results = await self._filter_only_search(spec, top_k)
            return SearchResponse(results=results, spec=spec, clarify=preserved_clarify,
                                  phase_timings={"query_understanding_ms": round(phase1_ms, 2)},
                                  spec_dict=spec_dict)

        if action in ("comparison", "explanation", "analytics"):
            return SearchResponse(results=[], spec=spec, clarify=f"Use /{action} endpoint",
                                  phase_timings={"query_understanding_ms": round(phase1_ms, 2)},
                                  spec_dict=spec_dict)

        # ── Full retrieval pipeline (candidate_search) ────────────────────────
        resp = await self._full_pipeline(spec, cfg, top_k, recruiter_prefs,
                                         recruiter_id=recruiter_id,
                                         recruiter_profile=recruiter_profile,
                                         phase1_ms=phase1_ms, spec_dict=spec_dict)
        if preserved_clarify and not resp.clarify:
            resp.clarify = preserved_clarify
        return resp

    # ── Full pipeline ─────────────────────────────────────────────────────────

    async def _full_pipeline(
        self,
        spec: CanonicalSearchSpec,
        cfg: dict,
        top_k: int,
        recruiter_prefs: dict | None = None,
        recruiter_id: str | None = None,
        recruiter_profile: Any | None = None,
        phase1_ms: float = 0.0,
        spec_dict: dict | None = None,
    ) -> SearchResponse:
        relaxations: list[dict] = []

        # ── Phase 2: Retrieval (steps 8-11) ──────────────────────────────────
        _t_phase2 = _time.perf_counter()

        # ── Step 9: SQL hard filter (built once, inlined as CTE downstream) ──
        if cfg["use_sql_filter"]:
            filter_sql, filter_params = build_candidate_filter(spec.must, spec.must_not)
        else:
            filter_sql, filter_params = "TRUE", []

        # Resolve doc_type filter from search_targets
        doc_types = _resolve_doc_types(spec.search_targets)
        use_retrieval_cache = bool(cfg.get("use_cache", True) and cfg.get("use_retrieval_cache", False))
        use_data_cache = bool(cfg.get("use_cache", True))

        # ── Step 10: Parallel retrieval ───────────────────────────────────────
        keyword_decision = (
            decide_keyword_policy(spec, cfg, candidate_count=None)
            if cfg["use_bm25"]
            else KeywordPolicyDecision(
                "skip", "keyword_disabled", "skip",
                int(cfg.get("keyword_timeout_ms") or 800),
            )
        )
        search_pool_mode = "separate" if self.search_pool is not self.pool else "primary"
        retrieval_policy: dict[str, Any] = {
            "keyword": {
                **keyword_decision.to_dict(),
                "backend": cfg.get("bm25_backend", "fts"),
            },
            "search_pool": search_pool_mode,
        }
        retrieval_timings: dict[str, float] = {}
        retrieval_db_timings: dict[str, Any] = {}
        retrieval_counts: dict[str, int] = {}
        hydration_passes: list[dict[str, Any]] = []

        with _obs_span("search.retrieve", input={
            "spec": spec_payload(spec),
            "filter": filter_payload(filter_sql, filter_params, doc_types),
            "config": retrieval_config_payload(cfg, top_k=top_k, search_pool_mode=search_pool_mode),
            "keyword_initial_policy": retrieval_policy["keyword"],
            "payload_version": "search-retrieval/v1",
        }):
            # total_candidates_scanned runs alongside the retrievers so we keep
            # the metric without paying the cost of shipping every UUID to Python.
            import contextvars
            ctx = contextvars.copy_context()

            async def _wrap_count():
                started = _time.perf_counter()
                db_timing: dict[str, Any] = {}
                with _obs_span("search.retrieve.count", input={
                    "filter": filter_payload(filter_sql, filter_params, doc_types),
                }):
                    total = await self._count_filtered(
                        filter_sql,
                        filter_params,
                        timings=db_timing,
                        use_cache=use_retrieval_cache,
                    )
                    elapsed = ms_since(started, _time.perf_counter)
                    retrieval_timings["count_ms"] = elapsed
                    retrieval_db_timings["count"] = db_timing
                    retrieval_counts["filtered_candidates"] = int(total)
                    _obs_update_current_span(output={
                        "candidate_count": total,
                        "elapsed_ms": elapsed,
                        "db_timing": db_timing_payload(db_timing),
                    })
                    return total

            async def _wrap_dense():
                started = _time.perf_counter()
                db_timing: dict[str, Any] = {}
                with _obs_span("search.retrieve.semantic", input={
                    "semantic_query": spec.semantic_query,
                    "embedding_text": spec.embed_text(),
                    "top_k": cfg["dense_top_k"],
                    "doc_type_filter": doc_types or [],
                    "include_content": not cfg.get("defer_chunk_content", True),
                    "filter_param_count": len(filter_params),
                    "hnsw_ef_search_min": settings.hnsw_ef_search,
                }):
                    res = await retrieve_dense(
                        spec, filter_sql, filter_params, self.search_pool, self.embedder,
                        top_k=cfg["dense_top_k"], doc_type_filter=doc_types or None,
                        include_content=not cfg.get("defer_chunk_content", True),
                        use_retrieval_cache=use_retrieval_cache,
                        timings=db_timing,
                    )
                    elapsed = ms_since(started, _time.perf_counter)
                    retrieval_timings["dense_ms"] = elapsed
                    retrieval_db_timings["dense"] = db_timing
                    retrieval_counts["dense_rows"] = len(res)
                    _obs_update_current_span(output={
                        "retrieved_chunks": len(res),
                        "elapsed_ms": elapsed,
                        "db_timing": db_timing_payload(db_timing),
                        "top_chunks": row_preview(res, limit=10),
                    })
                    return res

            async def _wrap_dense_skip():
                with _obs_span("search.retrieve.semantic", input={"enabled": False}):
                    retrieval_timings["dense_ms"] = 0.0
                    retrieval_counts["dense_rows"] = 0
                    _obs_update_current_span(output={
                        "skipped": True,
                        "reason": "dense_disabled",
                        "retrieved_chunks": 0,
                        "elapsed_ms": 0.0,
                    })
                return []

            async def _wrap_bm25(decision: KeywordPolicyDecision):
                started = _time.perf_counter()
                db_timing: dict[str, Any] = {}
                policy_dict = decision.to_dict()
                db_timing["tail_risk"] = policy_dict.get("tail_risk")
                backend = cfg.get("bm25_backend", "fts")
                with _obs_span("search.retrieve.keyword", input={
                    "lexical_terms": spec.lexical_terms,
                    "semantic_query": spec.semantic_query,
                    "top_k": cfg["bm25_top_k"],
                    "doc_type_filter": doc_types or [],
                    "include_content": not cfg.get("defer_chunk_content", True),
                    "policy": policy_dict,
                    "backend": backend,
                    "statement_timeout_ms": decision.timeout_ms,
                    "overfetch_factor": cfg.get("bm25_overfetch_factor", 4),
                    "overfetch_min": cfg.get("bm25_overfetch_min", 60),
                }):
                    try:
                        res = await asyncio.wait_for(
                            retrieve_bm25(
                                spec, filter_sql, filter_params, self.search_pool,
                                top_k=cfg["bm25_top_k"],
                                doc_type_filter=doc_types or None,
                                include_content=not cfg.get("defer_chunk_content", True),
                                statement_timeout_ms=decision.timeout_ms,
                                backend=backend,
                                overfetch_factor=cfg.get("bm25_overfetch_factor", 4),
                                overfetch_min=cfg.get("bm25_overfetch_min", 60),
                                use_retrieval_cache=use_retrieval_cache,
                                timings=db_timing,
                            ),
                            timeout=(decision.timeout_ms / 1000.0) + 0.25,
                        )
                    except (asyncio.TimeoutError, asyncpg.exceptions.QueryCanceledError):
                        elapsed = ms_since(started, _time.perf_counter)
                        retrieval_timings["keyword_ms"] = elapsed
                        retrieval_db_timings["keyword"] = db_timing
                        retrieval_counts["keyword_rows"] = 0
                        _obs_update_current_span(output={
                            "retrieved_chunks": 0,
                            "elapsed_ms": elapsed,
                            "db_timing": db_timing_payload(db_timing),
                            "timed_out": True,
                            "policy": policy_dict,
                            "backend": backend,
                            "tail_risk": policy_dict.get("tail_risk"),
                        })
                        return []
                    elapsed = ms_since(started, _time.perf_counter)
                    retrieval_timings["keyword_ms"] = elapsed
                    retrieval_db_timings["keyword"] = db_timing
                    retrieval_counts["keyword_rows"] = len(res)
                    _obs_update_current_span(output={
                        "retrieved_chunks": len(res),
                        "elapsed_ms": elapsed,
                        "db_timing": db_timing_payload(db_timing),
                        "policy": policy_dict,
                        "backend": backend,
                        "tail_risk": policy_dict.get("tail_risk"),
                        "top_chunks": row_preview(res, limit=10),
                    })
                    return res

            async def _wrap_keyword_skip(decision: KeywordPolicyDecision):
                policy_dict = decision.to_dict()
                with _obs_span("search.retrieve.keyword", input={
                    "lexical_terms": spec.lexical_terms,
                    "semantic_query": spec.semantic_query,
                    "top_k": cfg["bm25_top_k"],
                    "policy": policy_dict,
                    "backend": cfg.get("bm25_backend", "fts"),
                }):
                    retrieval_timings["keyword_ms"] = 0.0
                    retrieval_counts["keyword_rows"] = 0
                    retrieval_db_timings["keyword"] = {
                        "skipped": True,
                        "reason": decision.reason,
                        "tail_risk": policy_dict.get("tail_risk"),
                    }
                    _obs_update_current_span(output={
                        "skipped": True,
                        "reason": decision.reason,
                        "retrieved_chunks": 0,
                        "elapsed_ms": 0.0,
                        "policy": policy_dict,
                        "backend": cfg.get("bm25_backend", "fts"),
                        "tail_risk": policy_dict.get("tail_risk"),
                    })
                return []

            async def _wrap_skill():
                started = _time.perf_counter()
                db_timing: dict[str, Any] = {}
                skills = list((spec.should.skills or []) + (spec.must.skills or []))
                with _obs_span("search.retrieve.skill_match", input={
                    "skills": skills,
                    "top_k": cfg["skill_top_k"],
                    "include_content": not cfg.get("defer_chunk_content", True),
                    "filter_param_count": len(filter_params),
                }):
                    res = await retrieve_skill(
                        spec, filter_sql, filter_params, self.search_pool,
                        top_k=cfg["skill_top_k"],
                        include_content=not cfg.get("defer_chunk_content", True),
                        use_retrieval_cache=use_retrieval_cache,
                        timings=db_timing,
                    )
                    elapsed = ms_since(started, _time.perf_counter)
                    retrieval_timings["skill_ms"] = elapsed
                    retrieval_db_timings["skill"] = db_timing
                    retrieval_counts["skill_rows"] = len(res)
                    _obs_update_current_span(output={
                        "retrieved_chunks": len(res),
                        "elapsed_ms": elapsed,
                        "db_timing": db_timing_payload(db_timing),
                        "top_chunks": row_preview(res, limit=10),
                    })
                    return res

            async def _wrap_skill_skip():
                with _obs_span("search.retrieve.skill_match", input={"enabled": False}):
                    retrieval_timings["skill_ms"] = 0.0
                    retrieval_counts["skill_rows"] = 0
                    _obs_update_current_span(output={
                        "skipped": True,
                        "reason": "skill_exact_disabled",
                        "retrieved_chunks": 0,
                        "elapsed_ms": 0.0,
                    })
                return []

            async def _hydrate_with_trace(rows: list[dict[str, Any]], limit: int, pass_name: str):
                started = _time.perf_counter()
                db_timing: dict[str, Any] = {}
                with _obs_span("search.retrieve.hydrate_chunks", input={
                    "pass": pass_name,
                    "fused_rows": len(rows),
                    "limit": limit,
                    "reason": "chunk_content_deferred_until_after_fusion",
                }):
                    hydrated_rows = await self._hydrate_fused_chunks(
                        rows,
                        limit,
                        timings=db_timing,
                        use_cache=use_data_cache,
                    )
                    elapsed = ms_since(started, _time.perf_counter)
                    hydration_pass = {
                        "pass": pass_name,
                        "limit": limit,
                        "hydrated_rows": len(hydrated_rows),
                        "elapsed_ms": elapsed,
                        "db_timing": db_timing,
                    }
                    hydration_passes.append(hydration_pass)
                    retrieval_db_timings[f"hydrate_{pass_name}"] = db_timing
                    retrieval_timings[f"hydrate_{pass_name}_ms"] = elapsed
                    _obs_update_current_span(output={
                        **hydration_pass,
                        "db_timing": db_timing_payload(db_timing),
                        "top_chunks": row_preview(hydrated_rows, limit=10),
                    })
                    return hydrated_rows

            def _group_with_trace(rows: list[dict[str, Any]], source: str) -> list[SearchResult]:
                started = _time.perf_counter()
                with _obs_span("search.retrieve.group_candidates", input={
                    "source": source,
                    "row_count": len(rows),
                }):
                    grouped = group_by_candidate(rows)
                    elapsed = ms_since(started, _time.perf_counter)
                    retrieval_timings[f"group_{source}_ms"] = elapsed
                    _obs_update_current_span(output={
                        "candidate_count": len(grouped),
                        "elapsed_ms": elapsed,
                        "top_candidates": result_preview(grouped, limit=10),
                    })
                    return grouped

            count_task = asyncio.create_task(ctx.run(_wrap_count))
            dense_task = asyncio.create_task(ctx.run(_wrap_dense) if cfg["use_dense"] else ctx.run(_wrap_dense_skip))
            skill_task = asyncio.create_task(ctx.run(_wrap_skill) if cfg["use_skill_exact"] else ctx.run(_wrap_skill_skip))
            bm25_task = (
                asyncio.create_task(ctx.run(_wrap_bm25, keyword_decision))
                if cfg["use_bm25"] and keyword_decision.action == "run"
                else (
                    asyncio.create_task(ctx.run(_wrap_keyword_skip, keyword_decision))
                    if keyword_decision.action == "skip"
                    else None
                )
            )

            total_scanned = await count_task
            retrieval_policy["keyword"]["candidate_count"] = total_scanned
            if cfg["use_bm25"] and keyword_decision.action == "defer":
                keyword_decision = decide_keyword_policy(spec, cfg, candidate_count=total_scanned)
                retrieval_policy["keyword"] = {
                    **keyword_decision.to_dict(),
                    "backend": cfg.get("bm25_backend", "fts"),
                }
                if keyword_decision.action == "run":
                    bm25_task = asyncio.create_task(ctx.run(_wrap_bm25, keyword_decision))
                else:
                    bm25_task = asyncio.create_task(ctx.run(_wrap_keyword_skip, keyword_decision))

            dense_rows, skill_rows = await asyncio.gather(dense_task, skill_task)
            bm25_rows = await bm25_task if bm25_task is not None else []

            # ── Step 11: RRF fusion ───────────────────────────────────────────
            fusion_started = _time.perf_counter()
            with _obs_span("search.retrieve.fuse", input={
                "strategy": "rrf" if cfg["use_rrf"] else "first_enabled_branch",
                "dense_rows": len(dense_rows),
                "keyword_rows": len(bm25_rows),
                "skill_rows": len(skill_rows),
                "rrf_k": cfg.get("rrf_k", 60),
                "search_target_weighting": cfg["use_search_target_weighting"],
                "search_targets": spec.search_targets,
            }):
                if cfg["use_rrf"]:
                    fused = rrf_fuse(
                        dense_rows, bm25_rows, skill_rows,
                        spec_targets=spec.search_targets,
                        rrf_k=cfg.get("rrf_k", 60),
                        use_search_target_weighting=cfg["use_search_target_weighting"],
                    )
                else:
                    fused = dense_rows or bm25_rows or skill_rows
                elapsed = ms_since(fusion_started, _time.perf_counter)
                retrieval_timings["fusion_ms"] = elapsed
                retrieval_counts["fused_rows"] = len(fused)
                _obs_update_current_span(output={
                    "fused_rows": len(fused),
                    "elapsed_ms": elapsed,
                    "top_fused_chunks": row_preview(fused, limit=10),
                })

            # ── Step 12: Group chunks → candidates ───────────────────────────
            if cfg.get("defer_chunk_content", True):
                hydrate_limit = max(1, int(cfg.get("chunk_hydration_top_k") or len(fused)))
                hydrated = await _hydrate_with_trace(fused, hydrate_limit, "initial")
                results = _group_with_trace(hydrated, "hydrated_initial")
                available_candidates = len({r.get("candidate_id") for r in fused if r.get("candidate_id")})
                if (
                    len(results) < min(top_k, available_candidates)
                    and hydrate_limit < len(fused)
                ):
                    second_limit = min(len(fused), hydrate_limit * 2)
                    hydrated = await _hydrate_with_trace(fused, second_limit, "expanded")
                    results = _group_with_trace(hydrated, "hydrated_expanded")
            else:
                results = _group_with_trace(fused, "fused")

            all_grouped_results = list(results)
            candidate_id_window = [r.candidate_id for r in all_grouped_results if r.candidate_id]
            retrieval_counts["grouped_candidates"] = len(all_grouped_results)
            enrichment_limit = max(1, int(cfg.get("candidate_enrichment_top_k") or len(all_grouped_results)))
            visible_results = all_grouped_results[:enrichment_limit]
            deferred_candidate_ids = candidate_id_window[enrichment_limit:]
            retrieval_counts["candidates_selected_for_enrichment"] = len(visible_results)
            retrieval_counts["deferred_candidate_ids"] = len(deferred_candidate_ids)

            enrich_started = _time.perf_counter()
            enrich_db_timing: dict[str, Any] = {}
            with _obs_span("search.retrieve.enrich_candidates", input={
                "candidate_count": len(visible_results),
                "limit": enrichment_limit,
                "deferred_candidate_count": len(deferred_candidate_ids),
                "fields_loaded": [
                    "full_name",
                    "email",
                    "city",
                    "country",
                    "salary_min",
                    "salary_max",
                    "years_exp",
                    "skills",
                ],
            }):
                results = await self._enrich_candidates(
                    visible_results,
                    spec=spec,
                    timings=enrich_db_timing,
                    use_cache=use_data_cache,
                )
                elapsed = ms_since(enrich_started, _time.perf_counter)
                retrieval_timings["enrichment_ms"] = elapsed
                retrieval_db_timings["enrichment"] = enrich_db_timing
                retrieval_counts["enriched_candidates"] = len(results)
                _obs_update_current_span(output={
                    "enriched_candidates": len(results),
                    "elapsed_ms": elapsed,
                    "db_timing": db_timing_payload(enrich_db_timing),
                    "top_candidates": result_preview(results, limit=10),
                })

            phase2_ms = (_time.perf_counter() - _t_phase2) * 1000
            retrieval_timings["total_ms"] = round(phase2_ms, 2)
            retrieval_policy["timings_ms"] = dict(retrieval_timings)
            retrieval_policy["db_timings"] = dict(retrieval_db_timings)
            retrieval_policy["row_counts"] = dict(retrieval_counts)
            retrieval_policy["hydration_passes"] = list(hydration_passes)
            retrieval_policy["candidate_ids"] = candidate_id_window
            retrieval_policy["deferred_candidate_ids"] = deferred_candidate_ids
            retrieval_policy["deferred_enrichment"] = {
                "enabled": bool(deferred_candidate_ids),
                "visible_enriched": len(results),
                "available_candidate_ids": len(candidate_id_window),
                "fetch_endpoint": "/talent/api/candidates/batch",
            }
            timing_summary = retrieval_timing_summary(
                retrieval_timings,
                retrieval_db_timings,
                retrieval_counts,
            )
            retrieval_policy["timing_summary"] = timing_summary
            _obs_update_current_span(output={
                "total_candidates_scanned": total_scanned,
                "branch_rows": dict(retrieval_counts),
                "branch_timings_ms": dict(retrieval_timings),
                "timing_summary": timing_summary,
                "db_timings": {
                    key: db_timing_payload(value)
                    for key, value in retrieval_db_timings.items()
                },
                "keyword_policy": retrieval_policy["keyword"],
                "hydration_passes": hydration_passes,
                "candidate_ids": candidate_id_window[:50],
                "deferred_candidate_count": len(deferred_candidate_ids),
                "top_candidates_after_enrichment": result_preview(results, limit=10),
            })

        # ── Phase 3: Ranking & diversity (steps 13-18) ───────────────────────
        _t_phase3 = _time.perf_counter()

        # ── Step 13: Empty-result guard ───────────────────────────────────────
        if not results and cfg.get("use_auto_relax", True):
            relaxed_spec, relaxations = _auto_relax_spec(spec)
            if relaxed_spec is not None:
                logger.info("Auto-relaxing empty search with %s", relaxations)
                relaxed_cfg = dict(cfg)
                relaxed_cfg["use_auto_relax"] = False
                relaxed_resp = await self._full_pipeline(
                    relaxed_spec,
                    relaxed_cfg,
                    top_k,
                    recruiter_prefs=recruiter_prefs,
                    recruiter_id=recruiter_id,
                    recruiter_profile=recruiter_profile,
                    phase1_ms=phase1_ms,
                    spec_dict=dataclasses.asdict(relaxed_spec),
                )
                relaxed_resp.relaxations_applied = relaxations + list(relaxed_resp.relaxations_applied or [])
                return relaxed_resp

        ranking_timings: dict[str, float] = {}
        with _obs_span("search.rank", input={
            "candidate_count": len(results),
            "config": ranking_config_payload(cfg, top_k=top_k),
            "input_candidates": result_preview(results, limit=10),
            "payload_version": "search-ranking/v1",
        }):
            # ── Step 14: Cross-encoder rerank ─────────────────────────────────
            if cfg["use_cross_encoder"] and results:
                reranker = self._get_reranker()
                ce_input = results[:cfg["cross_encoder_top_k"]]
                _ce_start = _time.perf_counter()
                try:
                    with _obs_span("search.rank.cross_encoder", input={
                        "semantic_query": spec.semantic_query,
                        "candidate_count": len(ce_input),
                        "top_k": cfg["cross_encoder_top_k"],
                    }):
                        ce_input = await reranker.rerank(spec.semantic_query, ce_input)
                        elapsed = ms_since(_ce_start, _time.perf_counter)
                        ranking_timings["cross_encoder_ms"] = elapsed
                        _obs_update_current_span(output={
                            "reranked_count": len(ce_input),
                            "elapsed_ms": elapsed,
                            "top_candidates": result_preview(ce_input, limit=10),
                        })
                finally:
                    self._release_reranker(reranker)
                logger.info(
                    "Cross-encoder rerank: %d candidates in %.0f ms",
                    len(ce_input), (_time.perf_counter() - _ce_start) * 1000,
                )
                results  = ce_input + results[cfg["cross_encoder_top_k"]:]
            else:
                ranking_timings["cross_encoder_ms"] = 0.0

            # ── Step 15: Feature ranker ───────────────────────────────────────
            if cfg["use_feature_ranker"]:
                rank_started = _time.perf_counter()
                with _obs_span("search.rank.feature_score", input={
                    "candidate_count": len(results),
                    "use_cross_encoder": cfg["use_cross_encoder"],
                    "has_recruiter_prefs": recruiter_prefs is not None,
                    "has_recruiter_profile": recruiter_profile is not None,
                }):
                    results = score_results(
                        results, spec,
                        use_cross_encoder=cfg["use_cross_encoder"],
                        recruiter_prefs=recruiter_prefs,
                        recruiter_profile=recruiter_profile,
                    )
                    elapsed = ms_since(rank_started, _time.perf_counter)
                    ranking_timings["feature_ranker_ms"] = elapsed
                    _obs_update_current_span(output={
                        "scored_count": len(results),
                        "elapsed_ms": elapsed,
                        "top_candidates": result_preview(results, limit=10),
                    })
            else:
                ranking_timings["feature_ranker_ms"] = 0.0

            # ── Step 16: MMR diversity ────────────────────────────────────────
            if cfg["use_mmr"]:
                mmr_started = _time.perf_counter()
                with _obs_span("search.rank.diversify", input={
                    "candidate_count": len(results),
                    "top_k": top_k,
                    "lambda": cfg["mmr_lambda"],
                    "embedding_backed": self.embedder is not None,
                }):
                    mmr_embedder = (
                        None
                        if getattr(settings, "local_embedding_backend", "") == "fastembed"
                        else self.embedder
                    )
                    results = await mmr_select_async(
                        results,
                        top_k,
                        lam=cfg["mmr_lambda"],
                        embedder=mmr_embedder,
                        use_cache=use_data_cache,
                    )
                    elapsed = ms_since(mmr_started, _time.perf_counter)
                    ranking_timings["mmr_ms"] = elapsed
                    _obs_update_current_span(output={
                        "selected_count": len(results),
                        "elapsed_ms": elapsed,
                        "top_candidates": result_preview(results, limit=10),
                    })
            else:
                results = results[:top_k]
                ranking_timings["mmr_ms"] = 0.0

            # ── Step 17: Attach ranking signals + explanation ─────────────────
            explain_started = _time.perf_counter()
            with _obs_span("search.rank.explain", input={"candidate_count": len(results)}):
                for i, r in enumerate(results):
                    r.explanation = build_explanation(r, spec, rank_position=i + 1)
                elapsed = ms_since(explain_started, _time.perf_counter)
                ranking_timings["explanation_ms"] = elapsed
                _obs_update_current_span(output={
                    "explained_count": len(results),
                    "elapsed_ms": elapsed,
                })

            phase3_ms = (_time.perf_counter() - _t_phase3) * 1000
            ranking_timings["total_ms"] = round(phase3_ms, 2)
            _obs_update_current_span(output={
                "selected_count": len(results),
                "timings_ms": dict(ranking_timings),
                "top_results": result_preview(results, limit=10),
            })

        _metrics.observe_result_count(len(results))
        if not results:
            _metrics.incr("empty_results")

        retrieval_policy["ranking_timings_ms"] = dict(ranking_timings)

        resp = SearchResponse(
            results=results,
            spec=spec,
            relaxations_applied=relaxations,
            total_candidates_scanned=total_scanned,
            phase_timings={
                "query_understanding_ms": round(phase1_ms, 2),
                "retrieval_ms":           round(phase2_ms, 2),
                "ranking_ms":             round(phase3_ms, 2),
            },
            retrieval_policy=retrieval_policy,
            spec_dict=spec_dict,
            personalization_applied=bool(recruiter_profile and recruiter_profile.total_outcomes >= 5),
            candidate_ids=list(retrieval_policy.get("candidate_ids") or []),
            deferred_candidate_ids=list(retrieval_policy.get("deferred_candidate_ids") or []),
        )

        # ── Step 20: Log impressions (fire-and-forget) ────────────────────────
        # Assign impression IDs SYNCHRONOUSLY so they're present on the result
        # objects when the response serializes — the UI needs them to record
        # outcomes and relevance judgments. The DB write stays fire-and-forget.
        if cfg["use_impression_logging"]:
            from pipeline.observability import get_current_trace_id
            trace_id = get_current_trace_id()
            for r in results:
                r.impression_id = str(uuid.uuid4())
            asyncio.create_task(self._log_impressions(
                results, search_id=resp.search_id, recruiter_id=recruiter_id, langfuse_trace_id=trace_id
            ))

        return resp

    # ── Short-circuit paths ───────────────────────────────────────────────────

    async def _lookup_by_name(self, name: str, top_k: int) -> list[SearchResult]:
        sql = """
            SELECT id::text AS candidate_id, full_name, email, city, country,
                   salary_min, salary_max, years_exp, skills, '' AS best_chunk
            FROM candidates
            WHERE full_name ILIKE $1
            LIMIT $2
        """
        rows = await self.search_pool.fetch(sql, f"%{name}%", top_k)
        return [_row_to_result(r) for r in rows]

    async def _filter_only_search(self, spec: CanonicalSearchSpec, top_k: int) -> list[SearchResult]:
        where, params = build_filter_sql(spec.must)
        not_clause, params = build_must_not_sql(spec.must_not, params)
        idx = len(params) + 1
        if not_clause:
            sql = f"""
                SELECT id::text AS candidate_id, full_name, email, city, country,
                       salary_min, salary_max, years_exp, skills, '' AS best_chunk
                FROM candidates WHERE {where} AND {not_clause}
                ORDER BY updated_at DESC LIMIT ${idx}
            """
        else:
            sql = f"""
                SELECT id::text AS candidate_id, full_name, email, city, country,
                       salary_min, salary_max, years_exp, skills, '' AS best_chunk
                FROM candidates WHERE {where}
                ORDER BY updated_at DESC LIMIT ${idx}
            """
        params.append(top_k)
        rows = await self.search_pool.fetch(sql, *params)
        return [_row_to_result(r) for r in rows]

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _count_filtered(
        self,
        filter_sql: str,
        filter_params: list[Any],
        timings: dict[str, Any] | None = None,
        use_cache: bool = False,
    ) -> int:
        """Count candidates matching the filter — replaces the old pull-all-IDs pattern."""
        total_started = _time.perf_counter()
        timings = timings if timings is not None else {}
        cache_key = None
        if use_cache:
            cache_key = _cache.retrieval_key("count", {
                "filter_sql": filter_sql,
                "filter_params": filter_params,
            })
            timings["cache_namespace"] = _cache.namespace_for_key(cache_key)
            timings["cache_key_hash"] = _cache.hash_payload(cache_key)
            cache_started = _time.perf_counter()
            cached = await _cache.get(cache_key)
            timings["rowset_cache_lookup_ms"] = round((_time.perf_counter() - cache_started) * 1000, 2)
            if cached is not None:
                timings.update({
                    "cache_hit": True,
                    "rows": 1,
                    "total_ms": round((_time.perf_counter() - total_started) * 1000, 2),
                })
                finalize_db_timing(timings)
                return int(cached)
        timings["cache_hit"] = False
        try:
            acquire_started = _time.perf_counter()
            async with self.search_pool.acquire() as conn:
                timings["pool_acquire_ms"] = round((_time.perf_counter() - acquire_started) * 1000, 2)
                fetch_started = _time.perf_counter()
                value = await conn.fetchval(
                    f"SELECT COUNT(*) FROM candidates WHERE {filter_sql}",
                    *filter_params,
                )
                timings["fetch_roundtrip_ms"] = round((_time.perf_counter() - fetch_started) * 1000, 2)
            total = int(value or 0)
            timings["rows"] = 1
            timings["total_ms"] = round((_time.perf_counter() - total_started) * 1000, 2)
            finalize_db_timing(timings)
            if cache_key is not None:
                await _cache.set(cache_key, total)
            return total
        except Exception as exc:
            logger.debug("count_filtered failed: %s", exc)
            timings["error"] = str(exc)
            timings["total_ms"] = round((_time.perf_counter() - total_started) * 1000, 2)
            finalize_db_timing(timings)
            return 0

    async def _enrich_candidates(
        self,
        results: list[SearchResult],
        spec: CanonicalSearchSpec | None = None,
        timings: dict[str, Any] | None = None,
        use_cache: bool = False,
    ) -> list[SearchResult]:
        timings = timings if timings is not None else {}
        total_started = _time.perf_counter()
        matched_skill_terms = sorted({
            skill.strip().lower()
            for skill in (
                list(getattr(getattr(spec, "must", None), "skills", []) or [])
                + list(getattr(getattr(spec, "should", None), "skills", []) or [])
            )
            if skill and str(skill).strip()
        })
        if not results:
            timings.update({"skipped": True, "reason": "no_results", "total_ms": 0.0})
            return results
        if all((r.full_name or r.skills or r.city or r.country) for r in results) and not matched_skill_terms:
            for r in results:
                r.similarity_score = 1.0 - r.best_chunk_distance
            timings.update({
                "skipped": True,
                "reason": "profile_already_loaded_during_hydration",
                "rows": len(results),
                "total_ms": round((_time.perf_counter() - total_started) * 1000, 2),
            })
            finalize_db_timing(timings)
            return results
        ids = [r.candidate_id for r in results]
        cached_profiles: dict[str, dict[str, Any]] = {}
        if use_cache:
            for candidate_id in ids:
                candidate_cache_key = _cache.candidate_profile_key(candidate_id)
                timings["cache_namespace"] = _cache.namespace_for_key(candidate_cache_key)
                cached = await _cache.get(candidate_cache_key)
                if cached is not None:
                    cached_profiles[candidate_id] = cached
            if cached_profiles:
                for r in results:
                    p = cached_profiles.get(r.candidate_id)
                    if p:
                        _apply_candidate_profile(r, p)
                if len(cached_profiles) == len(ids) and not matched_skill_terms:
                    timings.update({
                        "all_cache_hit": True,
                        "cache_hits": len(cached_profiles),
                        "cache_misses": 0,
                        "rows": len(results),
                        "total_ms": round((_time.perf_counter() - total_started) * 1000, 2),
                    })
                    finalize_db_timing(timings)
                    return results

        ids_to_fetch = [candidate_id for candidate_id in ids if candidate_id not in cached_profiles]
        sql = """
            SELECT id::text AS candidate_id, full_name, email, city, country,
                   salary_min, salary_max, years_exp, skills, updated_at
            FROM candidates WHERE id = ANY($1::uuid[])
        """
        if ids_to_fetch:
            acquire_started = _time.perf_counter()
            async with self.search_pool.acquire() as conn:
                timings["pool_acquire_ms"] = round((_time.perf_counter() - acquire_started) * 1000, 2)
                fetch_started = _time.perf_counter()
                rows = await conn.fetch(sql, ids_to_fetch)
                timings["fetch_roundtrip_ms"] = round((_time.perf_counter() - fetch_started) * 1000, 2)
        else:
            rows = []
            timings["fetch_roundtrip_ms"] = 0.0
        timings["rows"] = len(rows)
        timings["cache_hits"] = len(cached_profiles)
        timings["cache_misses"] = len(ids_to_fetch)
        timings["all_cache_hit"] = False

        recency_by_candidate: dict[str, float] = {}
        if matched_skill_terms:
            with _obs_span("search.retrieve.enrich_candidates.skill_recency", input={
                "candidate_count": len(ids),
                "matched_skill_terms_count": len(matched_skill_terms),
            }):
                recency_cache_key = _cache.skill_recency_key(ids, matched_skill_terms)
                timings["skill_recency_cache_namespace"] = _cache.namespace_for_key(recency_cache_key)
                timings["skill_recency_cache_key_hash"] = _cache.hash_payload(recency_cache_key)
                cache_started = _time.perf_counter()
                cached_recency = await _cache.get(recency_cache_key) if use_cache else None
                timings["skill_recency_cache_lookup_ms"] = round(
                    (_time.perf_counter() - cache_started) * 1000,
                    2,
                )
                if cached_recency is not None:
                    timings["skill_recency_cache_hit"] = True
                    recency_by_candidate = {
                        str(candidate_id): float(days)
                        for candidate_id, days in dict(cached_recency).items()
                    }
                    _obs_update_current_span(output={
                        "cache_hit": True,
                        "returned_candidates": len(recency_by_candidate),
                        "elapsed_ms": timings["skill_recency_cache_lookup_ms"],
                    })
                else:
                    timings["skill_recency_cache_hit"] = False
                    try:
                        recency_fetch_started = _time.perf_counter()
                        recency_rows = await self.search_pool.fetch(
                            """
                            SELECT
                                cs.candidate_id::text AS candidate_id,
                                EXTRACT(EPOCH FROM (NOW() - MAX(cs.last_used_at))) / 86400.0
                                    AS matched_skill_recency_days
                            FROM candidate_skills cs
                            WHERE cs.candidate_id = ANY($1::uuid[])
                              AND LOWER(cs.skill) = ANY($2::text[])
                              AND cs.last_used_at IS NOT NULL
                            GROUP BY cs.candidate_id
                            """,
                            ids,
                            matched_skill_terms,
                        )
                        timings["skill_recency_fetch_roundtrip_ms"] = round(
                            (_time.perf_counter() - recency_fetch_started) * 1000,
                            2,
                        )
                        recency_by_candidate = {
                            row["candidate_id"]: float(row["matched_skill_recency_days"])
                            for row in recency_rows
                            if row.get("matched_skill_recency_days") is not None
                        }
                        if use_cache:
                            await _cache.set(recency_cache_key, recency_by_candidate)
                        _obs_update_current_span(output={
                            "cache_hit": False,
                            "returned_candidates": len(recency_by_candidate),
                            "db_timing": {
                                "fetch_roundtrip_ms": timings.get("skill_recency_fetch_roundtrip_ms", 0.0),
                            },
                        })
                    except Exception as exc:
                        logger.debug("skill recency fetch failed: %s", exc)
                        timings["skill_recency_error"] = str(exc)
                        _obs_update_current_span(output={
                            "cache_hit": False,
                            "error": str(exc),
                        })
        timings["total_ms"] = round((_time.perf_counter() - total_started) * 1000, 2)
        finalize_db_timing(timings)
        profile = {r["candidate_id"]: r for r in rows}
        if use_cache:
            for candidate_id, row in profile.items():
                await _cache.set(_cache.candidate_profile_key(candidate_id), _candidate_profile_payload(row))

        for r in results:
            p = cached_profiles.get(r.candidate_id) or profile.get(r.candidate_id)
            if p:
                _apply_candidate_profile(r, p)
            r.matched_skill_recency_days = recency_by_candidate.get(r.candidate_id)
        return results

    async def _hydrate_fused_chunks(
        self,
        rows: list[dict[str, Any]],
        limit: int,
        timings: dict[str, Any] | None = None,
        use_cache: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch chunk text for the narrowed fused set in one batched query."""
        timings = timings if timings is not None else {}
        total_started = _time.perf_counter()
        if not rows:
            timings.update({"skipped": True, "reason": "no_rows", "total_ms": 0.0})
            return []

        selected = rows[: max(0, min(limit, len(rows)))]
        chunk_ids = [r["chunk_id"] for r in selected if r.get("chunk_id")]
        if not chunk_ids:
            timings.update({"skipped": True, "reason": "no_chunk_ids", "total_ms": 0.0})
            return selected

        cached_by_chunk_id: dict[str, dict[str, Any]] = {}
        if use_cache:
            for chunk_id in chunk_ids:
                chunk_cache_key = _cache.chunk_key(chunk_id)
                timings["cache_namespace"] = _cache.namespace_for_key(chunk_cache_key)
                cached = await _cache.get(chunk_cache_key)
                if cached is not None:
                    cached_by_chunk_id[chunk_id] = cached
        missing_chunk_ids = [chunk_id for chunk_id in chunk_ids if chunk_id not in cached_by_chunk_id]

        if not missing_chunk_ids:
            timings.update({
                "all_cache_hit": True,
                "cache_hits": len(cached_by_chunk_id),
                "cache_misses": 0,
                "rows": len(selected),
                "requested_chunk_ids": len(chunk_ids),
                "total_ms": round((_time.perf_counter() - total_started) * 1000, 2),
            })
            finalize_db_timing(timings)
            return _merge_hydrated_rows(selected, cached_by_chunk_id)

        try:
            acquire_started = _time.perf_counter()
            async with self.search_pool.acquire() as conn:
                timings["pool_acquire_ms"] = round((_time.perf_counter() - acquire_started) * 1000, 2)
                fetch_started = _time.perf_counter()
                hydrated_rows = await conn.fetch(
                    """
                    SELECT
                        dc.id::text          AS chunk_id,
                        dc.content           AS content,
                        dc.document_id::text AS document_id,
                        cd.doc_type          AS doc_type,
                        cd.title             AS document_title
                    FROM document_chunks dc
                    JOIN candidate_documents cd ON cd.id = dc.document_id
                    WHERE dc.id = ANY($1::uuid[])
                    """,
                    missing_chunk_ids,
                )
                timings["fetch_roundtrip_ms"] = round((_time.perf_counter() - fetch_started) * 1000, 2)
            timings["rows"] = len(hydrated_rows)
            timings["requested_chunk_ids"] = len(chunk_ids)
            timings["cache_hits"] = len(cached_by_chunk_id)
            timings["cache_misses"] = len(missing_chunk_ids)
            timings["all_cache_hit"] = False
            timings["total_ms"] = round((_time.perf_counter() - total_started) * 1000, 2)
            finalize_db_timing(timings)
        except Exception as exc:
            logger.debug("chunk hydration failed: %s", exc)
            timings["error"] = str(exc)
            timings["total_ms"] = round((_time.perf_counter() - total_started) * 1000, 2)
            finalize_db_timing(timings)
            return selected

        by_chunk_id = {r["chunk_id"]: dict(r) for r in hydrated_rows}
        if use_cache:
            for chunk_id, payload in by_chunk_id.items():
                await _cache.set(_cache.chunk_key(chunk_id), _chunk_payload(payload))
        by_chunk_id.update(cached_by_chunk_id)
        return _merge_hydrated_rows(selected, by_chunk_id)

    async def _load_recruiter_hints(
        self, recruiter_id: str | None,
        use_cache: bool = True,
    ) -> tuple[bool, list]:
        """Fetch (personalization_enabled, fact hints) from recruiter_memory.

        Returns (False, []) on miss so new recruiters must explicitly opt in.
        On errors it also fail-closes to avoid applying memory unexpectedly.
        """
        if not recruiter_id:
            return False, []
        cache_key = _cache.recruiter_hints_key(recruiter_id)
        if use_cache:
            cached = await _cache.get(cache_key)
            if cached is not None:
                return bool(cached.get("enabled")), list(cached.get("hints") or [])
        try:
            enabled_row = await self.pool.fetchrow(
                """SELECT personalization_enabled FROM recruiter_preferences
                   WHERE recruiter_id = $1::uuid""",
                recruiter_id,
            )
            enabled = bool(enabled_row["personalization_enabled"]) if enabled_row else False
            if not enabled:
                return False, []
            rows = await self.pool.fetch(
                """SELECT content FROM recruiter_memory
                   WHERE recruiter_id = $1::uuid AND kind = 'fact'
                     AND status = 'active'
                   ORDER BY created_at DESC LIMIT 20""",
                recruiter_id,
            )
        except Exception as exc:
            logger.debug("recruiter memory fetch failed: %s", exc)
            return False, []
        hints = [{"text": r["content"]} for r in rows]
        if use_cache:
            await _cache.set(cache_key, {"enabled": enabled, "hints": hints})
        return enabled, hints

    async def _load_recruiter_prefs(
        self,
        recruiter_id: str | None,
        use_cache: bool = True,
    ) -> dict | None:
        """Fetch the recruiter's preference row, if any. Returns None on miss."""
        if not recruiter_id:
            return None
        cache_key = _cache.recruiter_prefs_key(recruiter_id)
        if use_cache:
            cached = await _cache.get(cache_key)
            if cached is not None:
                return cached
        try:
            row = await self.pool.fetchrow(
                """SELECT prioritize_recent_experience, prioritize_exact_skill_match,
                          diversity, weight_overrides_json
                   FROM recruiter_preferences WHERE recruiter_id = $1::uuid""",
                recruiter_id,
            )
        except Exception as exc:
            logger.debug("recruiter_preferences fetch failed: %s", exc)
            return None
        if not row:
            return None
        overrides = row["weight_overrides_json"]
        if isinstance(overrides, str):
            try:
                import json
                overrides = json.loads(overrides)
            except Exception:
                overrides = {}
        prefs = {
            "prioritize_recent_experience": row["prioritize_recent_experience"],
            "prioritize_exact_skill_match": row["prioritize_exact_skill_match"],
            "diversity":                    row["diversity"],
            "weight_overrides_json":        overrides or {},
        }
        if use_cache:
            await _cache.set(cache_key, prefs)
        return prefs

    def _get_reranker(self) -> Reranker:
        key = settings.reranker_model or "local-fast"
        if key not in self._reranker_cache:
            self._reranker_cache[key] = get_reranker(key)
        return self._reranker_cache[key]

    def _release_reranker(self, reranker: Reranker) -> None:
        if not settings.evict_local_reranker_after_use:
            return
        key = settings.reranker_model or "local-fast"
        if self._reranker_cache.get(key) is reranker:
            del self._reranker_cache[key]
            import gc
            gc.collect()

    async def _log_impressions(
        self,
        results: list[SearchResult],
        search_id: str = "",
        recruiter_id: str | None = None,
        langfuse_trace_id: str | None = None,
    ) -> None:
        try:
            records = [
                (r.impression_id or str(uuid.uuid4()), search_id or None, recruiter_id or None,
                 r.candidate_id, i + 1, r.feature_score, langfuse_trace_id)
                for i, r in enumerate(results)
            ]
            await self.pool.executemany(
                """INSERT INTO search_impressions (id, search_id, recruiter_id, candidate_id, position, final_score, langfuse_trace_id)
                   VALUES ($1, $2::uuid, $3::uuid, $4::uuid, $5, $6, $7)
                   ON CONFLICT DO NOTHING""",
                records,
            )
        except Exception as exc:
            logger.debug("Impression logging skipped: %s", exc)

    async def count_filtered_candidates(self, filters: Any = None) -> int:
        """Backward-compat helper used by old API endpoints."""
        sql = "SELECT COUNT(*) FROM candidates WHERE status = 'active'"
        return await self.pool.fetchval(sql)

    def _build_where_clause(self, filters: SearchFilters) -> tuple[str, list[Any]]:
        """Legacy filter builder kept for older API helpers and tests."""
        clauses: list[str] = []
        params: list[Any] = []
        idx = 1

        status = filters.status or ["active"]
        if len(status) == 1:
            clauses.append(f"status = ${idx}")
            params.append(status[0])
        else:
            clauses.append(f"status = ANY(${idx}::text[])")
            params.append(status)
        idx += 1

        if filters.country:
            clauses.append(f"LOWER(country) = LOWER(${idx})")
            params.append(filters.country)
            idx += 1
        if filters.city:
            clauses.append(f"LOWER(city) = LOWER(${idx})")
            params.append(filters.city)
            idx += 1
        if filters.location:
            clauses.append(
                f"(LOWER(location) LIKE LOWER(${idx}) OR "
                f"LOWER(city) LIKE LOWER(${idx}) OR "
                f"LOWER(country) LIKE LOWER(${idx}))"
            )
            params.append(f"%{filters.location}%")
            idx += 1
        if filters.min_age is not None:
            clauses.append(f"age >= ${idx}")
            params.append(filters.min_age)
            idx += 1
        if filters.max_age is not None:
            clauses.append(f"age <= ${idx}")
            params.append(filters.max_age)
            idx += 1
        if filters.skills:
            clauses.append(
                f"skills @> ${idx}::text[]"
                if filters.skills_match == "and"
                else f"skills && ${idx}::text[]"
            )
            params.append(filters.skills)
            idx += 1
        if filters.interests:
            clauses.append(
                f"interests @> ${idx}::text[]"
                if filters.interests_match == "and"
                else f"interests && ${idx}::text[]"
            )
            params.append(filters.interests)
            idx += 1
        if filters.min_years_exp is not None:
            clauses.append(f"years_exp >= ${idx}")
            params.append(filters.min_years_exp)
            idx += 1
        if filters.max_years_exp is not None:
            clauses.append(f"years_exp <= ${idx}")
            params.append(filters.max_years_exp)
            idx += 1
        if filters.min_salary is not None:
            clauses.append(f"salary_max >= ${idx}")
            params.append(filters.min_salary)
            idx += 1
        if filters.max_salary is not None:
            clauses.append(f"salary_min <= ${idx}")
            params.append(filters.max_salary)

        return " AND ".join(clauses), params


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_doc_types(targets: list[str]) -> list[str]:
    doc_types: list[str] = []
    for t in targets:
        doc_types.extend(SEARCH_TARGET_DOC_TYPES.get(t, []))
    return list(set(doc_types))


def _candidate_profile_payload(row: Any) -> dict[str, Any]:
    data = dict(row)
    return {
        "candidate_id": str(data["candidate_id"]),
        "full_name": data.get("full_name") or "",
        "email": data.get("email"),
        "city": data.get("city"),
        "country": data.get("country"),
        "salary_min": data.get("salary_min"),
        "salary_max": data.get("salary_max"),
        "years_exp": int(data.get("years_exp") or 0),
        "skills": list(data.get("skills") or []),
    }


def _apply_candidate_profile(result: SearchResult, profile: Any) -> None:
    data = dict(profile)
    result.full_name = data.get("full_name") or ""
    result.email = data.get("email")
    result.city = data.get("city")
    result.country = data.get("country")
    result.salary_min = data.get("salary_min")
    result.salary_max = data.get("salary_max")
    result.years_exp = int(data.get("years_exp") or 0)
    result.skills = list(data.get("skills") or [])
    result.similarity_score = 1.0 - result.best_chunk_distance


def _chunk_payload(row: Any) -> dict[str, Any]:
    data = dict(row)
    return {
        "chunk_id": str(data["chunk_id"]),
        "content": data.get("content") or "",
        "document_id": data.get("document_id"),
        "doc_type": data.get("doc_type"),
        "document_title": data.get("document_title"),
    }


def _merge_hydrated_rows(
    selected: list[dict[str, Any]],
    by_chunk_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    hydrated: list[dict[str, Any]] = []
    for row in selected:
        item = dict(row)
        match = by_chunk_id.get(row.get("chunk_id"))
        if match:
            item["content"] = match.get("content") or ""
            item["document_id"] = match.get("document_id")
            item["doc_type"] = match.get("doc_type")
            item["document_title"] = match.get("document_title")
        hydrated.append(item)
    return hydrated


def _row_to_result(row: Any) -> SearchResult:
    return SearchResult(
        candidate_id      = str(row["candidate_id"]),
        full_name         = row.get("full_name") or "",
        email             = row.get("email"),
        city              = row.get("city"),
        country           = row.get("country"),
        salary_min        = row.get("salary_min"),
        salary_max        = row.get("salary_max"),
        years_exp         = int(row.get("years_exp") or 0),
        skills            = list(row.get("skills") or []),
        best_chunk        = row.get("best_chunk") or "",
    )


async def _empty() -> list:
    return []


async def _call_with_optional_use_cache(fn, *args, use_cache: bool = True):
    """Call cache-aware helpers without breaking older test doubles."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return await fn(*args, use_cache=use_cache)
    accepts_cache = "use_cache" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if accepts_cache:
        return await fn(*args, use_cache=use_cache)
    return await fn(*args)


def _apply_dynamic_retrieval_limits(
    cfg: dict[str, Any],
    top_k: int,
    override_keys: set[str],
) -> dict[str, Any]:
    """Shrink broad retrieval knobs for small result requests.

    Explicit per-request overrides win so benchmarking/admin tooling can still
    ask for large retrieval pools.
    """
    tuned = dict(cfg)
    if not tuned.get("use_dynamic_retrieval_limits", True):
        return tuned

    final_k = max(1, int(top_k or tuned.get("final_top_k") or 25))
    recall_factor = 8 if tuned.get("use_cross_encoder") else 6

    def _cap_int(key: str, target: int) -> None:
        if key in override_keys:
            return
        current = int(tuned.get(key) or target)
        tuned[key] = max(0, min(current, target))

    _cap_int("dense_top_k", max(30, final_k * recall_factor))
    _cap_int("bm25_top_k", max(30, final_k * recall_factor))
    _cap_int("skill_top_k", max(20, final_k * 4))

    if "cross_encoder_top_k" not in override_keys:
        current_ce = int(tuned.get("cross_encoder_top_k") or 0)
        ce_target = max(final_k, min(max(10, final_k * 3), final_k * 5))
        tuned["cross_encoder_top_k"] = max(0, min(current_ce, ce_target))

    if "chunk_hydration_top_k" not in override_keys:
        retrieval_total = sum(
            int(tuned.get(key) or 0)
            for enabled, key in (
                (tuned.get("use_dense"), "dense_top_k"),
                (tuned.get("use_bm25"), "bm25_top_k"),
                (tuned.get("use_skill_exact"), "skill_top_k"),
            )
            if enabled
        )
        hydrate_target = max(
            10,
            final_k * 2,
            int(tuned.get("cross_encoder_top_k") or 0) * 2,
        )
        tuned["chunk_hydration_top_k"] = (
            min(retrieval_total, hydrate_target)
            if retrieval_total > 0 else hydrate_target
        )

    if "candidate_enrichment_top_k" not in override_keys:
        retrieval_total = sum(
            int(tuned.get(key) or 0)
            for enabled, key in (
                (tuned.get("use_dense"), "dense_top_k"),
                (tuned.get("use_bm25"), "bm25_top_k"),
                (tuned.get("use_skill_exact"), "skill_top_k"),
            )
            if enabled
        )
        visible_k = max(1, int(tuned.get("visible_enrichment_top_k") or 10))
        current = int(tuned.get("candidate_enrichment_top_k") or 0)
        enrichment_target = max(final_k * 3, visible_k * 2, final_k)
        if current > 0:
            enrichment_target = min(current, enrichment_target)
        tuned["candidate_enrichment_top_k"] = (
            min(retrieval_total, enrichment_target)
            if retrieval_total > 0 else enrichment_target
        )

    return tuned


_FILTER_KEYS_THAT_MATTER = (
    "skills", "country", "city", "min_years_exp", "max_years_exp",
    "min_salary", "max_salary", "applied_role",
)


def _has_meaningful_must_values(must) -> bool:
    """True if MustFilters carries any real recruiter intent.

    Status defaults to ['active'] for every search and shouldn't on its own
    count as a meaningful filter. Used by the partial-extraction fallback to
    decide whether a clarify response is worth blocking on or can be demoted
    to a soft notice over real results.
    """
    if not must:
        return False
    if must.skills:                       return True
    if must.country:                      return True
    if must.city:                         return True
    if must.applied_role:                 return True
    if must.min_years_exp is not None:    return True
    if must.max_years_exp is not None:    return True
    if must.min_salary    is not None:    return True
    if must.max_salary    is not None:    return True
    return False


def _has_filter_values(explicit_filters: dict[str, Any] | None) -> bool:
    """True if the caller supplied any real filter (skills, location, exp, etc.).

    Used to decide whether an empty-query request still has enough to run
    against — in which case we skip the LLM planner and go straight to a
    SQL filter-only search.
    """
    if not explicit_filters:
        return False
    for key in _FILTER_KEYS_THAT_MATTER:
        value = explicit_filters.get(key)
        if value in (None, "", [], {}):
            continue
        return True
    return False


def _spec_from_explicit_filters(explicit_filters: dict[str, Any]) -> CanonicalSearchSpec:
    """Build a minimal spec directly from caller filters, bypassing the LLM.

    Marks the spec as filters_only so the intent router and downstream
    short-circuits both treat it as a structured query.
    """
    from pipeline.spec import MustFilters, ShouldFilters, MustNotFilters
    from pipeline.validator import _merge_explicit

    must = MustFilters()
    spec = CanonicalSearchSpec(
        input_type     = "filters_only",
        intent         = "candidate_filter",
        must           = must,
        should         = ShouldFilters(),
        must_not       = MustNotFilters(),
        semantic_query = "",
        hyde_profile   = None,
        lexical_terms  = [],
        search_targets = ["candidate_profile", "resume_chunks"],
        confidence     = 1.0,
        used_fallback  = False,
    )
    spec = _merge_explicit(spec, explicit_filters)
    return spec


def _auto_relax_spec(spec: CanonicalSearchSpec) -> tuple[CanonicalSearchSpec | None, list[dict[str, Any]]]:
    """Return one relaxed retry spec for empty-result direct searches.

    This intentionally performs at most one relaxation so latency stays bounded
    and the caller can clearly explain what changed.
    """
    if len(spec.must.skills) > 2 and getattr(spec.must, "skills_match", "and") == "and":
        relaxed = dataclasses.replace(
            spec,
            must=dataclasses.replace(spec.must, skills_match="or"),
        )
        return relaxed, [{
            "field": "skills_match",
            "from": "and",
            "to": "or",
            "reason": "no_results_with_all_required_skills",
        }]

    if spec.must.city or spec.must.country:
        original_locations = [value for value in [spec.must.city, spec.must.country] if value]
        should_locations = list(spec.should.locations or [])
        for location in original_locations:
            if location not in should_locations:
                should_locations.append(location)
        relaxed = dataclasses.replace(
            spec,
            must=dataclasses.replace(spec.must, city=None, country=None),
            should=dataclasses.replace(spec.should, locations=should_locations),
        )
        return relaxed, [{
            "field": "location",
            "from": {
                "city": spec.must.city,
                "country": spec.must.country,
            },
            "to": "soft_preference",
            "reason": "no_results_with_strict_location_filter",
        }]

    return None, []


def _query_terms(query_text: str) -> set[str]:
    terms: set[str] = set()
    for token in _TOKEN_RE.findall(query_text.lower()):
        if token in _QUERY_STOPWORDS or len(token) < 3:
            continue
        terms.add(token)
        if token.endswith("s") and len(token) > 4:
            terms.add(token[:-1])
    return terms


def _text_terms(values: list[Any]) -> set[str]:
    terms: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, list):
            terms.update(_text_terms(value))
            continue
        for token in _TOKEN_RE.findall(str(value).lower()):
            terms.add(token)
            if token.endswith("s") and len(token) > 4:
                terms.add(token[:-1])
    return terms


def _lexical_score(query_terms: set[str], values: list[Any]) -> float:
    if not query_terms:
        return 0.0
    terms = _text_terms(values)
    return sum(1.0 for term in query_terms if term in terms)


def _candidate_lexical_score(query_terms: set[str], candidate: dict[str, Any]) -> float:
    if not query_terms:
        return 0.0
    values: list[Any] = [
        candidate.get("full_name"),
        candidate.get("city"),
        candidate.get("country"),
        candidate.get("skills", []),
    ]
    for chunk in candidate.get("chunks", []):
        values.extend([
            chunk.get("content"),
            chunk.get("doc_type"),
            chunk.get("document_title"),
        ])
    skill_score = _lexical_score(query_terms, candidate.get("skills", [])) * 2.0
    return skill_score + _lexical_score(query_terms, values)
