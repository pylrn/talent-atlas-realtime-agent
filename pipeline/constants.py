"""Pipeline-wide constants. Version strings are appended to cache keys so
bumping a version automatically invalidates stale cached entries."""

from __future__ import annotations

# ── Cache versioning ──────────────────────────────────────────────────────────
PLANNER_VERSION          = "v2"
EMBEDDING_MODEL_VERSION  = "minilm-l6-v2"
HYDE_VERSION             = "v1"

# ── Allowed / forbidden field sets ───────────────────────────────────────────
ALLOWED_FILTERS: frozenset[str] = frozenset({
    "status", "country", "city",
    "min_years_exp", "max_years_exp",
    "skills",
    "min_salary", "max_salary",
})

ALLOWED_STATUSES: frozenset[str] = frozenset({
    "active", "archived", "hired", "rejected",
})

ALLOWED_SEARCH_TARGETS: frozenset[str] = frozenset({
    "candidate_profile",
    "resume_chunks",
    "recruiter_notes",
    "interview_notes",
    "project_chunks",
    "application_forms",
})

ALLOWED_INTENTS: frozenset[str] = frozenset({
    "candidate_lookup",
    "candidate_filter",
    "candidate_search",
    "candidate_comparison",
    "candidate_explanation",
    "analytics_query",
})

# Protected characteristics — never extract even if implied by query
FORBIDDEN_FIELDS: frozenset[str] = frozenset({
    "gender", "age", "nationality", "race",
    "religion", "marital_status", "disability", "pregnancy",
})

# ── Search target → doc_type mapping (what exists in candidate_documents) ────
SEARCH_TARGET_DOC_TYPES: dict[str, list[str]] = {
    "candidate_profile":  [],                          # structured row, no doc_type
    "resume_chunks":      ["resume"],
    "recruiter_notes":    ["other"],
    "interview_notes":    ["transcript"],
    "project_chunks":     ["bio", "other"],
    "application_forms":  ["cover_letter"],
}


# ── Numeric thresholds ────────────────────────────────────────────────────────
CONFIDENCE_THRESHOLD     = 0.6
MMR_LAMBDA               = 0.7
RRF_K                    = 60                          # standard constant

# ── Explanation tier thresholds (final_score 0-100) ──────────────────────────
TIER_STRONG  = 85
TIER_GOOD    = 70
TIER_PARTIAL = 55
