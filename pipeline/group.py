"""Group chunk-level fusion results into one SearchResult per candidate.

Keeps the best chunk + up to 2 supporting chunks from different documents.
"""

from __future__ import annotations

from typing import Any

from pipeline.search_result import SearchResult


def group_by_candidate(
    fused_rows: list[dict[str, Any]],
    max_supporting: int = 2,
) -> list[SearchResult]:
    """Deduplicate to one SearchResult per candidate_id.

    Best chunk = highest fused_rrf_score.
    Supporting chunks = next best, preferring different documents.
    """
    # Group rows by candidate
    candidates: dict[str, list[dict]] = {}
    for row in fused_rows:
        cid = row["candidate_id"]
        if cid not in candidates:
            candidates[cid] = []
        candidates[cid].append(row)

    results: list[SearchResult] = []

    for cid, rows in candidates.items():
        rows.sort(key=lambda r: (
            -float(r.get("fused_rrf_score") or 0.0),
            str(r.get("candidate_id") or ""),
            str(r.get("chunk_id") or ""),
        ))
        best = rows[0]

        # Gather supporting chunks — prefer different documents
        seen_docs: set[str] = {best["document_id"]}
        supporting: list[dict] = []

        for row in rows[1:]:
            if len(supporting) >= max_supporting:
                break
            if row["document_id"] not in seen_docs:
                supporting.append({
                    "chunk_id":       str(row.get("chunk_id") or ""),
                    "document_id":    str(row.get("document_id") or ""),
                    "content":        row["content"],
                    "doc_type":       row.get("doc_type"),
                    "document_title": row.get("document_title"),
                    "fused_rrf_score": row["fused_rrf_score"],
                })
                seen_docs.add(row["document_id"])

        # Fill remaining slots from same doc if needed
        for row in rows[1:]:
            if len(supporting) >= max_supporting:
                break
            if row["document_id"] == best["document_id"] and row["chunk_id"] != best["chunk_id"]:
                supporting.append({
                    "chunk_id":       str(row.get("chunk_id") or ""),
                    "document_id":    str(row.get("document_id") or ""),
                    "content":        row["content"],
                    "doc_type":       row.get("doc_type"),
                    "document_title": row.get("document_title"),
                    "fused_rrf_score": row["fused_rrf_score"],
                })

        results.append(SearchResult(
            candidate_id      = cid,
            full_name         = best.get("full_name") or "",
            email             = best.get("email"),
            city              = best.get("city"),
            country           = best.get("country"),
            salary_min        = best.get("salary_min"),
            salary_max        = best.get("salary_max"),
            years_exp         = int(best.get("years_exp") or 0),
            skills            = list(best.get("skills") or []),
            best_chunk        = best["content"],
            best_chunk_id     = str(best.get("chunk_id") or "") or None,
            best_document_id  = str(best.get("document_id") or "") or None,
            best_chunk_distance = float(best.get("distance") or 1.0),
            doc_type          = best.get("doc_type") or "",
            document_title    = best.get("document_title"),
            supporting_chunks = supporting,
            fused_rrf_score   = best["fused_rrf_score"],
            retrieval_paths   = best.get("retrieval_paths", []),
        ))

    results.sort(key=lambda r: (
        -float(r.fused_rrf_score or 0.0),
        str(r.candidate_id or ""),
    ))
    return results
