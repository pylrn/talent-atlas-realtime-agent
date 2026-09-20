"""SearchResult dataclass — one per candidate after deduplication."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SearchResult:
    # ── Identity ──────────────────────────────────────────────────────────────
    candidate_id:   str
    impression_id:  Optional[str]    = None   # assigned at impression-logging time
    full_name:      str              = ""
    email:          Optional[str]    = None
    city:           Optional[str]    = None
    country:        Optional[str]    = None
    salary_min:     Optional[int]    = None
    salary_max:     Optional[int]    = None
    years_exp:      int              = 0
    skills:         list[str]        = field(default_factory=list)
    matched_skill_recency_days: Optional[float] = None

    # ── Best matching evidence ────────────────────────────────────────────────
    best_chunk:          str              = ""
    best_chunk_id:       Optional[str]    = None
    best_document_id:    Optional[str]    = None
    best_chunk_distance: float            = 1.0
    doc_type:            str              = ""
    document_title:      Optional[str]    = None
    supporting_chunks:   list[dict]       = field(default_factory=list)

    # ── Retrieval scores ──────────────────────────────────────────────────────
    fused_rrf_score:  float            = 0.0
    similarity_score: float            = 0.0   # 1 - best_chunk_distance

    # ── Rerank / feature scores ───────────────────────────────────────────────
    rerank_score:    Optional[float]   = None
    feature_score:   float             = 0.0   # final weighted score 0-100
    sort_basis:      str               = "fused_rrf_score"

    # Legacy names kept for old tests/scripts/API helpers.
    rank_score: Optional[float] = None
    rrf_score:  Optional[float] = None

    # ── Observability ─────────────────────────────────────────────────────────
    retrieval_paths:  list[str]  = field(default_factory=list)
    ranking_signals:  list[dict] = field(default_factory=list)

    # ── Explanation block (filled by explanation.py) ──────────────────────────
    explanation: Optional[dict] = None

    def __post_init__(self) -> None:
        if self.rrf_score is not None and not self.fused_rrf_score:
            self.fused_rrf_score = float(self.rrf_score)
        elif self.rrf_score is None:
            self.rrf_score = self.fused_rrf_score

        if self.rank_score is not None and not self.feature_score:
            self.feature_score = float(self.rank_score)
        elif self.rank_score is None:
            self.rank_score = self.feature_score
