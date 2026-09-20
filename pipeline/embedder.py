"""
Embedding provider abstraction.

Supports multiple providers — swap via EMBEDDING_PROVIDER env var:
  - "openai"     → text-embedding-3-small / text-embedding-3-large
  - "gemini"     → text-embedding-004
  - "cohere"     → embed-english-v3.0 / embed-multilingual-v3.0
  - "voyage"     → voyage-3 / voyage-3-lite
  - "jina"       → jina-embeddings-v3
  - "local"      → sentence-transformers (zero API cost)

All providers implement the same async interface for easy swapping.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import List

from pipeline import settings

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════
# ABSTRACT BASE
# ══════════════════════════════════════════

class EmbeddingProvider(ABC):
    """Abstract interface for embedding providers."""

    @abstractmethod
    async def embed(self, texts: List[str]) -> List[List[float]]:
        """Embed a batch of texts. Returns list of float vectors."""
        ...

    @property
    @abstractmethod
    def dimensions(self) -> int:
        """Expected dimensionality of output vectors."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        """Human-readable provider + model name."""
        ...

    @property
    def cost_per_million_tokens(self) -> float:
        """Estimated cost in USD per 1M tokens. Override in subclasses."""
        return 0.0


# ══════════════════════════════════════════
# OPENAI
# ══════════════════════════════════════════

class OpenAIEmbedder(EmbeddingProvider):
    """
    OpenAI embedding models.

    Models:
      - text-embedding-3-small:  1536 dims, $0.02/1M tokens (default)
      - text-embedding-3-large:  3072 dims, $0.13/1M tokens
      - text-embedding-ada-002:  1536 dims, $0.10/1M tokens (legacy)

    Supports dimension reduction via the `dimensions` parameter.
    """

    COST_MAP = {
        "text-embedding-3-small": 0.02,
        "text-embedding-3-large": 0.13,
        "text-embedding-ada-002": 0.10,
    }

    def __init__(self, model: str | None = None, dims: int | None = None):
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(api_key=settings.openai_api_key)
        self.model = model or settings.embedding_model
        self._dimensions = dims or settings.embedding_dimensions

    async def embed(self, texts: List[str]) -> List[List[float]]:
        kwargs = {"model": self.model, "input": texts}
        # Only text-embedding-3-* supports the dimensions param
        if "text-embedding-3" in self.model:
            kwargs["dimensions"] = self._dimensions
        response = await self.client.embeddings.create(**kwargs)
        return [item.embedding for item in response.data]

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def name(self) -> str:
        return f"openai/{self.model}"

    @property
    def cost_per_million_tokens(self) -> float:
        return self.COST_MAP.get(self.model, 0.02)


# ══════════════════════════════════════════
# GOOGLE GEMINI
# ══════════════════════════════════════════

class GeminiEmbedder(EmbeddingProvider):
    """
    Google Gemini embedding models.

    Models:
      - text-embedding-004:  768 dims (default), up to 2048 output dims
      - embedding-001:       768 dims (older)

    Free tier: 1500 requests/min. Paid: $0.00 for embedding models.
    """

    def __init__(self, model: str | None = None, dims: int | None = None):
        from google import genai
        self.client = genai.Client(api_key=settings.google_api_key)
        self.model = model or "text-embedding-004"
        self._dimensions = dims or settings.embedding_dimensions

    async def embed(self, texts: List[str]) -> List[List[float]]:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(
            None,
            lambda: self.client.models.embed_content(
                model=self.model,
                contents=texts,
                config={"output_dimensionality": self._dimensions},
            ),
        )
        return [list(emb.values) for emb in result.embeddings]

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def name(self) -> str:
        return f"gemini/{self.model}"

    @property
    def cost_per_million_tokens(self) -> float:
        return 0.0  # Gemini embedding is free


# ══════════════════════════════════════════
# COHERE
# ══════════════════════════════════════════

class CohereEmbedder(EmbeddingProvider):
    """
    Cohere embedding models.

    Models:
      - embed-english-v3.0:        1024 dims, $0.10/1M tokens
      - embed-multilingual-v3.0:   1024 dims, $0.10/1M tokens
      - embed-english-light-v3.0:  384 dims,  $0.10/1M tokens (fastest)

    Supports input_type: "search_document" for indexing, "search_query" for queries.
    """

    def __init__(self, model: str = "embed-english-v3.0"):
        import cohere
        self.client = cohere.AsyncClientV2(api_key=settings.cohere_api_key if hasattr(settings, 'cohere_api_key') else "")
        self.model = model
        self._dimensions = {"embed-english-v3.0": 1024, "embed-multilingual-v3.0": 1024, "embed-english-light-v3.0": 384}.get(model, 1024)
        self._input_type = "search_document"  # switch to "search_query" at query time

    async def embed(self, texts: List[str]) -> List[List[float]]:
        response = await self.client.embed(
            model=self.model,
            texts=texts,
            input_type=self._input_type,
            embedding_types=["float"],
        )
        return [list(emb) for emb in response.embeddings.float_]

    def as_query_embedder(self) -> "CohereEmbedder":
        """Return a copy configured for query-time embedding."""
        clone = CohereEmbedder(model=self.model)
        clone._input_type = "search_query"
        return clone

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def name(self) -> str:
        return f"cohere/{self.model}"

    @property
    def cost_per_million_tokens(self) -> float:
        return 0.10


# ══════════════════════════════════════════
# VOYAGE AI
# ══════════════════════════════════════════

class VoyageEmbedder(EmbeddingProvider):
    """
    Voyage AI embedding models — strong MTEB benchmark performance.

    Models:
      - voyage-3:       1024 dims, $0.06/1M tokens (best quality)
      - voyage-3-lite:  512 dims,  $0.02/1M tokens (fast + cheap)
      - voyage-code-3:  1024 dims, $0.06/1M tokens (code-optimized)
    """

    DIMS = {"voyage-3": 1024, "voyage-3-lite": 512, "voyage-code-3": 1024}
    COST = {"voyage-3": 0.06, "voyage-3-lite": 0.02, "voyage-code-3": 0.06}

    def __init__(self, model: str = "voyage-3-lite"):
        import voyageai
        self.client = voyageai.AsyncClient()
        self.model = model
        self._dimensions = self.DIMS.get(model, 1024)

    async def embed(self, texts: List[str]) -> List[List[float]]:
        result = await self.client.embed(texts, model=self.model)
        return result.embeddings

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def name(self) -> str:
        return f"voyage/{self.model}"

    @property
    def cost_per_million_tokens(self) -> float:
        return self.COST.get(self.model, 0.06)


# ══════════════════════════════════════════
# JINA AI
# ══════════════════════════════════════════

class JinaEmbedder(EmbeddingProvider):
    """
    Jina AI embedding models.

    Models:
      - jina-embeddings-v3:  1024 dims, $0.02/1M tokens
      - jina-embeddings-v2-base-en: 768 dims, free tier available

    Uses the OpenAI-compatible API endpoint.
    """

    def __init__(self, model: str = "jina-embeddings-v3", dims: int = 1024):
        from openai import AsyncOpenAI
        self.client = AsyncOpenAI(
            api_key=settings.jina_api_key if hasattr(settings, 'jina_api_key') else "",
            base_url="https://api.jina.ai/v1",
        )
        self.model = model
        self._dimensions = dims

    async def embed(self, texts: List[str]) -> List[List[float]]:
        response = await self.client.embeddings.create(
            model=self.model,
            input=texts,
        )
        return [item.embedding for item in response.data]

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def name(self) -> str:
        return f"jina/{self.model}"

    @property
    def cost_per_million_tokens(self) -> float:
        return 0.02


# ══════════════════════════════════════════
# LOCAL (sentence-transformers)
# ══════════════════════════════════════════

class LocalEmbedder(EmbeddingProvider):
    """
    Local sentence-transformers models — $0 cost, runs on device.

    Recommended models:
      - all-MiniLM-L6-v2:       384 dims, ~22M params, fastest
      - all-mpnet-base-v2:      768 dims, ~109M params, best quality
      - bge-small-en-v1.5:      384 dims, ~33M params, strong MTEB
      - bge-base-en-v1.5:       768 dims, ~109M params, top-tier
      - nomic-embed-text-v1.5:  768 dims, ~137M params, long context
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ImportError(
                "sentence-transformers required for local embeddings. "
                "Install: pip install sentence-transformers"
            )
        logger.info("Loading local model: %s", model_name)
        try:
            self.model = SentenceTransformer(model_name)
        except Exception as exc:
            logger.warning(
                "Could not load local model '%s' with online metadata checks (%s). "
                "Retrying from the local cache only.",
                model_name,
                exc,
            )
            self.model = SentenceTransformer(model_name, local_files_only=True)
        self._model_name = model_name
        self._dimensions = self.model.get_sentence_embedding_dimension()
        logger.info("Loaded (%d dimensions)", self._dimensions)

    async def embed(self, texts: List[str]) -> List[List[float]]:
        loop = asyncio.get_running_loop()
        embeddings = await loop.run_in_executor(
            None,
            lambda: self.model.encode(
                texts,
                normalize_embeddings=True,
                show_progress_bar=False,
                batch_size=64,
            ),
        )
        return [emb.tolist() for emb in embeddings]

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def name(self) -> str:
        return f"local/{self._model_name}"

    @property
    def cost_per_million_tokens(self) -> float:
        return 0.0


