"""Route detector — heuristic, no LLM, sub-millisecond.

Classifies the raw input into a route string so downstream stages
know which planner prompt + pipeline path to use.
"""

from __future__ import annotations

import re

_NAME_RE    = re.compile(r"^[A-Z][a-z]+ [A-Z][a-z]+$")          # "Malcolm Price"
_FILTER_TOK = re.compile(r"^(active|archived|hired|rejected|uk|us|india|[a-z]+)$", re.I)

_FILTER_ONLY_WORDS = {
    "active", "archived", "hired", "rejected",
    "in", "from", "with", "and", "or", "candidates",
}


def detect_route(
    query: str | None = None,
    jd: str | None = None,
    explicit_filters: dict | None = None,
) -> str:
    """Return one of: lookup | jd_mode | query_mode | filters_only.

    Priority:
      1. If jd is provided → jd_mode
      2. If query looks like a name → lookup
      3. If query is empty / filter-only words → filters_only
      4. If query is long (≥50 words) → jd_mode
      5. Otherwise → query_mode
    """
    if jd and jd.strip():
        return "jd_mode"

    text = (query or "").strip()

    if not text:
        return "filters_only"

    if _NAME_RE.match(text):
        return "lookup"

    words = text.split()
    if len(words) >= 50:
        return "jd_mode"

    # All tokens look like filter keywords
    non_filter = [w for w in words if w.lower() not in _FILTER_ONLY_WORDS]
    if not non_filter:
        return "filters_only"

    return "query_mode"
