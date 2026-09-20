"""Build parameterised SQL WHERE clauses from MustFilters.

Returns (clause_str, params_list) compatible with asyncpg.
"""

from __future__ import annotations

import re
from typing import Any

from pipeline.spec import MustFilters


_COUNTRY_EQUIVALENTS = {
    "us": ["USA", "US", "United States", "United States of America", "America", "The States"],
    "usa": ["USA", "US", "United States", "United States of America", "America", "The States"],
    "united states": ["USA", "US", "United States", "United States of America", "America", "The States"],
    "united states of america": ["USA", "US", "United States", "United States of America", "America", "The States"],
    "america": ["USA", "US", "United States", "United States of America", "America", "The States"],
    "the states": ["USA", "US", "United States", "United States of America", "America", "The States"],
    "uk": ["UK", "United Kingdom", "Britain", "Great Britain", "England"],
    "united kingdom": ["UK", "United Kingdom", "Britain", "Great Britain", "England"],
    "britain": ["UK", "United Kingdom", "Britain", "Great Britain", "England"],
    "great britain": ["UK", "United Kingdom", "Britain", "Great Britain", "England"],
    "england": ["UK", "United Kingdom", "Britain", "Great Britain", "England"],
}


def _case_variants(value: str) -> list[str]:
    variants = [
        value,
        value.title(),
        value.upper(),
        value.lower(),
    ]
    return list(dict.fromkeys(v for v in variants if v))


def _country_match_values(raw: str) -> list[str]:
    cleaned = raw.strip().lower().replace("-", " ").replace("_", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return _COUNTRY_EQUIVALENTS.get(cleaned, _case_variants(cleaned))


def build_filter_sql(must: MustFilters) -> tuple[str, list[Any]]:
    """Return (WHERE clause, params).  The clause always starts with 'status = $1'."""
    clauses: list[str] = []
    params:  list[Any] = []
    idx = 1

    # status (always applied — never relaxable)
    status = must.status or ["active"]
    clauses.append(f"status = ANY(${idx}::text[])")
    params.append(status)
    idx += 1

    if must.country:
        # Treat common country aliases as equivalent so planner-normalized "us"
        # still matches the DB value "USA", while preserving btree index use.
        clauses.append(f"country = ANY(${idx}::text[])")
        params.append(_country_match_values(must.country))
        idx += 1

    if must.city:
        clauses.append(
            f"LOWER(REPLACE(city, '-', ' ')) = LOWER(REPLACE(${idx}, '-', ' '))"
        )
        params.append(must.city)
        idx += 1

    if must.min_years_exp is not None:
        clauses.append(f"years_exp >= ${idx}")
        params.append(must.min_years_exp)
        idx += 1

    if must.max_years_exp is not None:
        clauses.append(f"years_exp <= ${idx}")
        params.append(must.max_years_exp)
        idx += 1

    if must.min_salary is not None:
        clauses.append(f"salary_max >= ${idx}")
        params.append(must.min_salary)
        idx += 1

    if must.max_salary is not None:
        clauses.append(f"salary_min <= ${idx}")
        params.append(must.max_salary)
        idx += 1

    if must.skills:
        # must.skills defaults to AND semantics, but auto-relax can switch it
        # to OR so direct API callers do not hit a silent zero-result wall.
        clauses.append(
            f"skills @> ${idx}::text[]"
            if getattr(must, "skills_match", "and") == "and"
            else f"skills && ${idx}::text[]"
        )
        params.append(must.skills)
        idx += 1

    # applied_role column does not exist in live DB — filter intentionally omitted

    where = " AND ".join(clauses) if clauses else "TRUE"
    return where, params


def build_candidate_filter(must: MustFilters, must_not) -> tuple[str, list[Any]]:
    """Return (where_clause, params) combining must + must_not filters.

    Suitable for inlining as a CTE inside retrieval queries:
        WITH filtered AS (SELECT id FROM candidates WHERE {clause}) ...
    """
    where, params = build_filter_sql(must)
    not_clause, params = build_must_not_sql(must_not, params)
    if not_clause:
        return f"{where} AND {not_clause}", params
    return where, params


def build_must_not_sql(must_not, existing_params: list[Any]) -> tuple[str, list[Any]]:
    """Return extra NOT clauses from must_not (appended to existing params)."""
    clauses: list[str] = []
    params = list(existing_params)
    idx = len(params) + 1

    if must_not.status:
        clauses.append(f"status != ALL(${idx}::text[])")
        params.append(must_not.status)
        idx += 1

    if must_not.skills:
        clauses.append(f"NOT (skills && ${idx}::text[])")
        params.append(must_not.skills)
        idx += 1

    return " AND ".join(clauses), params
