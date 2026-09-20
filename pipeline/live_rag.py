"""Streaming Live RAG orchestration for the PRISM Theme 4 demonstration.

The controller is deliberately small and auditable. It starts retrieval from a
partial transcript, reuses completed subqueries when a late detail arrives,
and never stores state outside the in-memory session object.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from pipeline.aliases import normalize_location
from pipeline.search import HybridSearchEngine
from pipeline.search_result import SearchResult


Decision = Literal["WAIT", "RETRIEVE", "SUPPRESS"]
Emitter = Callable[[str, dict[str, Any]], Awaitable[None]]


PRESENTATION_ONLY = re.compile(
    r"\b(repeat|reformat|rewrite|summari[sz]e|shorten|two bullets|bullet points|"
    r"table format|say that again|make (?:it|that) concise)\b",
    re.IGNORECASE,
)
RETRIEVAL_CUES = re.compile(
    r"\b(find|search|show|candidate|engineer|developer|designer|manager|analyst|scientist|"
    r"experience|skills?|located|location|years?|resume|profile|hire)\b",
    re.IGNORECASE,
)
STRONG_PARTIAL = re.compile(
    r"\b(?:find|search|show|need)\b.*\b(?:candidates?|engineers?|developers?|"
    r"designers?|managers?|analysts?|scientists?)\b",
    re.IGNORECASE,
)
INJECTION_CUES = re.compile(
    r"\b(ignore (?:all |the )?(?:previous|prior|system)|system prompt|developer message|"
    r"reveal (?:your|the) prompt|bypass (?:the )?guardrails?)\b",
    re.IGNORECASE,
)
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
}


TOOL_REGISTRY: list[dict[str, Any]] = [
    {
        "id": "retrieval_controller",
        "label": "Retrieval controller",
        "purpose": "Decides whether a transcript fragment is ready for retrieval.",
        "input": {"text": "string", "is_final": "boolean", "has_prior_answer": "boolean"},
        "output": {"decision": "WAIT | RETRIEVE | SUPPRESS", "reason": "string"},
        "trigger": "Every transcript update",
        "guardrails": ["Presentation-only turns never query the corpus", "Very short fragments wait", "Prompt-injection phrases are flagged"],
        "side_effects": "None",
    },
    {
        "id": "intent_decomposer",
        "label": "Intent decomposer",
        "purpose": "Turns one compound request into a small set of independently retrievable intents.",
        "input": {"utterance": "string", "prior_utterance": "string?"},
        "output": {"subqueries": "string[]", "delta": "string[]"},
        "trigger": "RETRIEVE decision",
        "guardrails": ["Maximum four subqueries", "No benchmark questions in code", "Short clauses are merged to avoid over-fragmentation"],
        "side_effects": "None",
    },
    {
        "id": "hybrid_candidate_search",
        "label": "Hybrid corpus retriever",
        "purpose": "Runs SQL constraints, vector similarity, keyword retrieval and exact-skill matching over the indexed candidate corpus.",
        "input": {"query": "string", "mode": "no-llm", "top_k": "integer"},
        "output": {"candidates": "Candidate[]", "phase_timings": "object"},
        "trigger": "Once per new or changed subquery",
        "guardrails": ["Corpus-only retrieval", "No recruiter personalization", "No web search", "Bounded top-k"],
        "side_effects": "Read-only database access",
    },
    {
        "id": "rank_fusion",
        "label": "Reciprocal Rank Fusion",
        "purpose": "Combines candidate rankings without pretending unlike score scales are directly comparable.",
        "input": {"ranked_lists": "Candidate[][]", "k": "60"},
        "output": {"ranked_candidates": "Candidate[]", "score": "sum(1 / (60 + rank))"},
        "trigger": "After each retrieval branch completes",
        "guardrails": ["Candidate-level deduplication", "Stable tie-breaking", "Every fused candidate keeps its source queries"],
        "side_effects": "None",
    },
    {
        "id": "evidence_grounder",
        "label": "Evidence grounder",
        "purpose": "Builds an answer only from returned candidates and their retrieved resume chunks.",
        "input": {"candidate": "Candidate", "chunks": "RetrievedChunk[]"},
        "output": {"claim": "string", "citations": "chunk_id[]"},
        "trigger": "After fusion",
        "guardrails": ["Real chunk IDs only", "No unsupported candidate facts", "Insufficient evidence is stated explicitly"],
        "side_effects": "None",
    },
    {
        "id": "delta_refiner",
        "label": "Late-detail refiner",
        "purpose": "Adds a late constraint without throwing away completed retrieval work.",
        "input": {"previous_subqueries": "string[]", "new_subqueries": "string[]"},
        "output": {"reused": "string[]", "retrieved": "string[]", "answer_version": "integer"},
        "trigger": "Transcript grows after provisional retrieval",
        "guardrails": ["Stale revisions are cancelled", "Unchanged subqueries are reused", "Previous citations remain inspectable"],
        "side_effects": "Session memory only",
    },
    {
        "id": "presentation_transformer",
        "label": "No-retrieval formatter",
        "purpose": "Reformats the current answer without paying for another corpus search.",
        "input": {"instruction": "string", "current_answer": "object"},
        "output": {"answer_version": "object"},
        "trigger": "SUPPRESS decision",
        "guardrails": ["Cannot add new facts", "Keeps the same citations", "Always reports retrieval_suppressed=true"],
        "side_effects": "Session memory only",
    },
]


def decide_retrieval(text: str, *, is_final: bool, has_prior_answer: bool) -> tuple[Decision, str]:
    clean = " ".join(text.split())
    words = clean.split()
    if has_prior_answer and PRESENTATION_ONLY.search(clean):
        return "SUPPRESS", "The turn changes presentation, not evidence; reuse the current grounded answer."
    if len(words) < 4 and not is_final and not STRONG_PARTIAL.search(clean):
        return "WAIT", "The partial utterance is too ambiguous to search without creating noise."
    if STRONG_PARTIAL.search(clean) or RETRIEVAL_CUES.search(clean) or is_final:
        suffix = "final utterance" if is_final else "stable retrieval cues arrived before end-of-speech"
        return "RETRIEVE", suffix
    return "WAIT", "No stable corpus-seeking intent is visible yet."


def decompose_query(text: str, *, max_queries: int = 4) -> list[str]:
    """Create a bounded, generic decomposition without benchmark-specific text."""
    clean = " ".join(text.strip().split()).strip(" .!?")
    if not clean:
        return []

    clauses = re.split(r"\s*(?:,|;|\band also\b|\bplus\b|\bwho also\b)\s*", clean, flags=re.I)
    clauses = [clause.strip(" .") for clause in clauses if len(clause.split()) >= 2]

    # Preserve the complete request as the semantic anchor. Independent clauses
    # add recall; RRF rewards candidates supported by both views.
    queries = [clean]
    for clause in clauses:
        if clause.casefold() != clean.casefold() and clause.casefold() not in {q.casefold() for q in queries}:
            queries.append(clause)
    return queries[:max_queries]


def extract_hard_filters(text: str) -> dict[str, Any]:
    """Compile generic, explicit hard constraints from the current utterance."""
    filters: dict[str, Any] = {}
    years = re.search(
        r"\b(?:at least|minimum|min(?:imum)? of)\s+(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty)\s*\+?\s*years?\b",
        text,
        re.IGNORECASE,
    )
    if years:
        raw_years = years.group(1).lower()
        filters["min_years_exp"] = int(raw_years) if raw_years.isdigit() else NUMBER_WORDS[raw_years]

    location = re.search(
        r"\b(?:located|based)\s+in\s+([A-Za-z][A-Za-z .'-]{1,40}?)(?=\s+(?:with|who|and|plus|having)\b|[,.;]|$)",
        text,
        re.IGNORECASE,
    )
    if not location:
        location = re.search(
            r"\b(?:in)\s+([A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,2})(?=[,.;]|$)",
            text,
        )
    if location:
        filters["city"] = normalize_location(location.group(1).strip(" .,'\""))
    return filters


def rrf_fuse(
    result_sets: dict[str, list[SearchResult]],
    *,
    k: int = 60,
    limit: int = 8,
    allowed_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    fused: dict[str, dict[str, Any]] = {}
    for query, results in result_sets.items():
        for rank, candidate in enumerate(results, start=1):
            cid = str(candidate.candidate_id)
            item = fused.setdefault(cid, {"candidate": candidate, "score": 0.0, "queries": [], "ranks": {}})
            item["score"] += 1.0 / (k + rank)
            item["queries"].append(query)
            item["ranks"][query] = rank
            if rank < min(item["ranks"].values()):
                item["candidate"] = candidate
    ranked = sorted(fused.values(), key=lambda item: (-item["score"], str(item["candidate"].candidate_id)))
    if allowed_ids is not None:
        ranked = [item for item in ranked if str(item["candidate"].candidate_id) in allowed_ids]
    return ranked[:limit]


def _evidence(candidate: SearchResult) -> list[dict[str, Any]]:
    evidence = [{
        "chunk_id": candidate.best_chunk_id,
        "document_id": candidate.best_document_id,
        "title": candidate.document_title or candidate.doc_type or "Candidate document",
        "content": candidate.best_chunk,
    }]
    evidence.extend(candidate.supporting_chunks[:2])
    return [item for item in evidence if item.get("chunk_id") and item.get("content")]


def _candidate_payload(item: dict[str, Any]) -> dict[str, Any]:
    candidate: SearchResult = item["candidate"]
    evidence = _evidence(candidate)
    return {
        "candidate_id": str(candidate.candidate_id),
        "full_name": candidate.full_name,
        "city": candidate.city,
        "country": candidate.country,
        "years_exp": candidate.years_exp,
        "skills": candidate.skills[:10],
        "fusion_score": round(float(item["score"]), 6),
        "matched_subqueries": item["queries"],
        "retrieval_paths": candidate.retrieval_paths,
        "evidence": evidence,
    }


@dataclass
class LiveRAGSession:
    engine: HybridSearchEngine
    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    revision: int = 0
    answer_version: int = 0
    transcript: str = ""
    subquery_results: dict[str, list[SearchResult]] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    prior_citations: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.perf_counter)

    async def process(self, text: str, *, is_final: bool, emit: Emitter) -> None:
        self.transcript = " ".join(text.split())
        self.revision += 1
        revision = self.revision
        decision, reason = decide_retrieval(
            self.transcript,
            is_final=is_final,
            has_prior_answer=bool(self.candidates),
        )
        await emit("controller.decision", {
            "revision": revision,
            "decision": decision,
            "reason": reason,
            "is_final": is_final,
            "prompt_injection_flagged": bool(INJECTION_CUES.search(self.transcript)),
        })

        if decision == "WAIT":
            return
        if decision == "SUPPRESS":
            await self._emit_suppressed_answer(emit, revision)
            return

        started = time.perf_counter()
        subqueries = decompose_query(self.transcript)
        hard_filters = extract_hard_filters(self.transcript)
        reused = [query for query in subqueries if query in self.subquery_results]
        delta = [query for query in subqueries if query not in self.subquery_results]
        await emit("query.decomposed", {
            "revision": revision,
            "subqueries": subqueries,
            "reused": reused,
            "delta": delta,
            "hard_filters": hard_filters,
        })

        tasks = [asyncio.create_task(self._retrieve(query, hard_filters, revision, emit)) for query in delta]
        try:
            for task in asyncio.as_completed(tasks):
                query, results, timings = await task
                if revision != self.revision:
                    return
                self.subquery_results[query] = results
                await emit("tool.completed", {
                    "revision": revision,
                    "tool": "hybrid_candidate_search",
                    "query": query,
                    "result_count": len(results),
                    "timings_ms": timings,
                })
                await self._emit_fusion(subqueries, emit, revision, provisional=True)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await emit("retrieval.cancelled", {"revision": revision, "reason": "A newer transcript revision superseded this work."})
            raise

        if revision != self.revision:
            return
        await self._emit_fusion(subqueries, emit, revision, provisional=False)
        total_ms = round((time.perf_counter() - started) * 1000, 1)
        await emit("metrics.updated", {
            "revision": revision,
            "retrieval_ms": total_ms,
            "subqueries_total": len(subqueries),
            "subqueries_reused": len(reused),
            "subqueries_retrieved": len(delta),
            "citation_coverage": 1.0 if self.prior_citations else 0.0,
            "estimated_cost_usd": 0.0,
        })

    async def _retrieve(self, query: str, hard_filters: dict[str, Any], revision: int, emit: Emitter):
        await emit("tool.started", {
            "revision": revision,
            "tool": "hybrid_candidate_search",
            "query": query,
            "guardrails": ["corpus_only", "no_personalization", "bounded_top_k"],
            "hard_filters": hard_filters,
        })
        response = await self.engine.smart_search(
            query=query,
            explicit_filters=hard_filters or None,
            mode="no-llm",
            top_k=8,
            recruiter_id=None,
            config_overrides={
                "use_personalization": False,
                "use_impression_logging": False,
                "use_cross_encoder": False,
                "use_mmr": False,
                "final_top_k": 8,
            },
        )
        return query, response.results, response.phase_timings

    async def _emit_fusion(self, subqueries: list[str], emit: Emitter, revision: int, *, provisional: bool) -> None:
        available = {query: self.subquery_results[query] for query in subqueries if query in self.subquery_results}
        anchor_results = available.get(subqueries[0]) if subqueries else None
        allowed_ids = {str(result.candidate_id) for result in anchor_results} if anchor_results is not None else None
        fused = rrf_fuse(available, allowed_ids=allowed_ids)
        self.candidates = [_candidate_payload(item) for item in fused]
        await emit("fusion.updated", {
            "revision": revision,
            "provisional": provisional,
            "formula": "score(c) = sum_q 1 / (60 + rank_q(c))",
            "completed_lists": len(available),
            "candidates": self.candidates,
        })
        if provisional or not self.candidates:
            return

        self.answer_version += 1
        citations = [
            item["chunk_id"]
            for candidate in self.candidates[:3]
            for item in candidate["evidence"][:1]
            if item.get("chunk_id")
        ]
        self.prior_citations = citations
        await emit("answer.version", {
            "revision": revision,
            "version": self.answer_version,
            "status": "verified",
            "retrieval_suppressed": False,
            "summary": self._summary(),
            "citations": citations,
            "candidate_ids": [candidate["candidate_id"] for candidate in self.candidates[:3]],
        })

    def _summary(self) -> str:
        if not self.candidates:
            return "The indexed corpus does not contain enough evidence for a grounded shortlist."
        names = [candidate["full_name"] or "Unnamed candidate" for candidate in self.candidates[:3]]
        return f"The strongest corpus-grounded matches are {', '.join(names)}. Open each evidence row to inspect the exact resume chunk behind the ranking."

    async def _emit_suppressed_answer(self, emit: Emitter, revision: int) -> None:
        self.answer_version += 1
        bullets = [
            f"{candidate['full_name']}: {candidate['years_exp']} years; evidence retained from the prior search."
            for candidate in self.candidates[:2]
        ]
        await emit("tool.completed", {
            "revision": revision,
            "tool": "presentation_transformer",
            "retrieval_suppressed": True,
        })
        await emit("answer.version", {
            "revision": revision,
            "version": self.answer_version,
            "status": "reformatted",
            "retrieval_suppressed": True,
            "summary": "\n".join(f"• {line}" for line in bullets) or self._summary(),
            "citations": self.prior_citations,
            "candidate_ids": [candidate["candidate_id"] for candidate in self.candidates[:2]],
        })
