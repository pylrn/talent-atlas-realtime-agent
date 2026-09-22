"""Stage: Cross-encoder re-ranking (toggleable).

After RRF fusion, re-scores the top-N candidates using a cross-encoder
model that processes (query, candidate evidence) jointly — much more
accurate than cosine similarity alone.

Supported models:
  local-fast     → cross-encoder/ms-marco-MiniLM-L-6-v2  (default, free)
  local-balanced → cross-encoder/ms-marco-MiniLM-L-12-v2
  local-best     → BAAI/bge-reranker-v2-m3
  cohere         → Cohere Rerank API (paid)
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import List

from pipeline.search_result import SearchResult

logger = logging.getLogger(__name__)

RERANKER_CATALOG = {
    "local-fast":     ("cross-encoder/ms-marco-MiniLM-L-6-v2",  "$0, ~30ms, good quality"),
    "local-balanced": ("cross-encoder/ms-marco-MiniLM-L-12-v2", "$0, ~60ms, better quality"),
    "local-best":     ("BAAI/bge-reranker-v2-m3",               "$0, ~80ms, best local"),
    "cohere":         ("rerank-english-v3.0",                    "$1/1K queries, excellent"),
}

_RERANK_DOCUMENT_MAX_CHARS = 2400


def _build_rerank_document(result: SearchResult) -> str:
    """Build a compact, evidence-rich document for pairwise reranking.

    Cross-encoder limits are token based. The previous 512-character slice
    used only a fraction of the model context and could cut the decisive skill
    evidence out of long resume chunks.
    """
    location = ", ".join(part for part in (result.city, result.country) if part)
    profile = [
        f"Candidate: {result.full_name}" if result.full_name else "",
        f"Location: {location}" if location else "",
        f"Experience: {result.years_exp} years experience" if result.years_exp > 0 else "",
        f"Skills: {', '.join(result.skills)}" if result.skills else "",
    ]
    evidence = [result.best_chunk]
    evidence.extend(chunk.get("content", "") for chunk in result.supporting_chunks[:2])
    text = "\n".join(part for part in profile + evidence if part)
    return text[:_RERANK_DOCUMENT_MAX_CHARS]


class Reranker(ABC):
    @abstractmethod
    async def rerank(self, query: str, results: List[SearchResult]) -> List[SearchResult]:
        ...

    async def warmup(self) -> None:
        """Force model graph compilation / first-token init. Default: no-op."""
        return None


class LocalCrossEncoderReranker(Reranker):
    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        try:
            from sentence_transformers import CrossEncoder
        except ImportError:
            raise ImportError("pip install sentence-transformers")
        logger.info("Loading cross-encoder: %s", model_name)
        self.model = CrossEncoder(model_name)
        self._name = model_name

    async def rerank(self, query: str, results: List[SearchResult]) -> List[SearchResult]:
        if not results:
            return results

        pairs = []
        for r in results:
            pairs.append((query, _build_rerank_document(r)))

        loop   = asyncio.get_running_loop()
        scores = await loop.run_in_executor(
            None, lambda: self.model.predict(pairs, batch_size=32).tolist()
        )

        for r, score in zip(results, scores):
            r.rerank_score = float(score)

        results.sort(key=lambda r: (
            -float(r.rerank_score or 0.0),
            str(r.candidate_id or ""),
        ))
        for r in results:
            r.sort_basis = "rerank_score"

        logger.info("Re-ranked %d results with %s", len(results), self._name)
        return results

    async def warmup(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: self.model.predict([("warmup query", "warmup document")], batch_size=1),
        )


class FastEmbedCrossEncoderReranker(Reranker):
    """Compact ONNX cross-encoder used by the free demo deployment."""

    MODEL_NAMES = {
        "cross-encoder/ms-marco-MiniLM-L-6-v2": "Xenova/ms-marco-MiniLM-L-6-v2",
        "cross-encoder/ms-marco-MiniLM-L-12-v2": "Xenova/ms-marco-MiniLM-L-12-v2",
    }

    def __init__(self, model_name: str):
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError as exc:
            raise ImportError("fastembed required for the lightweight reranker backend") from exc

        try:
            fastembed_name = self.MODEL_NAMES[model_name]
        except KeyError as exc:
            raise ValueError(
                f"Local reranker '{model_name}' is not available through fastembed"
            ) from exc

        logger.info("Loading lightweight cross-encoder: %s", fastembed_name)
        self.model = TextCrossEncoder(fastembed_name)
        self._name = fastembed_name

    async def rerank(self, query: str, results: List[SearchResult]) -> List[SearchResult]:
        if not results:
            return results

        documents = [_build_rerank_document(result) for result in results]

        loop = asyncio.get_running_loop()
        scores = await loop.run_in_executor(
            None,
            lambda: list(self.model.rerank(query, documents)),
        )
        for result, score in zip(results, scores):
            result.rerank_score = float(score)
            result.sort_basis = "rerank_score"

        results.sort(key=lambda result: (
            -float(result.rerank_score or 0.0),
            str(result.candidate_id or ""),
        ))
        logger.info("Re-ranked %d results with %s", len(results), self._name)
        return results

    async def warmup(self) -> None:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: list(self.model.rerank("warmup query", ["warmup document"])),
        )


class CohereReranker(Reranker):
    def __init__(self, model: str = "rerank-english-v3.0"):
        import cohere
        from pipeline import settings
        self.client = cohere.AsyncClientV2(api_key=settings.cohere_api_key)
        self.model  = model

    async def rerank(self, query: str, results: List[SearchResult]) -> List[SearchResult]:
        if not results:
            return results
        documents = []
        for r in results:
            text = r.best_chunk
            for sc in r.supporting_chunks:
                text += f"\n\n[{sc.get('doc_type','')}]: {sc.get('content','')}"
            documents.append(text)

        resp = await self.client.rerank(
            model=self.model, query=query, documents=documents, top_n=len(documents)
        )
        for item in resp.results:
            results[item.index].rerank_score = item.relevance_score
        results.sort(key=lambda r: (
            -float(r.rerank_score or 0.0),
            str(r.candidate_id or ""),
        ))
        for r in results:
            r.sort_basis = "rerank_score"
        return results


def get_reranker(choice: str = "local-fast") -> Reranker:
    if choice == "cohere":
        return CohereReranker()
    if choice in RERANKER_CATALOG:
        model_name = RERANKER_CATALOG[choice][0]
        from pipeline import settings
        if settings.local_reranker_backend == "fastembed":
            return FastEmbedCrossEncoderReranker(model_name)
        return LocalCrossEncoderReranker(model_name)
    raise ValueError(f"Unknown reranker '{choice}'. Options: {list(RERANKER_CATALOG)}")
