"""RRF fusion of multiple retrieval result lists.

Combines dense, BM25, and skill result lists using Reciprocal Rank Fusion.
Search targets that match spec.search_targets get full weight (1.0);
others get half weight (0.5) — never filtered out (recall safety).
"""

from __future__ import annotations

from typing import Any

from pipeline.constants import SEARCH_TARGET_DOC_TYPES, RRF_K


def rrf_fuse(
    dense_rows:  list[dict[str, Any]],
    bm25_rows:   list[dict[str, Any]],
    skill_rows:  list[dict[str, Any]],
    spec_targets: list[str],
    rrf_k: int = RRF_K,
    use_search_target_weighting: bool = True,
) -> list[dict[str, Any]]:
    """Return a fused, chunk-level ranked list.

    Each entry has keys: chunk_id, candidate_id, content, document_id,
    doc_type, document_title, distance, fused_rrf_score, retrieval_paths.
    """
    # Build doc_type → target_weight mapping
    target_doc_types: set[str] = set()
    for t in spec_targets:
        target_doc_types.update(SEARCH_TARGET_DOC_TYPES.get(t, []))

    def target_weight(doc_type: str) -> float:
        if not use_search_target_weighting or not spec_targets:
            return 1.0
        return 1.0 if doc_type in target_doc_types else 0.5

    # Assign per-source ranks and RRF scores
    chunk_scores: dict[str, dict[str, Any]] = {}

    def _accumulate(rows: list[dict], path_name: str) -> None:
        for rank, row in enumerate(rows, start=1):
            cid = row["chunk_id"]
            w   = target_weight(row.get("doc_type") or "")
            contribution = w / (rrf_k + rank)

            if cid not in chunk_scores:
                chunk_scores[cid] = {
                    **row,
                    "fused_rrf_score":  0.0,
                    "retrieval_paths": [],
                }
            chunk_scores[cid]["fused_rrf_score"] += contribution
            if path_name not in chunk_scores[cid]["retrieval_paths"]:
                chunk_scores[cid]["retrieval_paths"].append(path_name)

    _accumulate(dense_rows,  "dense")
    _accumulate(bm25_rows,   "bm25")
    _accumulate(skill_rows,  "skill")

    fused = list(chunk_scores.values())
    fused.sort(key=lambda r: (
        -float(r.get("fused_rrf_score") or 0.0),
        str(r.get("candidate_id") or ""),
        str(r.get("chunk_id") or ""),
    ))
    return fused