class FastEmbedEmbedder(EmbeddingProvider):
    """ONNX-backed local embeddings for memory-constrained deployments."""

    MODEL_NAMES = {
        "all-MiniLM-L6-v2": ("sentence-transformers/all-MiniLM-L6-v2", 384),
        "sentence-transformers/all-MiniLM-L6-v2": (
            "sentence-transformers/all-MiniLM-L6-v2",
            384,
        ),
        "bge-small-en-v1.5": ("BAAI/bge-small-en-v1.5", 384),
        "BAAI/bge-small-en-v1.5": ("BAAI/bge-small-en-v1.5", 384),
    }

    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise ImportError("fastembed required for the lightweight local backend") from exc

        try:
            fastembed_name, dimensions = self.MODEL_NAMES[model_name]
        except KeyError as exc:
            raise ValueError(
                f"Local model '{model_name}' is not available through fastembed"
            ) from exc

        logger.info("Loading lightweight local model: %s", fastembed_name)
        self.model = TextEmbedding(fastembed_name)
        self._model_name = model_name
        self._dimensions = dimensions

    async def embed(self, texts: List[str]) -> List[List[float]]:
        loop = asyncio.get_running_loop()
        embeddings = await loop.run_in_executor(
            None,
            lambda: list(self.model.embed(texts, batch_size=64)),
        )
        return [embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
                for embedding in embeddings]

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def name(self) -> str:
        return f"local-fastembed/{self._model_name}"


