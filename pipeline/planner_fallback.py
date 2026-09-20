"""Deterministic fallback planner — no LLM required.

Used when:
  - LLM is disabled (use_llm_planner = False)
  - LLM call fails
  - Input is trivially short (pure filter tokens)

Extracts hard constraints via regex; everything else goes into semantic_query.
Returns confidence = 0.5 so the confidence gate lets it through.
"""

from __future__ import annotations

import re

from pipeline.aliases import DISPLAY_SKILL_ALLOWLIST, SKILL_ALIASES, KNOWN_SKILLS, normalize_skill, normalize_location
from pipeline.spec import CanonicalSearchSpec, MustFilters, ShouldFilters, MustNotFilters

_YEARS_RE  = re.compile(r"(\d+)\s*\+?\s*(?:years?|yrs?)\b", re.I)
_CITY_HINTS = {
    "bangalore", "mumbai", "delhi", "hyderabad", "chennai", "pune",
    "london", "manchester", "edinburgh", "glasgow",
    "new york", "san francisco", "seattle", "austin", "chicago",
    "berlin", "amsterdam", "paris", "toronto", "singapore",
    "dubai", "sydney", "melbourne",
}
_COUNTRY_HINTS = {
    "india", "uk", "us", "usa", "germany", "france",
    "canada", "australia", "singapore", "uae",
}
_STATUS_RE = re.compile(r"\b(active|archived|hired|rejected)\b", re.I)




def fallback_plan(
    raw_input: str,
    explicit_filters: dict | None = None,
) -> CanonicalSearchSpec:
    text = raw_input.strip()
    tokens = re.findall(r"[a-z0-9][a-z0-9.\-+#]*", text.lower())

    # extract years
    min_years: int | None = None
    m = _YEARS_RE.search(text)
    if m:
        min_years = int(m.group(1))

    # extract status
    sm = _STATUS_RE.search(text)
    status = [sm.group(1).lower()] if sm else ["active"]

    # extract city / country from known hints
    city = next((normalize_location(t) for t in tokens if t in _CITY_HINTS), None)
    country = next((normalize_location(t) for t in tokens if t in _COUNTRY_HINTS), None)

    # extract skills that appear in the known vocabulary (single-token pass)
    found_skills = [
        normalize_skill(t) for t in tokens if normalize_skill(t) in KNOWN_SKILLS
    ]

    # multi-word alias pass: catch phrases like "machine learning", "deep learning", etc.
    text_lower = text.lower()
    for alias, canonical in SKILL_ALIASES.items():
        if " " in alias and alias in text_lower and canonical in KNOWN_SKILLS:
            found_skills.append(canonical)

    found_skills = list(dict.fromkeys(found_skills))  # dedupe, preserve order

    must = MustFilters(
        skills        = found_skills,
        country       = country,
        city          = city,
        min_years_exp = min_years,
        status        = status,
    )

    # build semantic query from non-structural tokens
    skip = set(found_skills) | {city, country} | {str(min_years) if min_years else ""}
    sem_tokens = [t for t in tokens if t not in skip and len(t) > 2]
    semantic_query = " ".join(sem_tokens[:12]) or text[:80]

    spec = CanonicalSearchSpec(
        input_type     = "query",
        intent         = "candidate_search",
        must           = must,
        should         = ShouldFilters(),
        must_not       = MustNotFilters(),
        semantic_query = semantic_query,
        hyde_profile   = None,
        lexical_terms  = found_skills[:6],
        search_targets = ["candidate_profile", "resume_chunks"],
        confidence     = 0.5,
        used_fallback  = True,
    )

    # Merge explicit overrides
    if explicit_filters:
        from pipeline.validator import _merge_explicit
        spec = _merge_explicit(spec, explicit_filters)

    return spec
