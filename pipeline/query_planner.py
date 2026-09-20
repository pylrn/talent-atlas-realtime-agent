"""Optional LLM query planner for candidate search.

The planner turns a recruiter's natural-language query into a cleaned semantic
query plus structured candidate filters. It is intentionally separate from the
default fast search path so the product can benchmark and opt into it safely.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pipeline import settings
from pipeline.llm_models import (
    DEFAULT_LLM_MODELS as DEFAULT_LLM_MODELS,
    default_llm_model_for_provider,
)
from pipeline.search import SearchFilters

logger = logging.getLogger(__name__)

PLACEHOLDER_SUFFIX = "..."
DEFAULT_MIN_CONFIDENCE = 0.35
PROVIDER_COST_PER_MILLION = {
    "openai": (0.15, 0.60),
    "gemini": (0.50, 3.00),
}


@dataclass
class PlannerUsage:
    """Estimated LLM usage for benchmark reporting."""

    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


@dataclass
class PlannedSearch:
    """Planner output used by benchmark strategies."""

    query: str
    filters: SearchFilters
    confidence: float = 0.0
    reasoning: str = ""
    used_fallback: bool = False
    usage: PlannerUsage = field(default_factory=PlannerUsage)


class LLMQueryPlanner:
    """Call an LLM to infer search filters and a cleaner semantic query."""

    def __init__(
        self,
        provider: Literal["openai", "gemini"] | None = None,
        model: str | None = None,
        min_confidence: float = DEFAULT_MIN_CONFIDENCE,
        input_cost_per_million: float | None = None,
        output_cost_per_million: float | None = None,
    ):
        self.provider = provider or settings.llm_provider
        self.model = model or _default_model_for_provider(self.provider)
        self.min_confidence = min_confidence
        defaults = PROVIDER_COST_PER_MILLION.get(self.provider, (0.0, 0.0))
        self.input_cost_per_million = (
            defaults[0] if input_cost_per_million is None else input_cost_per_million
        )
        self.output_cost_per_million = (
            defaults[1] if output_cost_per_million is None else output_cost_per_million
        )

    async def plan(
        self,
        query: str,
        base_filters: SearchFilters | None = None,
    ) -> PlannedSearch:
        """Return a planned query, falling back safely on missing keys/errors."""
        base_filters = base_filters or SearchFilters()
        if not self._has_valid_key():
            return fallback_plan(query, base_filters, reason="LLM provider is not configured")

        system_prompt = _planner_system_prompt()
        user_prompt = _planner_user_prompt(query, base_filters)
        usage = PlannerUsage(
            llm_calls=1,
            input_tokens=_estimate_tokens(system_prompt) + _estimate_tokens(user_prompt),
        )

        try:
            raw_response = await self._call_llm(system_prompt, user_prompt)
            usage.output_tokens = _estimate_tokens(raw_response)
            usage.estimated_cost_usd = _estimate_cost(
                usage.input_tokens,
                usage.output_tokens,
                self.input_cost_per_million,
                self.output_cost_per_million,
            )
            planned = parse_planner_response(
                raw_response,
                query,
                base_filters,
                min_confidence=self.min_confidence,
            )
            planned.usage = usage
            return planned
        except Exception as exc:
            logger.warning("LLM query planning failed: %s", exc)
            usage.estimated_cost_usd = _estimate_cost(
                usage.input_tokens,
                usage.output_tokens,
                self.input_cost_per_million,
                self.output_cost_per_million,
            )
            planned = fallback_plan(query, base_filters, reason=str(exc))
            planned.usage = usage
            return planned

    def _has_valid_key(self) -> bool:
        if self.provider == "openai":
            return _looks_configured(settings.openai_api_key)
        if self.provider == "gemini":
            return _looks_configured(settings.google_api_key)
        return False

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        if self.provider == "openai":
            return await self._call_openai(system_prompt, user_prompt)
        if self.provider == "gemini":
            return await self._call_gemini(system_prompt, user_prompt)
        raise ValueError(f"Unsupported LLM provider: {self.provider}")

    async def _call_openai(self, system_prompt: str, user_prompt: str) -> str:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        response = await client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=500,
        )
        return response.choices[0].message.content or ""

    async def _call_gemini(self, system_prompt: str, user_prompt: str) -> str:
        from google import genai

        client = genai.Client(api_key=settings.google_api_key)
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model=self.model,
                contents=f"{system_prompt}\n\n{user_prompt}",
                config={"temperature": 0.1, "max_output_tokens": 2048},
            ),
        )
        return response.text or ""


def _default_model_for_provider(provider: str) -> str:
    configured_provider = getattr(settings, "llm_provider", "gemini")
    configured_model = getattr(settings, "llm_model", "")
    if provider == configured_provider and configured_model:
        return configured_model
    return default_llm_model_for_provider(provider)


def parse_planner_response(
    raw_response: str,
    original_query: str,
    base_filters: SearchFilters | None = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> PlannedSearch:
    """Parse and validate planner JSON, returning fallback on invalid output."""
    base_filters = base_filters or SearchFilters()
    try:
        payload = json.loads(_extract_json_object(raw_response))
    except Exception as exc:
        return fallback_plan(original_query, base_filters, reason=f"invalid JSON: {exc}")

    confidence = _coerce_confidence(payload.get("confidence", 0.0))
    if confidence < min_confidence:
        return fallback_plan(
            original_query,
            base_filters,
            reason=f"confidence {confidence:.2f} below threshold {min_confidence:.2f}",
            confidence=confidence,
        )

    query = _clean_text(payload.get("query")) or original_query
    inferred = _filters_from_payload(payload.get("filters") or {})
    merged = merge_with_explicit_filters(base_filters, inferred)
    return PlannedSearch(
        query=query,
        filters=merged,
        confidence=confidence,
        reasoning=_clean_text(payload.get("reasoning")),
        used_fallback=False,
    )


def fallback_plan(
    query: str,
    filters: SearchFilters | None = None,
    reason: str = "",
    confidence: float = 0.0,
) -> PlannedSearch:
    """Return the original query and filters unchanged."""
    return PlannedSearch(
        query=query,
        filters=copy_filters(filters or SearchFilters()),
        confidence=confidence,
        reasoning=reason,
        used_fallback=True,
    )


def merge_with_explicit_filters(base: SearchFilters, inferred: SearchFilters) -> SearchFilters:
    """Let explicit user filters override LLM-inferred filters."""
    use_inferred_skills = base.skills is None
    return SearchFilters(
        location=base.location if base.location is not None else inferred.location,
        country=base.country if base.country is not None else inferred.country,
        city=base.city if base.city is not None else inferred.city,
        min_age=base.min_age,
        max_age=base.max_age,
        skills=base.skills if base.skills is not None else inferred.skills,
        interests=base.interests,
        min_years_exp=(
            base.min_years_exp if base.min_years_exp is not None else inferred.min_years_exp
        ),
        max_years_exp=(
            base.max_years_exp if base.max_years_exp is not None else inferred.max_years_exp
        ),
        min_salary=base.min_salary if base.min_salary is not None else inferred.min_salary,
        max_salary=base.max_salary if base.max_salary is not None else inferred.max_salary,
        status=base.status,
        doc_types=base.doc_types,
        skills_match=inferred.skills_match if use_inferred_skills else base.skills_match,
        interests_match=base.interests_match,
    )


def copy_filters(filters: SearchFilters) -> SearchFilters:
    return SearchFilters(
        location=filters.location,
        country=filters.country,
        city=filters.city,
        min_age=filters.min_age,
        max_age=filters.max_age,
        skills=list(filters.skills) if filters.skills is not None else None,
        interests=list(filters.interests) if filters.interests is not None else None,
        min_years_exp=filters.min_years_exp,
        max_years_exp=filters.max_years_exp,
        min_salary=filters.min_salary,
        max_salary=filters.max_salary,
        status=filters.status,
        doc_types=list(filters.doc_types) if filters.doc_types is not None else None,
        skills_match=filters.skills_match,
        interests_match=filters.interests_match,
    )


def _filters_from_payload(raw_filters: dict[str, Any]) -> SearchFilters:
    skills = _coerce_skills(raw_filters.get("skills"))
    skills_match = str(raw_filters.get("skills_match") or "or").strip().lower()
    if skills_match not in {"and", "or"}:
        skills_match = "or"

    return SearchFilters(
        location=_clean_text(raw_filters.get("location")) or None,
        country=_clean_text(raw_filters.get("country")) or None,
        city=_clean_text(raw_filters.get("city")) or None,
        skills=skills or None,
        skills_match=skills_match,  # type: ignore[arg-type]
        min_years_exp=_coerce_int(raw_filters.get("min_years_exp")),
        max_years_exp=_coerce_int(raw_filters.get("max_years_exp")),
        min_salary=_coerce_int(raw_filters.get("min_salary")),
        max_salary=_coerce_int(raw_filters.get("max_salary")),
    )


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


def _coerce_skills(value: Any) -> list[str]:
    if value is None:
        return []
    raw_items = value.split(",") if isinstance(value, str) else value
    if not isinstance(raw_items, list):
        return []
    skills: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        skill = _clean_text(item).lower()
        if not skill or skill in seen:
            continue
        skills.append(skill)
        seen.add(skill)
    return skills


def _coerce_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(1.0, max(0.0, confidence))


def _clean_text(value: Any) -> str:
    text = "" if value is None else str(value)
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _estimate_tokens(text: str) -> int:
    return max(1, round(len(text) / 4))


def _estimate_cost(
    input_tokens: int,
    output_tokens: int,
    input_cost_per_million: float,
    output_cost_per_million: float,
) -> float:
    return (
        (input_tokens / 1_000_000) * input_cost_per_million
        + (output_tokens / 1_000_000) * output_cost_per_million
    )


def _looks_configured(api_key: str) -> bool:
    value = api_key.strip()
    return bool(value) and not value.endswith(PLACEHOLDER_SUFFIX)


def _planner_system_prompt() -> str:
    return """You are a query planner for a candidate search engine.

Extract only candidate-level constraints that are explicitly implied by the
recruiter's query. Do not invent requirements. Do not output document types.

Respond with valid JSON only:
{
  "query": "clean semantic search text",
  "filters": {
    "location": null,
    "country": null,
    "city": null,
    "skills": [],
    "skills_match": "or",
    "min_years_exp": null,
    "max_years_exp": null,
    "min_salary": null,
    "max_salary": null
  },
  "confidence": 0.0,
  "reasoning": "short explanation"
}"""


def _planner_user_prompt(query: str, filters: SearchFilters) -> str:
    return f"""Recruiter query: {query}

Existing explicit filters:
- location: {filters.location}
- country: {filters.country}
- city: {filters.city}
- skills: {filters.skills}
- skills_match: {filters.skills_match}
- min_years_exp: {filters.min_years_exp}
- max_years_exp: {filters.max_years_exp}
- min_salary: {filters.min_salary}
- max_salary: {filters.max_salary}

Return the planned search JSON."""
