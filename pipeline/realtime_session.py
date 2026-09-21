"""Session-scoped realtime agent harness for Talent Atlas.

Gemini Live owns the audio turn while this module owns search state, bounded
tools, cancellation, retrieval reuse and the evidence graph.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from pipeline.realtime_coordinator import BranchResult, RealtimeCoordinator
from pipeline.realtime_events import EventEnvelope, TraceGraph
from pipeline.realtime_plan import SearchPlanRevision
from pipeline.realtime_tools import RealtimeToolDispatcher
from pipeline.search_result import SearchResult


CandidateLoader = Callable[[list[str]], Awaitable[dict[str, Any]]]


class StreamingTraceGraph(TraceGraph):
    def __init__(self, *, session_id: str, queue: asyncio.Queue[EventEnvelope]) -> None:
        super().__init__(session_id=session_id)
        self.queue = queue

    def emit(self, *args: Any, **kwargs: Any) -> EventEnvelope:
        event = super().emit(*args, **kwargs)
        self.queue.put_nowait(event)
        return event


class HybridBranchRunner:
    """Run each retrieval path independently so revisions can reuse branches."""

    def __init__(self, engine: Any, *, branch_top_k: int = 80, sql_limit: int = 500) -> None:
        self.engine = engine
        self.branch_top_k = branch_top_k
        self.sql_limit = sql_limit

    async def __call__(self, branch: str, plan: SearchPlanRevision) -> BranchResult:
        if branch == "sql":
            return await self._run_sql(plan)
        if branch == "skills" and not (plan.must_skills or plan.should_skills):
            return BranchResult(branch=branch, candidates=[], details={
                "restrictive": False,
                "reason": "no_explicit_skills",
                "config": self._config(branch),
            })

        explicit_filters: dict[str, Any] | None = None
        query = plan.query
        if branch == "skills":
            explicit_filters = {
                "skills": plan.must_skills,
                "skills_match": "and",
                "should": {"skills": plan.should_skills},
            }
            query = " ".join(plan.must_skills + plan.should_skills) or plan.query

        config = self._config(branch)
        response = await self.engine.smart_search(
            query=query,
            explicit_filters=explicit_filters,
            mode="no-llm",
            top_k=self.branch_top_k,
            config_overrides=config,
        )
        candidates = [_serialize_result(result) for result in response.results]
        return BranchResult(
            branch=branch,
            candidates=candidates,
            details={
                "restrictive": branch == "skills" and bool(plan.must_skills),
                "config": config,
                "retrieval_policy": getattr(response, "retrieval_policy", {}) or {},
                "bounded_limit": self.branch_top_k,
            },
        )

    async def _run_sql(self, plan: SearchPlanRevision) -> BranchResult:
        restrictive = any((
            plan.city,
            plan.country,
            plan.min_years_exp is not None,
            plan.max_years_exp is not None,
            plan.status,
            plan.excluded_skills,
        ))
        if not restrictive:
            return BranchResult(branch="sql", candidates=[], details={
                "restrictive": False,
                "reason": "no_structured_constraints",
                "bounded_limit": self.sql_limit,
            })
        filters = {
            key: value
            for key, value in {
                "city": plan.city,
                "country": plan.country,
                "min_years_exp": plan.min_years_exp,
                "max_years_exp": plan.max_years_exp,
                "status": plan.status,
            }.items()
            if value is not None
        }
        response = await self.engine.smart_search(
            query="",
            explicit_filters=filters,
            mode="no-llm",
            top_k=self.sql_limit,
            config_overrides=self._config("sql"),
        )
        candidates = [_serialize_result(result) for result in response.results]
        if plan.excluded_skills:
            excluded = set(plan.excluded_skills)
            candidates = [
                candidate for candidate in candidates
                if not excluded.intersection(str(skill).casefold() for skill in candidate.get("skills", []))
            ]
        return BranchResult(branch="sql", candidates=candidates, details={
            "restrictive": True,
            "filters": filters,
            "excluded_skills": plan.excluded_skills,
            "bounded_limit": self.sql_limit,
        })

    @staticmethod
    def _config(branch: str) -> dict[str, Any]:
        return {
            "use_llm_planner": False,
            "use_planner_fallback": True,
            "use_fallback_repair": False,
            "use_dense": branch == "vector",
            "use_bm25": branch == "bm25",
            "use_skill_exact": branch == "skills",
            "use_sql_filter": branch == "sql",
            "use_cross_encoder": False,
            "use_feature_ranker": False,
            "use_mmr": False,
            "use_auto_relax": False,
            "use_impression_logging": False,
            "use_dynamic_retrieval_limits": False,
            "keyword_policy": "force" if branch == "bm25" else "skip",
        }


class RealtimeAgentSession:
    """Own one interruptible conversation and its immutable search revisions."""

    def __init__(
        self,
        engine: Any,
        *,
        session_id: str | None = None,
        candidate_loader: CandidateLoader | None = None,
    ) -> None:
        self.engine = engine
        self.session_id = session_id or f"rt_{uuid.uuid4().hex}"
        self.events: asyncio.Queue[EventEnvelope] = asyncio.Queue()
        self.graph = StreamingTraceGraph(session_id=self.session_id, queue=self.events)
        self.coordinator = RealtimeCoordinator(
            HybridBranchRunner(engine),
            session_id=self.session_id,
            graph=self.graph,
        )
        self.current_plan: SearchPlanRevision | None = None
        self.current_candidates: list[dict[str, Any]] = []
        self.answer_format = "brief"
        self.candidate_loader = candidate_loader or self._default_candidate_loader
        self.tools = RealtimeToolDispatcher({
            "search_candidates": self._search_candidates,
            "revise_search": self._revise_search,
            "inspect_candidate": self._inspect_candidate,
            "compare_candidates": self._compare_candidates,
            "format_current_answer": self._format_current_answer,
            "cancel_current_action": self._cancel_current_action,
        })

    async def _search_candidates(self, arguments: dict[str, Any]) -> dict[str, Any]:
        top_k = int(arguments.pop("top_k", 8))
        revision = SearchPlanRevision.create(**arguments)
        return await self._execute_revision(revision, top_k=top_k)

    async def _revise_search(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.current_plan is None:
            raise ValueError("There is no active search to revise")
        top_k = int(arguments.pop("top_k", len(self.current_candidates) or 8))
        revision = self.current_plan.patch(**arguments)
        return await self._execute_revision(revision, top_k=top_k)

    async def _execute_revision(
        self,
        revision: SearchPlanRevision,
        *,
        top_k: int,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        outcome = await self.coordinator.execute(revision)
        candidates = outcome.candidates[:max(1, min(top_k, 20))]
        candidates, rerank_details = await self._rerank(revision.query, candidates)
        rerank_id = f"rerank-{revision.revision_id}"
        self.graph.upsert_node(
            node_id=rerank_id,
            kind="rerank",
            status="completed",
            revision_id=revision.revision_id,
            parent_ids=[f"fusion-{revision.revision_id}"],
            details={**rerank_details, "candidate_count": len(candidates), "candidates": candidates},
        )
        evidence = [
            {
                "candidate_id": candidate.get("candidate_id"),
                "name": candidate.get("name"),
                "evidence": candidate.get("best_evidence") or candidate.get("best_chunk") or "",
                "retrieval_paths": candidate.get("retrieval_paths", []),
            }
            for candidate in candidates
        ]
        ground_id = f"ground-{revision.revision_id}"
        self.graph.upsert_node(
            node_id=ground_id,
            kind="ground",
            status="completed",
            revision_id=revision.revision_id,
            parent_ids=[rerank_id],
            details={"candidate_count": len(candidates), "evidence": evidence, "candidates": candidates},
        )
        self.graph.upsert_node(
            node_id=f"answer-{revision.revision_id}",
            kind="answer",
            status="completed",
            revision_id=revision.revision_id,
            parent_ids=[ground_id],
            details={
                "format": self.answer_format,
                "candidate_count": len(candidates),
                "candidate_ids": [candidate.get("candidate_id") for candidate in candidates],
                "instruction": "Answer only from the bounded grounded candidate payload returned by this tool.",
            },
        )
        self.current_plan = revision
        self.current_candidates = candidates
        return {
            "revision_id": revision.revision_id,
            "parent_revision_id": revision.parent_revision_id,
            "plan": revision.model_dump(mode="json", exclude={"branch_fingerprints"}),
            "candidates": candidates,
            "count": len(candidates),
            "reused_branches": sorted(outcome.reused),
            "executed_branches": sorted(outcome.executed),
            "failed_branches": sorted(outcome.failed),
            "degraded": outcome.degraded,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "answer_rule": "Use only the evidence in this tool response; state when evidence is missing.",
        }

    async def _rerank(
        self,
        query: str,
        candidates: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not candidates or not hasattr(self.engine, "_get_reranker"):
            return candidates, {"applied": False, "reason": "reranker_unavailable_or_empty"}
        try:
            reranker = self.engine._get_reranker()
            objects = [_candidate_to_result(candidate) for candidate in candidates]
            reranked = await reranker.rerank(query, objects)
            by_id = {candidate["candidate_id"]: candidate for candidate in candidates}
            ordered: list[dict[str, Any]] = []
            for result in reranked:
                candidate = dict(by_id[str(result.candidate_id)])
                candidate["rerank_score"] = result.rerank_score
                ordered.append(candidate)
            return ordered, {
                "applied": True,
                "model": type(reranker).__name__,
                "query": query,
            }
        except Exception as exc:
            return candidates, {
                "applied": False,
                "degraded": True,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }

    async def _inspect_candidate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.candidate_loader([arguments["candidate_id"]])
        result["include_documents"] = bool(arguments.get("include_documents", True))
        return result

    async def _compare_candidates(self, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.candidate_loader(arguments["candidate_ids"])
        return {
            "focus": arguments.get("focus"),
            "candidates": result.get("candidates", []),
            "comparison_rule": "Compare only returned profile fields and evidence; do not infer protected traits.",
        }

    async def _format_current_answer(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.answer_format = arguments["format"]
        return {
            "format": self.answer_format,
            "candidates": self.current_candidates,
            "retrieval_rerun": False,
        }

    async def _cancel_current_action(self, arguments: dict[str, Any]) -> dict[str, Any]:
        await self.coordinator.close()
        return {"cancelled": True, "reason": arguments.get("reason", "user_requested")}

    async def _default_candidate_loader(self, candidate_ids: list[str]) -> dict[str, Any]:
        pool = getattr(self.engine, "pool", None)
        if pool is None:
            candidates = [
                candidate for candidate in self.current_candidates
                if candidate.get("candidate_id") in set(candidate_ids)
            ]
            return {"candidates": candidates, "count": len(candidates)}
        from pipeline.agent_tools import do_get_candidate_details

        return await do_get_candidate_details(pool, candidate_ids, limit=len(candidate_ids))

    def snapshot(self) -> dict[str, Any]:
        return self.graph.snapshot()

    async def close(self) -> None:
        await self.coordinator.close()


def _serialize_result(result: Any) -> dict[str, Any]:
    explanation = getattr(result, "explanation", None) or {}
    return {
        "candidate_id": str(getattr(result, "candidate_id", "")),
        "name": getattr(result, "full_name", "") or "",
        "city": getattr(result, "city", None),
        "country": getattr(result, "country", None),
        "years_exp": getattr(result, "years_exp", 0),
        "skills": list(getattr(result, "skills", []) or []),
        "best_evidence": (explanation.get("best_evidence") or getattr(result, "best_chunk", "") or "")[:1200],
        "best_chunk": (getattr(result, "best_chunk", "") or "")[:1200],
        "supporting_chunks": list(getattr(result, "supporting_chunks", []) or [])[:3],
        "similarity_score": round(float(getattr(result, "similarity_score", 0.0) or 0.0), 6),
        "rrf_score": round(float(getattr(result, "fused_rrf_score", 0.0) or 0.0), 8),
        "feature_score": round(float(getattr(result, "feature_score", 0.0) or 0.0), 4),
        "rerank_score": getattr(result, "rerank_score", None),
        "retrieval_paths": list(getattr(result, "retrieval_paths", []) or []),
        "sort_basis": getattr(result, "sort_basis", "fused_rrf_score"),
        "doc_type": getattr(result, "doc_type", "") or "",
        "document_title": getattr(result, "document_title", None),
    }


def _candidate_to_result(candidate: dict[str, Any]) -> SearchResult:
    return SearchResult(
        candidate_id=str(candidate.get("candidate_id") or ""),
        full_name=str(candidate.get("name") or ""),
        city=candidate.get("city"),
        country=candidate.get("country"),
        years_exp=int(candidate.get("years_exp") or 0),
        skills=list(candidate.get("skills") or []),
        best_chunk=str(candidate.get("best_chunk") or candidate.get("best_evidence") or ""),
        supporting_chunks=list(candidate.get("supporting_chunks") or []),
        fused_rrf_score=float(candidate.get("fusion_score") or candidate.get("rrf_score") or 0.0),
        similarity_score=float(candidate.get("similarity_score") or 0.0),
    )
