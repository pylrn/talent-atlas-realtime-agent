"""The fast path: a grounded acknowledgement, produced before retrieval starts.

A voice agent has two latency budgets. The slow path — retrieve, fuse, rank,
enrich — is measured in seconds. The fast path is the few hundred milliseconds
in which the agent has to say *something*, or the caller concludes it stopped
listening.

The fast path is only safe if it cannot be wrong, so it is derived from the plan
diff rather than from the corpus. "Removing the Bangalore filter and keeping the
skill requirements" is a statement about an instruction the agent has just
received, so it is true the moment it is said. "I found three candidates" is a
statement about evidence that does not exist yet, and no amount of speed makes
it true.

Two consequences follow, and both are deliberate:

* The acknowledgement never echoes free text from the recruiter. A query can
  contain a number — "find 3 engineers" — and quoting it back would turn the
  fast path into a result claim. Only structured constraint values, which come
  from the plan, are ever spoken.

* Every acknowledgement is classified by the same auditor that judges filler
  speech while a tool is in flight. The fast path audits itself, so a phrasing
  that would be a violation elsewhere cannot slip through here.
"""

from __future__ import annotations

from typing import Any

from pipeline.realtime_filler import classify_filler
from pipeline.realtime_plan import SearchPlanRevision

# The acknowledgement must fit in a breath. Anything longer is the slow path's
# job to say, once it has evidence.
MAX_WORDS = 24

# Constraint fields, in the order a recruiter would hear them.
CONSTRAINT_FIELDS = (
    "city",
    "country",
    "min_years_exp",
    "max_years_exp",
    "must_skills",
    "should_skills",
    "excluded_skills",
    "should_locations",
    "should_themes",
    "should_roles",
)


def _phrase(field: str, value: Any) -> str:
    if field == "city":
        return f"the {value} location filter"
    if field == "country":
        return f"the {value} country filter"
    if field == "min_years_exp":
        return f"the {value} year minimum"
    if field == "max_years_exp":
        return f"the {value} year maximum"
    if field == "must_skills":
        return "the required skills"
    if field == "should_skills":
        return "the preferred skills"
    if field == "excluded_skills":
        return "the exclusion list"
    if field == "should_locations":
        return "the preferred locations"
    if field == "should_themes":
        return "the theme hints"
    if field == "should_roles":
        return "the role hints"
    return field.replace("_", " ")


def _join(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return f"{', '.join(parts[:-1])}, and {parts[-1]}"


def _present(revision: SearchPlanRevision, field: str) -> bool:
    value = getattr(revision, field, None)
    return value not in (None, "", [])


def describe_constraints(revision: SearchPlanRevision) -> list[str]:
    return [
        _phrase(field, getattr(revision, field))
        for field in CONSTRAINT_FIELDS
        if _present(revision, field)
    ]


def acknowledge(
    previous: SearchPlanRevision | None,
    revision: SearchPlanRevision,
    *,
    changed_fields: list[str] | tuple[str, ...] = (),
    replaces_goal: bool = False,
) -> dict[str, Any]:
    """Build the one sentence the agent may say before evidence exists.

    Returns a report rather than a string, because the caller needs to know how
    long the fast path took and whether the sentence passed its own audit.
    """
    if previous is None or replaces_goal:
        parts = describe_constraints(revision)
        text = (
            f"Starting a fresh search with {_join(parts)}."
            if parts
            else "Starting a fresh search."
        )
    else:
        removed = [
            field
            for field in CONSTRAINT_FIELDS
            if _present(previous, field) and not _present(revision, field)
        ]
        added = [
            field
            for field in CONSTRAINT_FIELDS
            if _present(revision, field) and not _present(previous, field)
        ]
        clauses: list[str] = []
        if removed:
            clauses.append("removing " + _join([_phrase(f, getattr(previous, f)) for f in removed]))
        if added:
            clauses.append("adding " + _join([_phrase(f, getattr(revision, f)) for f in added]))
        if "query" in set(changed_fields):
            clauses.append("updating the search wording")
        if not clauses:
            clauses.append("keeping the same criteria")
        text = "Got it — " + _join(clauses) + "."

    text = _fit(text)
    report = classify_filler(text)
    return {
        "text": text,
        "word_count": len(text.split()),
        "changed_fields": sorted(str(field) for field in changed_fields),
        "grounded": report["grounded"],
        "kind": report["kind"],
        "audit_reason": report["reason"],
        "policy": (
            "The fast path may describe the instruction it just received and nothing "
            "about the result. Outcome claims belong to the slow path, after evidence."
        ),
    }


def _fit(text: str) -> str:
    """Trim to the word budget without cutting mid-clause where possible."""
    words = text.split()
    if len(words) <= MAX_WORDS:
        return text
    trimmed = " ".join(words[:MAX_WORDS]).rstrip(" ,;:—-")
    return f"{trimmed}."
