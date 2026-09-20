"""LLM-powered candidate insight generation for top search results."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pipeline import settings
from pipeline.prompts import get_insights_system_prompt
from pipeline.llm_models import (
    DEFAULT_LLM_MODELS as DEFAULT_LLM_MODELS,
    default_llm_model_for_provider,
    gemini_model_supports_thinking,
)

logger = logging.getLogger(__name__)

PLACEHOLDER_SUFFIX = "..."


@dataclass
class CandidateInsightInput:
    """Compact candidate evidence sent to the LLM."""

    candidate_id: str
    full_name: str
    city: str | None
    country: str | None
    salary_min: int | None
    salary_max: int | None
    years_exp: int | None
    skills: list[str]
    best_chunk: str
    similarity_score: float
    doc_type: str
    document_title: str | None
    supporting_chunks: list[dict[str, Any]] = field(default_factory=list)
    rerank_score: float | None = None
    rank_score: float | None = None


@dataclass
class AIInsightMatrixEntry:
    """One row in the AI evidence matrix."""

    candidate_id: str
    full_name: str
    fit_score: int
    recommendation: str
    strengths: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    interview_probe: str = ""
    grounding_notes: str = ""


@dataclass
class AIInsightResult:
    """Validated AI insight response."""

    status: Literal["ok", "unavailable", "error"]
    provider: str
    model: str
    best_candidate_id: str | None = None
    summary: str = ""
    comparative_reasoning: str = ""
    matrix: list[AIInsightMatrixEntry] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)
    error: str | None = None


class AIInsightService:
    """Generate a recruiter-facing best-fit readout from the current top results."""

    def __init__(
        self,
        provider: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 25.0,
        thinking_level: str | None = None,
        verify_grounding: bool | None = None,
    ):
        self.provider = provider or settings.llm_provider
        self.model = model or _default_model_for_provider(self.provider)
        self.timeout_seconds = timeout_seconds
        self.thinking_level = thinking_level
        self.verify_grounding = (
            settings.insights_verify_grounding
            if verify_grounding is None
            else verify_grounding
        )

    async def generate(
        self,
        query: str,
        filters_applied: dict[str, Any],
        candidates: list[CandidateInsightInput],
    ) -> AIInsightResult:
        """Return an AI insight, never raising for normal LLM/key failures."""
        start = time.perf_counter()
        if not candidates:
            return AIInsightResult(
                status="unavailable",
                provider=self.provider,
                model=self.model,
                summary="Run a search with matching candidates before requesting AI insight.",
                timings_ms={"total": _elapsed_ms(start)},
            )

        if not self._has_valid_key():
            return AIInsightResult(
                status="unavailable",
                provider=self.provider,
                model=self.model,
                summary=f"{self.provider.title()} is not configured for AI insights.",
                timings_ms={"total": _elapsed_ms(start)},
            )

        system_prompt, prompt_version, lf_prompt = get_insights_system_prompt()
        user_prompt = _user_prompt(query, filters_applied, candidates)
        try:
            llm_start = time.perf_counter()
            raw_response = await asyncio.wait_for(
                self._call_llm(system_prompt, user_prompt, lf_prompt=lf_prompt, prompt_version=prompt_version),
                timeout=self.timeout_seconds,
            )
            result = parse_ai_insight_response(
                raw_response,
                candidates,
                provider=self.provider,
                model=self.model,
            )
            if result.status != "ok":
                result = _search_based_fallback(
                    candidates,
                    provider=self.provider,
                    model=self.model,
                    error=result.summary or result.error or "invalid AI insight response",
                )
            result.timings_ms["llm"] = _elapsed_ms(llm_start)
            result.timings_ms["total"] = _elapsed_ms(start)
            return result
        except Exception as exc:
            error = "LLM insight call timed out" if isinstance(exc, TimeoutError) else str(exc)
            logger.warning("AI insight generation failed: %s", error)
            result = _search_based_fallback(
                candidates,
                provider=self.provider,
                model=self.model,
                error=error,
            )
            result.timings_ms["total"] = _elapsed_ms(start)
            return result

    def _has_valid_key(self) -> bool:
        if self.provider == "openai":
            return _looks_configured(settings.openai_api_key)
        if self.provider == "gemini":
            return _looks_configured(settings.google_api_key)
        if self.provider == "groq":
            return _looks_configured(settings.groq_api_key)
        return False

    async def _call_llm(self, system_prompt: str, user_prompt: str, lf_prompt: Any = None, prompt_version: int | None = None) -> str:
        if self.provider == "openai":
            return await self._call_openai(system_prompt, user_prompt, lf_prompt=lf_prompt, prompt_version=prompt_version)
        if self.provider == "gemini":
            return await self._call_gemini(system_prompt, user_prompt, lf_prompt=lf_prompt, prompt_version=prompt_version)
        if self.provider == "groq":
            return await self._call_groq(system_prompt, user_prompt, lf_prompt=lf_prompt, prompt_version=prompt_version)
        raise ValueError(f"Unsupported LLM provider: {self.provider}")

    async def _call_openai(self, system_prompt: str, user_prompt: str, lf_prompt: Any = None, prompt_version: int | None = None) -> str:
        from pipeline.observability import get_async_openai
        AsyncOpenAI = get_async_openai()
        supports_langfuse_name = getattr(AsyncOpenAI, "__module__", "").startswith("langfuse.")

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        create_kwargs = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 1200,
            "response_format": {"type": "json_object"},
        }
        if supports_langfuse_name:
            create_kwargs["name"] = "insights.generate"
        if lf_prompt is not None:
            create_kwargs["langfuse_prompt"] = lf_prompt
        response = await client.chat.completions.create(**create_kwargs)
        return response.choices[0].message.content or ""

    async def _call_groq(self, system_prompt: str, user_prompt: str, lf_prompt: Any = None, prompt_version: int | None = None) -> str:
        from pipeline.observability import get_async_openai
        AsyncOpenAI = get_async_openai()
        supports_langfuse_name = getattr(AsyncOpenAI, "__module__", "").startswith("langfuse.")

        client = AsyncOpenAI(api_key=settings.groq_api_key, base_url="https://api.groq.com/openai/v1")
        create_kwargs = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 1200,
            "response_format": {"type": "json_object"},
        }
        if supports_langfuse_name:
            create_kwargs["name"] = "insights.generate"
        if lf_prompt is not None:
            create_kwargs["langfuse_prompt"] = lf_prompt
        response = await client.chat.completions.create(**create_kwargs)
        return response.choices[0].message.content or ""

    async def _call_gemini(self, system_prompt: str, user_prompt: str, lf_prompt: Any = None, prompt_version: int | None = None) -> str:
        from google import genai
        from pipeline.observability import start_generation as _obs_start_generation, update_current_generation as _obs_update_current_generation

        client = genai.Client(api_key=settings.google_api_key)
        loop = asyncio.get_running_loop()
        config = {
            "temperature": 0.1,
            "max_output_tokens": 2048,
            "response_mime_type": "application/json",
        }
        if self.thinking_level and gemini_model_supports_thinking(self.model):
            config["thinking_config"] = {"thinking_level": self.thinking_level.upper()}

        gen_kwargs = {
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "model": self.model,
            "metadata": {
                "provider": "gemini",
                "prompt_version": prompt_version or "fallback",
            },
        }
        if lf_prompt is not None:
            gen_kwargs["prompt"] = lf_prompt

        with _obs_start_generation("insights.generate", **gen_kwargs):
            response = await loop.run_in_executor(
                None,
                lambda: client.models.generate_content(
                    model=self.model,
                    contents=f"{system_prompt}\n\n{user_prompt}",
                    config=config,
                ),
            )
            if response:
                usage = {}
                if getattr(response, "usage_metadata", None):
                    usage = {
                        "input": response.usage_metadata.prompt_token_count,
                        "output": response.usage_metadata.candidates_token_count,
                        "total": response.usage_metadata.total_token_count,
                    }
                _obs_update_current_generation(
                    output=response.text or "",
                    usage_details=usage,
                )
        return response.text or ""

    async def run_grounding_check(
        self,
        result: "AIInsightResult",
        candidates: list[CandidateInsightInput],
        trace_id: str | None,
    ) -> None:
        """Run hallucination checks in the background and record scores to Langfuse.

        Never raises — designed to be called via FastAPI BackgroundTasks so it
        cannot affect the response already sent to the user.
        """
        if not self.verify_grounding or result.status != "ok" or not result.matrix:
            return
        try:
            from pipeline.observability import record_score, ScoreName
            candidate_map = {c.candidate_id: c for c in candidates}
            verify_tasks = [
                self._verify_grounding(entry, candidate_map[entry.candidate_id])
                for entry in result.matrix
                if entry.candidate_id in candidate_map
            ]
            if not verify_tasks:
                return
            rates = await asyncio.gather(*verify_tasks)
            avg_rate = sum(rates) / len(rates)
            if trace_id:
                record_score(
                    trace_id=trace_id,
                    name=ScoreName.HALLUCINATION_RATE,
                    value=avg_rate,
                    comment=f"Avg hallucination rate across {len(verify_tasks)} candidates",
                )
        except Exception as exc:
            logger.warning("Background grounding check failed: %s", exc)

    async def _verify_grounding(
        self,
        entry: AIInsightMatrixEntry,
        candidate: CandidateInsightInput,
    ) -> float:
        """Verify the LLM claims in strengths and evidence against the source chunks.

        Returns the hallucination rate (0.0 to 1.0).
        """
        claims = [c.strip() for c in entry.strengths + entry.evidence if c.strip()]
        if not claims:
            return 0.0

        chunks = [candidate.best_chunk] + [chunk.get("content", "") for chunk in candidate.supporting_chunks if isinstance(chunk, dict)]
        chunks_text = "\n\n".join(f"Snippet {i+1}:\n{c}" for i, c in enumerate(chunks) if c.strip())
        if not chunks_text:
            return 1.0

        prompt = f"""Analyze whether the following claims about candidate {entry.full_name} are fully supported by the candidate's resume snippets.