# ══════════════════════════════════════════
# FACTORY
# ══════════════════════════════════════════

# Registry of all available providers and their models
PROVIDER_CATALOG = {
    "openai": {
        "class": OpenAIEmbedder,
        "models": {
            "text-embedding-3-small": {"dims": 1536, "cost": 0.02},
            "text-embedding-3-large": {"dims": 3072, "cost": 0.13},
        },
    },
    "gemini": {
        "class": GeminiEmbedder,
        "models": {
            "text-embedding-004": {"dims": 768, "cost": 0.00},
        },
    },
    "cohere": {
        "class": CohereEmbedder,
        "models": {
            "embed-english-v3.0": {"dims": 1024, "cost": 0.10},
            "embed-multilingual-v3.0": {"dims": 1024, "cost": 0.10},
            "embed-english-light-v3.0": {"dims": 384, "cost": 0.10},
        },
    },
    "voyage": {
        "class": VoyageEmbedder,
        "models": {
            "voyage-3": {"dims": 1024, "cost": 0.06},
            "voyage-3-lite": {"dims": 512, "cost": 0.02},
        },
    },
    "jina": {
        "class": JinaEmbedder,
        "models": {
            "jina-embeddings-v3": {"dims": 1024, "cost": 0.02},
        },
    },
    "local": {
        "class": LocalEmbedder,
        "models": {
            "all-MiniLM-L6-v2": {"dims": 384, "cost": 0.00},
            "all-mpnet-base-v2": {"dims": 768, "cost": 0.00},
            "bge-small-en-v1.5": {"dims": 384, "cost": 0.00},
            "bge-base-en-v1.5": {"dims": 768, "cost": 0.00},
            "nomic-embed-text-v1.5": {"dims": 768, "cost": 0.00},
        },
    },
}


def get_embedder(
    provider: str | None = None,
    model: str | None = None,
    dims: int | None = None,
) -> EmbeddingProvider:
    """
    Factory: returns an embedding provider instance.

    Args:
        provider: Provider name (openai, gemini, cohere, voyage, jina, local).
                  Defaults to EMBEDDING_PROVIDER env var.
        model: Model name. Defaults to EMBEDDING_MODEL env var.
        dims: Override output dimensions (if supported by provider).
    """
    provider = provider or settings.embedding_provider
    model = model or settings.embedding_model

    if provider == "openai":
        return OpenAIEmbedder(model=model, dims=dims)
    elif provider == "gemini":
        return GeminiEmbedder(model=model, dims=dims)
    elif provider == "cohere":
        return CohereEmbedder(model=model)
    elif provider == "voyage":
        return VoyageEmbedder(model=model)
    elif provider == "jina":
        return JinaEmbedder(model=model, dims=dims or 1024)
    elif provider == "local":
        if settings.local_embedding_backend == "fastembed":
            return FastEmbedEmbedder(model_name=model)
        return LocalEmbedder(model_name=model)
    else:
        raise ValueError(
            f"Unknown embedding provider: '{provider}'. "
            f"Available: {list(PROVIDER_CATALOG.keys())}"
        )


def list_available_models() -> dict:
    """List all available providers and models with their specs."""
    return {
        provider: {
            model: info
            for model, info in data["models"].items()
        }
        for provider, data in PROVIDER_CATALOG.items()
    }
