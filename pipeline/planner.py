"""LLM query planner.

Converts recruiter natural-language input (query or JD) into a
CanonicalSearchSpec by calling the LLM with a structured output prompt.

Falls back to planner_fallback.py on any error or when disabled.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from pipeline import settings
from pipeline import cache as _cache
from pipeline import metrics as _metrics
from pipeline.observability import (
    is_enabled as _obs_enabled,
    _get_client as _obs_client,
    update_current_span as _obs_update_current_span,
    update_current_generation as _obs_update_current_generation,
    start_generation as _obs_start_generation,
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

from pipeline.constants import PLANNER_VERSION
from pipeline.planner_fallback import fallback_plan
from pipeline.prompts import build_user_message, get_planner_system_prompt
from pipeline.sanitize import sanitize_input
from pipeline.spec import CanonicalSearchSpec
from pipeline.hints import build_hints_block, hints_cache_tag
from pipeline.validator import validate_spec
from pipeline.normalizer import normalize_spec

logger = logging.getLogger(__name__)

_PLACEHOLDER = "..."


def _key_configured(api_key: str) -> bool:
    v = api_key.strip()
    return bool(v) and not v.endswith(_PLACEHOLDER)


# ── Public entry point ────────────────────────────────────────────────────────

async def plan(
    raw_input: str,
    explicit_filters: dict[str, Any] | None = None,
    route: str = "query_mode",
    cfg: dict[str, Any] | None = None,
) -> CanonicalSearchSpec:
    """Return a CanonicalSearchSpec for the given input.

    Steps (each individually toggleable via cfg):
      sanitize → cache lookup → LLM call → validate → normalize → cache write
    """
    cfg = cfg or {}
    use_sanitizer    = cfg.get("use_sanitizer",    True)
    use_cache        = cfg.get("use_cache",         True)
    use_llm_planner  = cfg.get("use_llm_planner",  True)
    use_fallback     = cfg.get("use_planner_fallback", True)
    use_repair       = cfg.get("use_fallback_repair", True)
    use_validator    = cfg.get("use_validator",     True)
    use_normalizer   = cfg.get("use_normalizer",    True)

    # Step 2: sanitize
    sanitized = sanitize_input(raw_input) if use_sanitizer else raw_input

    # Step 2b: complexity gate — skip LLM for simple keyword queries.
    # Queries with ≤4 tokens and no natural-language connectives are handled
    # identically by the regex fallback, so burning a paid LLM call adds no value.
    if use_llm_planner and _is_simple_query(sanitized):
        use_llm_planner = False
        logger.debug("Complexity gate: skipping LLM planner for simple query %r", sanitized[:60])

    # Step 3: cache lookup — include provider/model so fast & quality modes don't share entries
    filters_str = json.dumps(explicit_filters or {}, sort_keys=True)
    provider_for_key = cfg.get("llm_provider") or getattr(settings, "llm_provider", "gemini")
    model_for_key    = cfg.get("llm_model")    or getattr(settings, "llm_model", "")
    
    personalization_enabled = cfg.get("personalization_enabled", False)
    personalization_hints = cfg.get("personalization_hints", [])
    
    hints_tag = ""
    if personalization_enabled and personalization_hints:
        import hashlib
        payload = json.dumps([h["text"] for h in personalization_hints], sort_keys=True)
        hints_tag = "|h:" + hashlib.sha1(payload.encode()).hexdigest()[:12]
        
    planner_mode_tag = "llm" if use_llm_planner else "fallback"
    route_with_llm   = f"{route}|{planner_mode_tag}|{provider_for_key}|{model_for_key}{hints_tag}"
    ck = _cache.plan_key(raw_input.lower().strip(), filters_str, route_with_llm)
    if use_cache:
        with _obs_span("search.plan.cache_lookup", input={"key": ck, "query": raw_input}):
            cached = await _cache.get(ck)
            hit_summary = (
                {
                    "hit": True,
                    "rewritten_query": cached.get("rewritten_query"),
                    "filters": cached.get("filters"),
                }
                if cached is not None
                else {"hit": False}
            )
            _obs_update_current_span(output=hit_summary, metadata={"hit": cached is not None, "key": ck})
        if cached is not None:
            logger.debug("Plan cache hit for key %s", ck[:32])
            _metrics.incr("planner_cache_hits")
            return _spec_from_dict(cached)

    system_prompt_prefix = _build_hints_block(personalization_hints) if personalization_enabled else ""

    # Step 4: LLM planner (with fallback)
    if use_llm_planner:
        provider_override = cfg.get("llm_provider")
        model_override    = cfg.get("llm_model")
        thinking_level    = cfg.get("thinking_level")
        sys_prompt_base, prompt_version, lf_prompt = get_planner_system_prompt()
        with _obs_span("search.plan.generate", input={"query": sanitized}):
            _obs_update_current_span(metadata={"prompt_version": prompt_version or "fallback"})
            with _metrics.Timer("planner_ms"):
                try:
                    raw_dict = await asyncio.wait_for(
                        _call_llm(
                            sanitized, provider_override, model_override,
                            thinking_level, system_prompt_prefix=system_prompt_prefix,
                            sys_prompt_base=sys_prompt_base,
                        ),
                        timeout=settings.llm_planner_timeout_seconds,
                    )
                    planner_error = None
                except asyncio.TimeoutError:
                    provider_name = provider_override or settings.llm_provider
                    planner_error = f"LLM planner timed out after {settings.llm_planner_timeout_seconds}s ({provider_name})"
                    logger.warning(planner_error)
                    _obs_update_current_span(level="ERROR", status_message=planner_error)
                    raw_dict = None
                except Exception as exc:
                    planner_error = str(exc)
                    logger.warning("LLM planner call failed (%s): %s", provider_override or settings.llm_provider, exc)
                    _obs_update_current_span(level="ERROR", status_message=planner_error)
                    raw_dict = None

            _obs_update_current_span(output=raw_dict)
        _metrics.incr("planner_llm_calls")
        if raw_dict is None:
            if not use_fallback:
                raise RuntimeError("LLM planner failed and planner fallback is disabled")
            spec = fallback_plan(raw_input, explicit_filters)
            spec.planner_error = planner_error or "LLM planner returned no usable plan"
            _metrics.incr("planner_fallback_used")
        else:
            with _obs_span("search.plan.validate", input=raw_dict):
                if use_validator:
                    spec = validate_spec(raw_dict, original_input=raw_input,
                                         explicit_filters=explicit_filters)
                else:
                    spec = _spec_from_dict(raw_dict)
                _obs_update_current_span(output=spec.to_dict() if spec else None)
            if use_repair:
                spec = _repair_spec_with_fallback(
                    spec,
                    fallback_plan(raw_input, explicit_filters),
                )
    else:
        if not use_fallback:
            raise RuntimeError("LLM planner is disabled and planner fallback is disabled")
        spec = fallback_plan(raw_input, explicit_filters)
        _metrics.incr("planner_fallback_used")

    # Step 5b: normalize
    spec_pre_normalize = spec.to_dict()
    with _obs_span("search.plan.normalize", input=spec_pre_normalize):
        if use_normalizer:
            spec = normalize_spec(spec)
        _obs_update_current_span(output=spec.to_dict() if spec else None)

    # Write to cache.
    if use_cache and not spec.used_fallback:
        spec_dict = spec.to_dict()
        if "personalization_offer" in spec_dict:
            del spec_dict["personalization_offer"]
        await _cache.set(ck, spec_dict)

    return spec


def _repair_spec_with_fallback(
    spec: CanonicalSearchSpec,
    fallback: CanonicalSearchSpec,
) -> CanonicalSearchSpec:
    """Fill LLM omissions from fallback without replacing LLM decisions."""
    _append_missing(spec.must.skills, fallback.must.skills)
    _append_missing(spec.should.skills, fallback.should.skills)
    _append_missing(spec.should.themes, fallback.should.themes)
    _append_missing(spec.should.locations, fallback.should.locations)
    _append_missing(spec.should.roles, fallback.should.roles)
    _append_missing(spec.must_not.skills, fallback.must_not.skills)
    _append_missing(spec.must_not.status, fallback.must_not.status)
    _append_missing(spec.must_not.companies, fallback.must_not.companies)
    _append_missing(spec.lexical_terms, fallback.lexical_terms)
    _append_missing(spec.search_targets, fallback.search_targets)

    if not spec.must.country and fallback.must.country:
        spec.must.country = fallback.must.country
    if not spec.must.city and fallback.must.city:
        spec.must.city = fallback.must.city
    if spec.must.min_years_exp is None and fallback.must.min_years_exp is not None:
        spec.must.min_years_exp = fallback.must.min_years_exp
    if spec.must.max_years_exp is None and fallback.must.max_years_exp is not None:
        spec.must.max_years_exp = fallback.must.max_years_exp
    if spec.must.min_salary is None and fallback.must.min_salary is not None:
        spec.must.min_salary = fallback.must.min_salary
    if spec.must.max_salary is None and fallback.must.max_salary is not None:
        spec.must.max_salary = fallback.must.max_salary
    if not spec.must.applied_role and fallback.must.applied_role:
        spec.must.applied_role = fallback.must.applied_role
    if not spec.must.status and fallback.must.status:
        spec.must.status = list(fallback.must.status)

    return spec


def _append_missing(target: list[str], additions: list[str]) -> None:
    seen = {item.lower() for item in target}
    for item in additions:
        key = item.lower()
        if key not in seen:
            target.append(item)
            seen.add(key)


def _build_hints_block(hints: list[dict]) -> str:
    """Format the persistent personalization hints for the system prompt."""
    if not hints:
        return ""
    lines = ["=== ABOUT THIS USER (soft hints, NOT requirements) ==="]
    for h in hints:
        lines.append(f"- {h['text']}")
    lines.append("\nRULES for these hints:")
    lines.append("1. They may only influence should.skills, should.themes, should.locations,")
    lines.append("   should.roles, lexical_terms, semantic_query, or hyde_profile.")
    lines.append("2. They MUST NEVER be added to must.*, must_not.*, or any hard-filter field.")
    lines.append("   A hint is a soft bias, not a requirement.")
    lines.append("3. If the current query contradicts a hint, the current query wins —")
    lines.append("   ignore the hint for this search.")
    lines.append("4. Do not re-offer a hint that is already in this list.")
    return "\n".join(lines)


# ── LLM call ──────────────────────────────────────────────────────────────────

async def _call_llm(
    sanitized_input: str,
    provider_override: str | None = None,
    model_override: str | None = None,
    thinking_level: str | None = None,
    system_prompt_prefix: str = "",
    sys_prompt_base: str | None = None,
) -> dict | None:
    provider = provider_override or getattr(settings, "llm_provider", "gemini")
    try:
        if provider == "openai" and _key_configured(settings.openai_api_key):
            return await _call_openai(sanitized_input, model_override, system_prompt_prefix, sys_prompt_base)
        if provider == "gemini" and _key_configured(settings.google_api_key):
            return await _call_gemini(sanitized_input, model_override, thinking_level, system_prompt_prefix, sys_prompt_base)
        if provider == "groq" and _key_configured(settings.groq_api_key):
            return await _call_groq(sanitized_input, model_override, system_prompt_prefix, sys_prompt_base)
        if provider == "deepseek" and _key_configured(getattr(settings, "deepseek_api_key", "")):
            return await _call_deepseek(sanitized_input, model_override, system_prompt_prefix, sys_prompt_base)
    except Exception as exc:
        logger.warning("LLM planner call failed (%s): %s", provider, exc)
    return None


async def _call_openai(
    sanitized_input: str,
    model_override: str | None = None,
    system_prompt_prefix: str = "",
    sys_prompt_base: str | None = None,
) -> dict | None:
    from pipeline.observability import get_async_openai
    from pipeline.llm_models import default_llm_model_for_provider

    AsyncOpenAI = get_async_openai()
    model  = model_override or getattr(settings, "llm_model", None) or default_llm_model_for_provider("openai")
    client = AsyncOpenAI(api_key=settings.openai_api_key)
    supports_langfuse_name = getattr(AsyncOpenAI, "__module__", "").startswith("langfuse.")
    base = sys_prompt_base or ""
    sys_prompt = f"{system_prompt_prefix}\n\n{base}" if system_prompt_prefix else base
    create_kwargs = {
        "model": model,
        "messages": [
            {"role": "system",  "content": sys_prompt},
            {"role": "user",    "content": build_user_message(sanitized_input)},
        ],
        "temperature": 0.1,
        "max_tokens": 800,
        "response_format": {"type": "json_object"},
    }
    if supports_langfuse_name:
        create_kwargs["name"] = "search.plan.model"
    try:
        resp = await client.chat.completions.create(**create_kwargs)
    except Exception as exc:
        _raise_if_fatal(exc)
        raise
    return _parse_response(resp.choices[0].message.content or "")


async def _call_groq(
    sanitized_input: str,
    model_override: str | None = None,
    system_prompt_prefix: str = "",
    sys_prompt_base: str | None = None,
) -> dict | None:
    from pipeline.observability import get_async_openai
    from pipeline.llm_models import default_llm_model_for_provider

    AsyncOpenAI = get_async_openai()
    model  = model_override or getattr(settings, "llm_model", None) or default_llm_model_for_provider("groq")
    client = AsyncOpenAI(
        api_key=settings.groq_api_key,
        base_url="https://api.groq.com/openai/v1",
    )
    supports_langfuse_name = getattr(AsyncOpenAI, "__module__", "").startswith("langfuse.")
    base = sys_prompt_base or ""
    sys_prompt = f"{system_prompt_prefix}\n\n{base}" if system_prompt_prefix else base
    create_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user",   "content": build_user_message(sanitized_input)},
        ],
        "temperature": 0.1,
        "max_tokens": 800,
        "response_format": {"type": "json_object"},
    }
    if supports_langfuse_name:
        create_kwargs["name"] = "search.plan.model"
    try:
        resp = await client.chat.completions.create(**create_kwargs)
    except Exception as exc:
        _raise_if_fatal(exc)
        raise
    return _parse_response(resp.choices[0].message.content or "")


async def _call_deepseek(
    sanitized_input: str,
    model_override: str | None = None,
    system_prompt_prefix: str = "",
    sys_prompt_base: str | None = None,
) -> dict | None:
    """DeepSeek planner call. OpenAI-compatible API (base_url api.deepseek.com)."""
    from pipeline.observability import get_async_openai
    from pipeline.llm_models import default_llm_model_for_provider

    AsyncOpenAI = get_async_openai()
    model  = model_override or getattr(settings, "llm_model", None) or default_llm_model_for_provider("deepseek")
    client = AsyncOpenAI(
        api_key=getattr(settings, "deepseek_api_key", "") or "",
        base_url="https://api.deepseek.com",
    )
    supports_langfuse_name = getattr(AsyncOpenAI, "__module__", "").startswith("langfuse.")
    base = sys_prompt_base or ""
    sys_prompt = f"{system_prompt_prefix}\n\n{base}" if system_prompt_prefix else base
    create_kwargs = {
        "model": model,
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user",   "content": build_user_message(sanitized_input)},
        ],
        "temperature": 0.1,
        "max_tokens": 800,
        "response_format": {"type": "json_object"},
    }
    if supports_langfuse_name:
        create_kwargs["name"] = "search.plan.model"
    try:
        resp = await client.chat.completions.create(**create_kwargs)
    except Exception as exc:
        _raise_if_fatal(exc)
        raise
    return _parse_response(resp.choices[0].message.content or "")


async def _call_gemini(
    sanitized_input: str,
    model_override: str | None = None,
    thinking_level: str | None = None,
    system_prompt_prefix: str = "",
    sys_prompt_base: str | None = None,
) -> dict | None:
    from google import genai
    from pipeline.llm_models import default_llm_model_for_provider, gemini_model_supports_thinking

    model  = model_override or getattr(settings, "llm_model", None) or default_llm_model_for_provider("gemini")
    client = genai.Client(api_key=settings.google_api_key)
    loop   = asyncio.get_running_loop()

    base = sys_prompt_base or ""
    sys_prompt = f"{system_prompt_prefix}\n\n{base}" if system_prompt_prefix else base
    prompt = f"{sys_prompt}\n\n{build_user_message(sanitized_input)}"
    config = {
        "temperature": 0.1,
        "max_output_tokens": 4096,
        "response_mime_type": "application/json",
    }
    if thinking_level and gemini_model_supports_thinking(model):
        if "thinking_level" in genai.types.ThinkingConfig.model_fields:
            config["thinking_config"] = {"thinking_level": thinking_level.upper()}
    with _obs_start_generation(
        "search.plan.model",
        input=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": build_user_message(sanitized_input)},
        ],
        model=model,
        metadata={"provider": "gemini"},
    ):
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: client.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config,
                ),
            )
        except Exception as exc:
            _raise_if_fatal(exc)
            raise
        if resp:
            usage = {}
            if getattr(resp, "usage_metadata", None):
                usage = {
                    "input": resp.usage_metadata.prompt_token_count,
                    "output": resp.usage_metadata.candidates_token_count,
                    "total": resp.usage_metadata.total_token_count,
                }
            _obs_update_current_generation(output=resp.text, usage_details=usage)
    return _parse_response(resp.text or "")


# ── Helpers ───────────────────────────────────────────────────────────────────

_NL_MARKERS = frozenset({
    "who", "someone", "anyone", "can", "need", "needs", "want", "wants",
    "looking", "find", "show", "give", "we", "our", "the", "a", "an",
    "with", "that", "which", "has", "have", "had", "been", "able",
    "experience", "background", "skills", "knows", "worked", "built",
    "shipped", "grown", "led", "owns", "owns", "like", "similar",
})


def _is_simple_query(text: str) -> bool:
    """True when the query is short enough that the regex planner matches the LLM planner.

    Heuristic: ≤4 tokens AND none of them are natural-language connectives.
    Anything longer or conversational benefits from LLM semantic rewriting.
    """
    tokens = text.lower().split()
    if len(tokens) > 4:
        return False
    return not any(t in _NL_MARKERS for t in tokens)


def _raise_if_fatal(exc: Exception) -> None:
    """Re-raise immediately for errors where retrying/waiting is pointless.

    429 (rate limit / daily quota) and 503 (overloaded) from Groq or Gemini
    are known-fatal: the provider has already given up, so we should too
    instead of burning the remaining timeout seconds doing nothing.
    """
    msg = str(exc).lower()
    if any(code in msg for code in ("429", "503", "rate limit", "quota", "unavailable")):
        raise exc


# ── JSON parsing ──────────────────────────────────────────────────────────────

def _parse_response(raw: str) -> dict | None:
    text = raw.strip()
    # strip Gemini / Qwen thinking blocks before looking for JSON
    text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = text.strip()
    # strip markdown fences if present
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start = text.find("{")
        end   = text.rfind("}")
        if start == -1 or end <= start:
            logger.warning("No JSON object found in LLM response")
            return None
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning("JSON decode error: %s", e)
        return None


def _spec_from_dict(d: dict) -> CanonicalSearchSpec:
    """Reconstruct a CanonicalSearchSpec from a plain dict (e.g. from cache)."""
    from pipeline.spec import MustFilters, ShouldFilters, MustNotFilters

    def _must(m: dict) -> MustFilters:
        return MustFilters(
            skills        = m.get("skills") or [],
            skills_match  = m.get("skills_match") or "and",
            country       = m.get("country"),
            city          = m.get("city"),
            min_years_exp = m.get("min_years_exp"),
            max_years_exp = m.get("max_years_exp"),
            applied_role  = m.get("applied_role"),
            min_salary    = m.get("min_salary"),
            max_salary    = m.get("max_salary"),
            status        = m.get("status") or ["active"],
        )

    def _should(s: dict) -> ShouldFilters:
        return ShouldFilters(
            skills    = s.get("skills") or [],
            themes    = s.get("themes") or [],
            locations = s.get("locations") or [],
            roles     = s.get("roles") or [],
        )

    def _must_not(mn: dict) -> MustNotFilters:
        return MustNotFilters(
            skills    = mn.get("skills") or [],
            status    = mn.get("status") or [],
            companies = mn.get("companies") or [],
        )

    return CanonicalSearchSpec(
        input_type     = d.get("input_type", "query"),
        intent         = d.get("intent", "candidate_search"),
        must           = _must(d.get("must") or {}),
        should         = _should(d.get("should") or {}),
        must_not       = _must_not(d.get("must_not") or {}),
        semantic_query = d.get("semantic_query", ""),
        hyde_profile   = d.get("hyde_profile"),
        lexical_terms  = d.get("lexical_terms") or [],
        search_targets = d.get("search_targets") or [],
        confidence     = float(d.get("confidence", 0.0)),
        clarify        = d.get("clarify"),
        used_fallback  = bool(d.get("used_fallback", False)),
        planner_error  = d.get("planner_error"),
        personalization_offer = d.get("personalization_offer"),
    )
