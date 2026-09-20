"""Validates and sanitises LLM planner output.

Responsibilities:
  - Drop unknown fields (only ALLOWED_FILTERS survive)
  - Drop forbidden fields (FORBIDDEN_FIELDS) and log attempts
  - Coerce types (str→int, list normalisation)
  - Validate enum values (status, intent, search_targets)
  - Skill hallucination check: must.skills entries must appear in the
    original input text (alias-aware) — drops any that don't
  - Merge caller-supplied explicit_filters (explicit always wins)
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pipeline.aliases import normalize_skill, normalize_location, SKILL_ALIASES, KNOWN_SKILLS
from pipeline.constants import (
    ALLOWED_INTENTS,
    ALLOWED_SEARCH_TARGETS,
    ALLOWED_STATUSES,
    FORBIDDEN_FIELDS,
)
from pipeline.spec import (
    CanonicalSearchSpec,
    MustFilters,
    MustNotFilters,
    ShouldFilters,
)

logger = logging.getLogger(__name__)


# Exact string the planner is told to emit when it sees a protected-class
# filter request. We pin this here so the override can detect the LLM's own
# language without false-matching unrelated clarify messages.
_PROTECTED_CHAR_CLARIFY = "This system does not filter on protected characteristics."

# Phrases that indicate an actual request to filter on a protected class.
# Conservative — better to leave a real clarify in place than to clear a
# legitimate one. Stylistic pronouns ("he", "she") are not in the list.
_PROTECTED_TRIGGER_RE = re.compile(
    r"\b("
    r"only\s+(?:males?|females?|men|women|boys?|girls?)|"
    r"(?:no|exclude|not?)\s+(?:males?|females?|men|women)|"
    r"prefer(?:ably)?\s+(?:male|female|man|woman)|"
    r"young(?:er)?\s+(?:candidates?|persons?|engineers?|employees?|hires?|applicants?|people)|"
    r"under\s+\d{2}\s+years?\s+old|"
    r"over\s+\d{2}\s+years?\s+old|"
    r"elderly|"
    r"\bmarried\b|\bunmarried\b|\bdivorced\b|"
    r"pregnan(?:t|cy)|maternity\s+leave|"
    r"(?:must|should|only|prefer)\s+be\s+(?:hindu|muslim|christian|jewish|sikh|buddhist|atheist)|"
    r"(?:not|exclude|no)\s+(?:hindu|muslim|christian|jewish|sikh|buddhist)|"
    r"able[-\s]bodied|\bdisabled\b"
    r")\b",
    re.IGNORECASE,
)


def _input_requests_protected_field(text: str) -> bool:
    if not text:
        return False
    return bool(_PROTECTED_TRIGGER_RE.search(text))


# ── Public entry point ────────────────────────────────────────────────────────

def validate_spec(
    raw: dict[str, Any],
    original_input: str = "",
    explicit_filters: dict[str, Any] | None = None,
) -> CanonicalSearchSpec:
    """Parse and validate raw planner dict → CanonicalSearchSpec."""

    dropped: list[dict] = []

    # Check for forbidden field attempts and log them
    _check_forbidden(raw, original_input, dropped)

    input_type = _coerce_input_type(raw.get("input_type"))
    intent     = _coerce_intent(raw.get("intent"))

    must     = _parse_must(raw.get("must") or {})
    should   = _parse_should(raw.get("should") or {})
    must_not = _parse_must_not(raw.get("must_not") or {})

    semantic_query = _clean_str(raw.get("semantic_query"))
    hyde_profile   = _clean_str(raw.get("hyde_profile")) or None
    lexical_terms  = _coerce_str_list(raw.get("lexical_terms"))
    search_targets = _coerce_search_targets(raw.get("search_targets"))
    confidence     = _coerce_float(raw.get("confidence"), 0.0)
    clarify        = _clean_str(raw.get("clarify")) or None
    personalization_offer = _coerce_personalization_offer(raw.get("personalization_offer"))

    # Skill hallucination check — skills in must that aren't in the input
    # get DEMOTED to should.skills rather than dropped. This preserves the
    # LLM's expansion intent (useful for HyDE / soft ranking) without letting
    # a hallucinated skill act as a hard filter that excludes good candidates.
    if original_input:
        must.skills, demoted = _verify_skills(must.skills, original_input)
        if demoted:
            existing = {s.lower() for s in should.skills}
            for s in demoted:
                if s.lower() not in existing:
                    should.skills.append(s)
                    existing.add(s.lower())

    spec = CanonicalSearchSpec(
        input_type=input_type,
        intent=intent,
        must=must,
        should=should,
        must_not=must_not,
        semantic_query=semantic_query,
        hyde_profile=hyde_profile,
        lexical_terms=lexical_terms,
        search_targets=search_targets,
        confidence=confidence,
        clarify=clarify,
        dropped_items=dropped,
        personalization_offer=personalization_offer,
    )

    # ── False-positive clarify override ──────────────────────────────────────
    # The LLM sometimes raises the protected-characteristics clarify when the
    # input only contains stylistic pronouns ("he should be from NY") or
    # seniority words. If the clarify message matches the canned protected-
    # class one BUT the input has no real trigger phrase AND the output JSON
    # didn't include any forbidden field keys, treat it as a false positive:
    # clear the clarify and restore a reasonable confidence.
    if (
        spec.clarify
        and spec.clarify.strip() == _PROTECTED_CHAR_CLARIFY
        and not _input_requests_protected_field(original_input)
        and not (set(raw.keys()) & FORBIDDEN_FIELDS)
    ):
        logger.info(
            "Suppressed false-positive protected-char clarify (input has no trigger phrase): %.80s",
            original_input,
        )
        spec.clarify = None
        if spec.confidence < 0.7:
            spec.confidence = 0.7

    # Merge explicit caller-provided filters (they always win over LLM output)
    if explicit_filters:
        spec = _merge_explicit(spec, explicit_filters)

    return spec


# ── Field parsers ─────────────────────────────────────────────────────────────

def _parse_must(d: dict) -> MustFilters:
    skills_match = str(d.get("skills_match") or "and").strip().lower()
    if skills_match not in {"and", "or"}:
        skills_match = "and"
    return MustFilters(
        skills        = _coerce_skill_list(d.get("skills")),
        skills_match  = skills_match,  # type: ignore[arg-type]
        country       = _clean_str(d.get("country")) or None,
        city          = _clean_str(d.get("city")) or None,
        min_years_exp = _coerce_int(d.get("min_years_exp")),
        max_years_exp = _coerce_int(d.get("max_years_exp")),
        applied_role  = _clean_str(d.get("applied_role")) or None,
        min_salary    = _coerce_int(d.get("min_salary")),
        max_salary    = _coerce_int(d.get("max_salary")),
        status        = _coerce_status_list(d.get("status")),
    )


def _parse_should(d: dict) -> ShouldFilters:
    return ShouldFilters(
        skills    = _coerce_skill_list(d.get("skills")),
        themes    = _coerce_str_list(d.get("themes")),
        locations = _coerce_str_list(d.get("locations")),
        roles     = _coerce_str_list(d.get("roles")),
    )


def _parse_must_not(d: dict) -> MustNotFilters:
    return MustNotFilters(
        skills    = _coerce_skill_list(d.get("skills")),
        # NOTE: must_not.status must default to EMPTY (exclude nothing). Using
        # _coerce_status_list here forced ['active'], which combined with the
        # must.status=['active'] default produced "status=active AND status!=active"
        # — an always-false filter that zeroed out every LLM-planned search.
        status    = [s.lower() for s in _coerce_str_list(d.get("status"))
                     if s.lower() in ALLOWED_STATUSES],
        companies = _coerce_str_list(d.get("companies")),
    )


# ── Explicit-filter merge ─────────────────────────────────────────────────────

def _merge_explicit(spec: CanonicalSearchSpec, explicit: dict[str, Any]) -> CanonicalSearchSpec:
    """Explicit caller filters always override LLM-extracted ones."""
    m = spec.must
    if explicit.get("country") is not None:
        m.country = str(explicit["country"]).strip().lower()
    if explicit.get("city") is not None:
        m.city = str(explicit["city"]).strip().lower()
    if explicit.get("min_years_exp") is not None:
        m.min_years_exp = int(explicit["min_years_exp"])
    if explicit.get("max_years_exp") is not None:
        m.max_years_exp = int(explicit["max_years_exp"])
    if explicit.get("min_salary") is not None:
        m.min_salary = int(explicit["min_salary"])
    if explicit.get("max_salary") is not None:
        m.max_salary = int(explicit["max_salary"])
    if "skills" in explicit:
        m.skills = _coerce_skill_list(explicit.get("skills"))
    if explicit.get("skills_match") in {"and", "or"}:
        m.skills_match = str(explicit["skills_match"])
    if explicit.get("status"):
        m.status = _coerce_status_list(explicit["status"])
    if explicit.get("applied_role"):
        m.applied_role = str(explicit["applied_role"]).strip()
    if explicit.get("skill_weights"):
        weights: dict[str, float] = {}
        for name, value in dict(explicit["skill_weights"]).items():
            try:
                w = float(value)
            except (TypeError, ValueError):
                continue
            key = str(name).strip().lower()
            if not key:
                continue
            # Accept either 0-1 or 0-100; normalise to 0-1.
            if w > 1.0:
                w = w / 100.0
            weights[key] = max(0.0, min(1.0, w))
        if weights:
            spec.skill_weights = weights
    _merge_explicit_should(spec, explicit)
    return spec


def _merge_explicit_should(spec: CanonicalSearchSpec, explicit: dict[str, Any]) -> None:
    should = spec.should
    nested = explicit.get("should") if isinstance(explicit.get("should"), dict) else {}

    if "skills" in nested:
        should.skills = _coerce_skill_list(nested.get("skills"))
    elif "should_skills" in explicit or "preferred_skills" in explicit:
        should.skills = _coerce_skill_list(
            explicit.get("should_skills", explicit.get("preferred_skills"))
        )
    if "themes" in nested:
        should.themes = _coerce_str_list(nested.get("themes"))
    elif "should_themes" in explicit:
        should.themes = _coerce_str_list(explicit.get("should_themes"))
    if "locations" in nested:
        should.locations = _coerce_str_list(nested.get("locations"))
    elif "should_locations" in explicit:
        should.locations = _coerce_str_list(explicit.get("should_locations"))
    if "roles" in nested:
        should.roles = _coerce_str_list(nested.get("roles"))
    elif "should_roles" in explicit:
        should.roles = _coerce_str_list(explicit.get("should_roles"))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_forbidden(raw: dict, original_input: str, dropped: list[dict]) -> None:
    """Detect forbidden field keys in the LLM output and record what was dropped."""
    for section, sub in raw.items():
        if not isinstance(sub, dict):
            continue
        for key in list(sub.keys()):
            if key in FORBIDDEN_FIELDS:
                dropped.append({
                    "section": section,
                    "field":   key,
                    "value":   sub.get(key),
                    "reason":  "forbidden — protected characteristic",
                })
    if any(d["reason"].startswith("forbidden") for d in dropped):
        forbidden_keys = [d["field"] for d in dropped if d["reason"].startswith("forbidden")]
        logger.warning(
            "Forbidden field(s) %s attempted in planner output for input: %.80s",
            forbidden_keys, original_input,
        )
        try:
            from pipeline import metrics as _metrics
            _metrics.incr("forbidden_field_attempts", len(forbidden_keys))
        except Exception:
            pass


def _build_reverse_aliases() -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for alias, canonical in SKILL_ALIASES.items():
        result.setdefault(canonical, set()).add(alias)
    return result


_REVERSE_SKILL_ALIASES: dict[str, set[str]] = _build_reverse_aliases()


def _verify_skills(skills: list[str], original_input: str) -> tuple[list[str], list[str]]:
    """Split must.skills into (verified, demoted).

    Verified skills appear (directly, via alias, or via known abbreviation) in
    the input text and stay in must.skills. Demoted skills don't appear and
    are returned for the caller to fold into should.skills instead.
    """
    input_lower = original_input.lower()
    verified: list[str] = []
    demoted:  list[str] = []
    for skill in skills:
        skill_lower = skill.lower()
        raw_form    = skill_lower.replace("-", " ")
        # Also check any known alias that maps to this canonical skill (e.g. "ml" → "machine-learning")
        known_aliases = _REVERSE_SKILL_ALIASES.get(skill_lower, set())
        in_input = (
            raw_form in input_lower
            or skill_lower in input_lower
            or any(alias in input_lower for alias in known_aliases)
        )
        is_known = skill_lower in KNOWN_SKILLS

        if in_input and is_known:
            verified.append(skill)
        elif not in_input:
            logger.debug("Demoted must.skill %r to should.skills (not in input)", skill)
            demoted.append(skill)
        else:
            # It is in the input, but it's an unknown/hallucinated skill not in our catalog
            logger.debug("Dropped must.skill %r entirely (not in known catalog)", skill)
    return verified, demoted


def _coerce_input_type(v: Any) -> str:
    return v if v in {"query", "jd", "filters_only", "lookup"} else "query"


def _coerce_intent(v: Any) -> str:
    return v if v in ALLOWED_INTENTS else "candidate_search"


def _coerce_float(v: Any, default: float = 0.0) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return default


def _coerce_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        n = int(v)
        return n if n >= 0 else None
    except (TypeError, ValueError):
        return None


def _clean_str(v: Any) -> str:
    if v is None:
        return ""
    s = re.sub(r"[\x00-\x1f\x7f]", " ", str(v))
    return re.sub(r"\s+", " ", s).strip()


def _coerce_str_list(v: Any) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        v = [v]
    return [_clean_str(s) for s in v if s and _clean_str(s)]


def _coerce_skill_list(v: Any) -> list[str]:
    raw = _coerce_str_list(v)
    seen: set[str] = set()
    out: list[str] = []
    for s in raw:
        n = normalize_skill(s)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _coerce_status_list(v: Any) -> list[str]:
    raw = _coerce_str_list(v)
    valid = [s.lower() for s in raw if s.lower() in ALLOWED_STATUSES]
    return valid or ["active"]


def _coerce_personalization_offer(v: Any) -> dict | None:
    """Accept {hint: str, question: str}; reject anything else."""
    if not isinstance(v, dict):
        return None
    hint     = _clean_str(v.get("hint"))
    question = _clean_str(v.get("question"))
    if not hint or not question:
        return None
    return {"hint": hint[:300], "question": question[:200]}


def _coerce_search_targets(v: Any) -> list[str]:
    raw = _coerce_str_list(v)
    valid = [t for t in raw if t in ALLOWED_SEARCH_TARGETS]
    return valid  # empty = "all targets" (handled downstream)
