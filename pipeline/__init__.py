"""
Configuration management for Hybrid Search.
Loads from .env file and environment variables.
"""

from pydantic import field_validator
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    """Application settings loaded from environment."""

    # ─── Database ─────────────────────────
    database_url: str = "postgresql://hybrid_user:hybrid_pass@localhost:5432/hiring_platform"
    # Optional read/search database. Leave empty to use database_url for all
    # reads. In a Supabase + ParadeDB setup this points at the ParadeDB replica.
    search_database_url: str = ""

    # ─── Embedding Provider ───────────────
    # Options: openai, gemini, cohere, voyage, jina, local
    embedding_provider: str = "local"
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dimensions: int = 384
    # sentence-transformers is the full PyTorch path. fastembed uses compact
    # ONNX models and is intended for memory-constrained demo deployments.
    local_embedding_backend: str = "sentence-transformers"
    local_reranker_backend: str = "sentence-transformers"
    # Memory-constrained demos can release the optional cross-encoder after
    # each request. The next rerank pays a small model-load cost.
    evict_local_reranker_after_use: bool = False

    # ─── Pending Embedding Experiment ─────
    # Saved by the model settings page. Not activated until vectors are rebuilt.
    pending_embedding_provider: str | None = None
    pending_embedding_model: str | None = None
    pending_embedding_dimensions: int | None = None

    # ─── Dormant Planner / Rerank Experiments ─
    # The public Smart-search path is disabled, but benchmark scripts can still use these.
    llm_provider: str = "gemini"
    llm_model: str = "gemini-2.5-flash-lite"
    reranker_model: str = "local-fast"

    # ─── Per-mode LLM overrides (UI-configurable) ─
    # Each pair defaults to "" which means: fall back to llm_provider / llm_model above.
    # Fast mode prioritises latency, quality mode prioritises planning depth, insights
    # drives the post-search AI summary call.
    fast_llm_provider:     str = "gemini"
    fast_llm_model:        str = "gemini-2.5-flash-lite"
    quality_llm_provider:  str = "gemini"
    quality_llm_model:     str = "gemini-2.5-flash-lite"
    insights_llm_provider: str = ""
    insights_llm_model:    str = ""
    insights_verify_grounding: bool = False
    llm_planner_timeout_seconds: float = 12.0

    # Gemini-only: per-mode reasoning depth. Values: "low" | "medium" | "high".
    # Ignored for non-Gemini providers. API default (when unset) is "high" — slow + costly,
    # so we default planner modes to low/medium and insights to medium.
    fast_thinking_level:     str = "low"
    quality_thinking_level:  str = "medium"
    insights_thinking_level: str = "medium"

    # ─── API Keys (set whichever you use) ─
    openai_api_key: str = ""
    google_api_key: str = ""
    groq_api_key: str = ""
    deepseek_api_key: str = ""     # also reads DEEPSEEK_API_KEY env
    cohere_api_key: str = ""
    voyage_api_key: str = ""        # also reads VOYAGE_API_KEY env
    jina_api_key: str = ""

    # ── Langfuse observability ────────────────────────────────────────────
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_environment: str = "development"
    langfuse_release: str = ""
    # "dev_full" = capture inputs/outputs verbatim (resumes, emails).
    # "prod_redacted" = strip PII before sending.
    langfuse_trace_content_mode: str = "prod_redacted"
    # Fraction of happy-path traces to send (errors and slow traces always send).
    langfuse_sample_rate: float = 1.0
    langfuse_slow_threshold_ms: float = 1000.0
    otel_enabled: bool = True
    # Export one Langfuse observation per SQL statement (asyncpg auto-instrumentation).
    # Useful in dev for DB-latency debugging, but ~20 noise spans/search at scale —
    # leave off in prod. Defaults to on only in the development environment.
    langfuse_instrument_db: bool = True
    # Langfuse Prompt Management label for the recruiter agent prompt.
    # Use "staging" for prompt experiments without changing code.
    agent_prompt_label: str = "production"

    # ─── Chunking ─────────────────────────
    chunk_size: int = 512           # tokens per chunk
    chunk_overlap: int = 64         # overlap between consecutive chunks
    max_batch_size: int = 100       # max texts per embedding API call

    # ─── Search Tuning ────────────────────
    # ef_search: accuracy vs speed knob (lower = faster, less accurate).
    # 40 keeps small top-k searches quick; raise it for narrow filters or
    # high-recall quality sweeps after measuring recall/latency.
    hnsw_ef_search: int = 40
    default_top_k: int = 20
    # Keyword backend:
    #   fts      = current Postgres tsvector + ts_rank_cd path
    #   paradedb = pg_search/ParadeDB BM25 path on a self-managed DB/replica
    bm25_backend: str = "fts"
    bm25_overfetch_factor: int = 4
    bm25_overfetch_min: int = 60

    # ─── Features ──────────────────────────
    use_personalization: bool = False

    # ─── API Server ───────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # ─── Gemini Live voice ────────────────
    # Wait longer before treating a pause as end-of-turn. 1200 ms avoids
    # submitting short pauses in the middle of a recruiter's sentence.
    gemini_live_vad_silence_ms: int = 1200
    gemini_live_vad_prefix_ms: int = 200

    # ─── Connection Pool ──────────────────
    db_pool_min: int = 5
    db_pool_max: int = 20
    # Optional separate pool sizing for SEARCH_DATABASE_URL. 0 means reuse the
    # primary db_pool_min/db_pool_max settings.
    search_db_pool_min: int = 0
    search_db_pool_max: int = 0

    # ─── Cache ────────────────────────────────
    # memory = process-local LRU only.
    # redis = process-local L1 plus Redis L2 shared across workers/app nodes.
    cache_backend: str = "memory"
    redis_url: str = ""
    cache_namespace: str = "hybrid-search"
    cache_local_maxsize: int = 2048
    cache_redis_socket_timeout_seconds: float = 0.2
    cache_redis_connect_timeout_seconds: float = 0.2
    # Keep retrieval rowset caching opt-in until staging benchmarks confirm
    # stale 1-5 minute rowsets are acceptable for the deployment.
    search_use_retrieval_cache: bool = False

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }

    @field_validator(
        "pending_embedding_provider",
        "pending_embedding_model",
        "pending_embedding_dimensions",
        mode="before",
    )
    @classmethod
    def _empty_pending_embedding_values_are_none(cls, value):
        return None if value == "" else value

    @field_validator("search_database_url", mode="before")
    @classmethod
    def _empty_search_database_url_is_empty_string(cls, value):
        return "" if value is None else str(value).strip()

    @field_validator("bm25_backend", mode="before")
    @classmethod
    def _validate_bm25_backend(cls, value):
        backend = str(value or "fts").strip().lower()
        if backend not in {"fts", "paradedb"}:
            raise ValueError("bm25_backend must be 'fts' or 'paradedb'")
        return backend

    @field_validator("cache_backend", mode="before")
    @classmethod
    def _validate_cache_backend(cls, value):
        backend = str(value or "memory").strip().lower()
        if backend not in {"memory", "redis"}:
            raise ValueError("cache_backend must be 'memory' or 'redis'")
        return backend


# Singleton instance
settings = Settings()
