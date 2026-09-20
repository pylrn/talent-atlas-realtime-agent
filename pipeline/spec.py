"""CanonicalSearchSpec — the single contract passed between all pipeline stages."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional


@dataclass
class MustFilters:
    skills:        list[str]       = field(default_factory=list)
    skills_match:  Literal["and", "or"] = "and"
    country:       Optional[str]   = None
    city:          Optional[str]   = None
    min_years_exp: Optional[int]   = None
    max_years_exp: Optional[int]   = None
    applied_role:  Optional[str]   = None
    min_salary:    Optional[int]   = None
    max_salary:    Optional[int]   = None
    status:        list[str]       = field(default_factory=lambda: ["active"])


@dataclass
class ShouldFilters:
    skills:    list[str] = field(default_factory=list)
    themes:    list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    roles:     list[str] = field(default_factory=list)


@dataclass
class MustNotFilters:
    skills:    list[str] = field(default_factory=list)
    status:    list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)


InputType = Literal["query", "jd", "filters_only", "lookup"]
IntentType = Literal[
    "candidate_lookup",
    "candidate_filter",
    "candidate_search",
    "candidate_comparison",
    "candidate_explanation",
    "analytics_query",
]


@dataclass
class CanonicalSearchSpec:
    input_type:     InputType
    intent:         IntentType
    must:           MustFilters      = field(default_factory=MustFilters)
    should:         ShouldFilters    = field(default_factory=ShouldFilters)
    must_not:       MustNotFilters   = field(default_factory=MustNotFilters)
    semantic_query: str              = ""
    hyde_profile:   Optional[str]   = None
    lexical_terms:  list[str]        = field(default_factory=list)
    search_targets: list[str]        = field(default_factory=list)
    confidence:     float            = 0.0
    clarify:        Optional[str]   = None
    used_fallback:  bool             = False
    planner_error:  Optional[str]   = None
    skill_weights:  dict[str, float] = field(default_factory=dict)
    # Things the validator threw out of the LLM's raw output. Each item is
    # {"section": "...", "field": "...", "value": "...", "reason": "..."}.
    # Surfaced in the API response so the UI can tell the user what was
    # silently filtered.
    dropped_items:  list[dict]       = field(default_factory=list)
    # Optional "want me to remember this?" offer the LLM may emit. Shape:
    # {"hint": "<observation>", "question": "<yes/no question>"} or None.
    personalization_offer: Optional[dict] = None

    def embed_text(self) -> str:
        """Return the text that should be embedded for dense retrieval."""
        if self.hyde_profile:
            return self.hyde_profile
        return self.semantic_query or ""

    def is_filter_only(self) -> bool:
        return self.input_type == "filters_only" or not self.semantic_query

    def to_dict(self) -> dict:
        import dataclasses
        return dataclasses.asdict(self)