A claim is unsupported (hallucinated) if it asserts a skill, role, certification, or experience that is NOT mentioned or cannot be reasonably inferred from the snippets.

Claims to verify:
{chr(10).join(f"- {claim}" for claim in claims)}

Resume Snippets:
{chunks_text}

Return valid JSON object only in the following format:
{{
  "verifications": [
    {{
      "claim": "exact claim text",
      "supported": true/false
    }}
  ]
}}
"""
        try:
            system_prompt = "You are a factual verifier. Check if claims are grounded in snippets. Return JSON."
            from pipeline.observability import start_span as _obs_start_span, update_current_span as _obs_update_current_span
            with _obs_start_span("insights.verify_claims", input={"candidate_id": candidate.candidate_id, "claims_count": len(claims)}):
                raw_response = await self._call_llm(system_prompt, prompt)
                data = json.loads(_extract_json_object(raw_response))
                verifications = data.get("verifications", [])
                
                unsupported_count = 0
                total_count = 0
                for v in verifications:
                    total_count += 1
                    if not v.get("supported", True):
                        unsupported_count += 1
                
                if total_count == 0:
                    total_count = len(claims)
                
                hallucination_rate = unsupported_count / total_count
                _obs_update_current_span(output={"hallucination_rate": hallucination_rate, "verifications": verifications})
                return hallucination_rate
        except Exception as exc:
            logger.warning("Grounding check failed for %s: %s", entry.full_name, exc)
            return 0.0


def candidate_insight_input_from_result(result: Any) -> CandidateInsightInput:
    """Build an LLM-safe candidate packet from API or search result objects."""
    return CandidateInsightInput(
        candidate_id=str(getattr(result, "candidate_id", "")),
        full_name=str(getattr(result, "full_name", "")),
        city=getattr(result, "city", None),
        country=getattr(result, "country", None),
        salary_min=getattr(result, "salary_min", None),
        salary_max=getattr(result, "salary_max", None),
        years_exp=getattr(result, "years_exp", None),
        skills=list(getattr(result, "skills", []) or []),
        best_chunk=str(getattr(result, "best_chunk", "") or ""),
        similarity_score=float(getattr(result, "similarity_score", 0) or 0),
        doc_type=str(getattr(result, "doc_type", "") or ""),
        document_title=getattr(result, "document_title", None),
        supporting_chunks=list(getattr(result, "supporting_chunks", []) or []),
        rerank_score=getattr(result, "rerank_score", None),
        rank_score=getattr(result, "rank_score", None),
    )


def parse_ai_insight_response(
    raw_response: str,
    candidates: list[CandidateInsightInput],
    provider: str,
    model: str,
) -> AIInsightResult:
    """Parse LLM JSON and drop any rows that refer to candidates outside the top results."""
    candidate_map = {candidate.candidate_id: candidate for candidate in candidates}
    try:
        payload = json.loads(_extract_json_object(raw_response))
    except Exception as exc:
        return AIInsightResult(
            status="error",
            provider=provider,
            model=model,
            summary=f"Could not parse AI insight response: {exc}",
            error=str(exc),
        )

    matrix: list[AIInsightMatrixEntry] = []
    for raw_row in payload.get("matrix") or []:
        if not isinstance(raw_row, dict):
            continue
        candidate_id = _clean_text(raw_row.get("candidate_id"))
        candidate = candidate_map.get(candidate_id)
        if candidate is None:
            continue
        matrix.append(
            AIInsightMatrixEntry(
                candidate_id=candidate_id,
                full_name=candidate.full_name,
                fit_score=_coerce_score(raw_row.get("fit_score")),
                recommendation=_clean_text(raw_row.get("recommendation")) or "Review evidence",
                strengths=_coerce_text_list(raw_row.get("strengths"), limit=4),
                evidence=_coerce_text_list(raw_row.get("evidence"), limit=4),
                gaps=_coerce_text_list(raw_row.get("gaps"), limit=3),
                interview_probe=_clean_text(raw_row.get("interview_probe")),
                grounding_notes=_clean_text(raw_row.get("grounding_notes")),
            )
        )

    best_candidate_id = _clean_text(payload.get("best_candidate_id")) or None
    valid_matrix_ids = {row.candidate_id for row in matrix}
    if best_candidate_id not in valid_matrix_ids:
        best_candidate_id = max(matrix, key=lambda row: row.fit_score).candidate_id if matrix else None

    if not matrix:
        return AIInsightResult(
            status="error",
            provider=provider,
            model=model,
            summary="AI insight did not include any candidates from the current top results.",
            error="no valid candidate rows",
        )

    return AIInsightResult(
        status="ok",
        provider=provider,
        model=model,
        best_candidate_id=best_candidate_id,
        summary=_clean_text(payload.get("summary")) or "AI insight generated for the top results.",
        comparative_reasoning=_clean_text(payload.get("comparative_reasoning")),
        matrix=matrix,
    )


def _search_based_fallback(
    candidates: list[CandidateInsightInput],
    provider: str,
    model: str,
    error: str,
) -> AIInsightResult:
    """Return a deterministic evidence matrix when the remote LLM is unavailable."""
    rows = [
        AIInsightMatrixEntry(
            candidate_id=candidate.candidate_id,
            full_name=candidate.full_name,
            fit_score=_score_from_search(candidate),
            recommendation="Review with source evidence",
            strengths=_fallback_strengths(candidate),
            evidence=_fallback_evidence(candidate),
            gaps=["LLM comparison is unavailable; verify the source documents before deciding."],
            interview_probe=_fallback_probe(candidate),
        )
        for candidate in candidates[:10]
    ]
    rows.sort(key=lambda row: row.fit_score, reverse=True)
    best_candidate_id = rows[0].candidate_id if rows else None
    return AIInsightResult(
        status="error",
        provider=provider,
        model=model,
        best_candidate_id=best_candidate_id,
        summary=(
            "The LLM call failed, so this matrix falls back to search ranking, "
            "retrieval scores, skills, and cited snippets."
        ),
        matrix=rows,
        error=error,
    )


def _system_prompt() -> str:
    from pipeline.prompts import INSIGHTS_SYSTEM_PROMPT
    return INSIGHTS_SYSTEM_PROMPT


def _user_prompt(
    query: str,
    filters_applied: dict[str, Any],
    candidates: list[CandidateInsightInput],
) -> str:
    packet = {
        "query": query,
        "filters_applied": filters_applied,
        "candidates": [_candidate_packet(candidate) for candidate in candidates[:5]],
    }
    return json.dumps(packet, ensure_ascii=True, indent=2)


def _candidate_packet(candidate: CandidateInsightInput) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "full_name": candidate.full_name,
        "location": ", ".join(
            value for value in [candidate.city, candidate.country] if value
        ),
        "salary_min": candidate.salary_min,
        "salary_max": candidate.salary_max,
        "years_exp": candidate.years_exp,
        "skills": candidate.skills[:12],
        "scores": {
            "similarity_score": _round_float(candidate.similarity_score),
            "rank_score": _round_float(candidate.rank_score),
            "rerank_score": _round_float(candidate.rerank_score),
        },
        "best_evidence": {
            "doc_type": candidate.doc_type,
            "document_title": candidate.document_title,
            "content": _truncate(candidate.best_chunk, 700),
        },
        "supporting_evidence": [
            {
                "doc_type": _clean_text(chunk.get("doc_type")),
                "document_title": _clean_text(chunk.get("document_title")),
                "content": _truncate(_clean_text(chunk.get("content")), 450),
            }
            for chunk in candidate.supporting_chunks[:3]
            if isinstance(chunk, dict)
        ],
    }


def _extract_json_object(raw_response: str) -> str:
    text = raw_response.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fenced:
        return fenced.group(1)
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no JSON object found")
    return text[start : end + 1]


def _coerce_score(value: Any) -> int:
    try:
        score_value = float(value)
    except (TypeError, ValueError):
        return 0
    if 0 < score_value <= 1:
        score_value *= 100
    score = round(score_value)
    return max(0, min(100, score))


def _coerce_text_list(value: Any, limit: int) -> list[str]:
    raw_items = value if isinstance(value, list) else []
    items: list[str] = []
    for item in raw_items:
        text = _clean_text(item)
        if text:
            items.append(text)
        if len(items) >= limit:
            break
    return items


def _score_from_search(candidate: CandidateInsightInput) -> int:
    score = candidate.rerank_score
    if score is None:
        score = candidate.rank_score
    if score is None:
        score = candidate.similarity_score
    try:
        value = float(score)
    except (TypeError, ValueError):
        return 0
    if value <= 1:
        value *= 100
    return max(0, min(100, round(value)))


def _fallback_strengths(candidate: CandidateInsightInput) -> list[str]:
    strengths = []
    if candidate.skills:
        strengths.append(f"Skills include {', '.join(candidate.skills[:5])}.")
    if candidate.years_exp:
        strengths.append(f"{candidate.years_exp} years of experience listed.")
    if candidate.rerank_score is not None:
        strengths.append("Cross-encoder reranker supported this candidate.")
    elif candidate.rank_score is not None:
        strengths.append("Search ranking signals supported this candidate.")
    return strengths or ["Candidate appeared in the top search results."]


def _fallback_evidence(candidate: CandidateInsightInput) -> list[str]:
    evidence = []
    if candidate.best_chunk:
        evidence.append(_truncate(candidate.best_chunk, 220))
    for chunk in candidate.supporting_chunks[:2]:
        if isinstance(chunk, dict) and chunk.get("content"):
            evidence.append(_truncate(_clean_text(chunk.get("content")), 180))
    return evidence or ["No snippet evidence was available in the search result."]


def _fallback_probe(candidate: CandidateInsightInput) -> str:
    if candidate.skills:
        return f"Ask for a concrete example using {candidate.skills[0]} in a role-critical project."
    return "Ask for a concrete example that proves fit for the role-critical requirements."


def _clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _truncate(value: str, limit: int) -> str:
    text = _clean_text(value)
    return text if len(text) <= limit else f"{text[: limit - 3].rstrip()}..."


def _round_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _looks_configured(api_key: str) -> bool:
    value = api_key.strip()
    return bool(value) and not value.endswith(PLACEHOLDER_SUFFIX)


def _default_model_for_provider(provider: str) -> str:
    configured_provider = getattr(settings, "llm_provider", "gemini")
    configured_model = getattr(settings, "llm_model", "")
    if provider == configured_provider and configured_model:
        return configured_model
    return default_llm_model_for_provider(provider)


def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 2)
