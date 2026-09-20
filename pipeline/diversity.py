"""MMR diversity pass (toggleable).

Maximal Marginal Relevance re-selects top_k candidates trading off
relevance (feature_score) against similarity to already-selected candidates,
preventing near-duplicate results from crowding the top slots.

score_i = λ · feature_score_i − (1 − λ) · max_cos(embed_i, already_selected)

λ = 0.7 (default) means 70% relevance, 30% diversity.
"""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any

from pipeline import cache as _cache
from pipeline.search_result import SearchResult

logger = logging.getLogger(__name__)


def mmr_select(
    results: list[SearchResult],
    top_k: int,
    lam: float = 0.7,
    embedder: Any | None = None,
) -> list[SearchResult]:
    """Return top_k candidates via MMR.

    Without embedder (default): uses skill-set Jaccard as proxy for similarity
    so we don't need an extra embedding call.
    """
    if not results or top_k >= len(results):
        return results[:top_k]

    selected: list[SearchResult] = []
    remaining = list(results)

    while remaining and len(selected) < top_k:
        best_idx   = -1
        best_score = float("-inf")

        for i, cand in enumerate(remaining):
            relevance = cand.feature_score / 100.0
            if selected:
                max_sim = max(_skill_similarity(cand, s) for s in selected)
            else:
                max_sim = 0.0

            mmr_score = lam * relevance - (1.0 - lam) * max_sim

            if mmr_score > best_score:
                best_score = mmr_score
                best_idx   = i

        if best_idx == -1:
            break
        selected.append(remaining.pop(best_idx))

    return selected


async def mmr_select_async(
    results: list[SearchResult],
    top_k: int,
    lam: float = 0.7,
    embedder: Any | None = None,
    *,
    use_cache: bool = True,
) -> list[SearchResult]:
    """Return top_k candidates via MMR using blended skill + embedding similarity.

    Falls back to the existing skill-Jaccard-only selection whenever candidate
    embeddings cannot be produced.
    """
    if not results or top_k >= len(results):
        return results[:top_k]
    if embedder is None:
        return mmr_select(results, top_k, lam=lam)

    embeddings = await _candidate_embeddings(results, embedder, use_cache=use_cache)
    if not embeddings:
        return mmr_select(results, top_k, lam=lam)

    selected: list[SearchResult] = []
    remaining = list(results)

    while remaining and len(selected) < top_k:
        best_idx = -1
        best_score = float("-inf")

        for i, cand in enumerate(remaining):
            relevance = cand.feature_score / 100.0
            if selected:
                max_sim = max(_blended_similarity(cand, s, embeddings) for s in selected)
            else:
                max_sim = 0.0
            mmr_score = lam * relevance - (1.0 - lam) * max_sim
            if mmr_score > best_score:
                best_score = mmr_score
                best_idx = i

        if best_idx == -1:
            break
        selected.append(remaining.pop(best_idx))

    return selected


def _skill_similarity(a: SearchResult, b: SearchResult) -> float:
    """Jaccard similarity between two candidates' skill sets."""
    sa = {s.lower() for s in a.skills}
    sb = {s.lower() for s in b.skills}
    if not sa and not sb:
        return 0.0
    union = sa | sb
    inter = sa & sb
    return len(inter) / len(union)


def _profile_signature(result: SearchResult) -> str:
    chunks = [result.best_chunk]
    chunks.extend(
        chunk.get("content", "")
        for chunk in (result.supporting_chunks or [])[:2]
        if isinstance(chunk, dict)
    )
    skills = ", ".join(result.skills[:12])
    parts = [
        f"skills: {skills}" if skills else "",
        f"location: {result.city or ''} {result.country or ''}".strip(),
        " ".join(chunks).strip(),
    ]
    return "\n".join(part for part in parts if part).strip()[:2400]


async def _candidate_embeddings(
    results: list[SearchResult],
    embedder: Any,
    *,
    use_cache: bool,
) -> dict[str, list[float]]:
    texts_by_id = {
        result.candidate_id: _profile_signature(result)
        for result in results
        if result.candidate_id and _profile_signature(result)
    }
    if not texts_by_id:
        return {}

    cached: dict[str, list[float]] = {}
    missing_ids: list[str] = []
    missing_texts: list[str] = []
    provider_name = getattr(embedder, "name", "")
    dims = getattr(embedder, "dimensions", None)

    if use_cache:
        for candidate_id, text in texts_by_id.items():
            key = _cache.embed_key(text, provider=provider_name, dimensions=dims)
            value = await _cache.get(key)
            if isinstance(value, list) and value:
                cached[candidate_id] = value
            else:
                missing_ids.append(candidate_id)
                missing_texts.append(text)
    else:
        missing_ids = list(texts_by_id.keys())
        missing_texts = [texts_by_id[candidate_id] for candidate_id in missing_ids]

    if missing_texts:
        try:
            produced = await embedder.embed(missing_texts)
        except Exception as exc:
            logger.debug("MMR embedding generation failed: %s", exc)
            return cached
        for candidate_id, text, vector in zip(missing_ids, missing_texts, produced):
            if not isinstance(vector, list) or not vector:
                continue
            cached[candidate_id] = vector
            if use_cache:
                await _cache.set(
                    _cache.embed_key(text, provider=provider_name, dimensions=dims),
                    vector,
                )
    return cached


def _blended_similarity(
    a: SearchResult,
    b: SearchResult,
    embeddings: dict[str, list[float]],
) -> float:
    skill_sim = _skill_similarity(a, b)
    vec_a = embeddings.get(a.candidate_id)
    vec_b = embeddings.get(b.candidate_id)
    if not vec_a or not vec_b:
        return skill_sim
    return 0.5 * skill_sim + 0.5 * _cosine_similarity(vec_a, vec_b)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a <= 1e-12 or norm_b <= 1e-12:
        return 0.0
    return max(0.0, min(1.0, dot / (norm_a * norm_b)))
