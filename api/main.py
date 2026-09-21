"""
FastAPI application for Hybrid Search.

Endpoints:
  POST /search              — Standard two-stage search (fast, <100ms)
  POST /ingest              — Ingest a document for a candidate
  POST /candidates          — Create a new candidate
  GET  /candidates/{id}     — Get candidate details
  GET  /models              — List available embedding models
  GET  /health              — Health check
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import re as _re
import uuid as _uuid

# Load .env into os.environ so third-party SDKs (PydanticAI, Groq, Google) find API keys
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(override=False)
except ImportError:
    pass
# PydanticAI's google-gla provider reads GEMINI_API_KEY; our .env uses
# GOOGLE_API_KEY (same key value). Alias it so the Gemini agent model — and the
# Gemini fallback in agent_run — can authenticate.
import os as _os
if not _os.environ.get("GEMINI_API_KEY") and _os.environ.get("GOOGLE_API_KEY"):
    _os.environ["GEMINI_API_KEY"] = _os.environ["GOOGLE_API_KEY"]
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional, List, Literal, Mapping

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from pipeline import cache as _cache
from pipeline import settings
from pipeline.aliases import clean_skill_list
from pipeline.database import get_pool, get_search_pool, close_pool
from pipeline.search import HybridSearchEngine, SearchFilters
from pipeline.search_result import SearchResult
from pipeline.observability import (
    is_enabled as _obs_enabled,
    _get_client as _obs_client,
    start_span as _obs_start_span,
    trace_attributes as _obs_trace_attributes,
    update_current_span as _obs_update_current_span,
    get_current_trace_id as _obs_trace_id,
    get_trace_url as _obs_trace_url,
)

class _NullContext:
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def update(self, **kw): pass
from pipeline.ingest import IngestionPipeline
from pipeline.embedder import get_embedder, list_available_models
from pipeline.ai_insights import (
    AIInsightResult as PipelineAIInsightResult,
    AIInsightService,
    CandidateInsightInput,
    candidate_insight_input_from_result,
)
from pipeline.ranking_explanation import (
    build_filter_only_ranking_explanation,
    build_ranking_explanation,
)
from pipeline.reranker import RERANKER_CATALOG, get_reranker
from pipeline.metrics import metrics_snapshot
from pipeline.llm_models import list_llm_models
from pipeline.llm_models import gemini_model_supports_thinking
from pipeline.observability import init_langfuse, shutdown_langfuse, init_otel
from pipeline.live_rag import LiveRAGSession, TOOL_REGISTRY
from pipeline.gemini_live import GeminiLiveBridge, GoogleGenAILiveTransport
from pipeline.realtime_prompt import REALTIME_SYSTEM_PROMPT
from pipeline.realtime_session import RealtimeAgentSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)

_agent_model: str = "deepseek:deepseek-v4-flash"


_SEARCH_HISTORY_BATCH_SIZE = 25
_SEARCH_HISTORY_QUEUE_MAX = int(_os.environ.get("SEARCH_HISTORY_QUEUE_MAX", "1000"))


def _search_history_item(
    recruiter_id: str | None,
    query: str,
    filters: dict,
    results: list,
    latency_ms: int,
) -> dict[str, Any] | None:
    if not recruiter_id:
        return None
    top10 = [
        {
            "id": str(r.candidate_id),
            "score": round(float(r.rank_score or r.rrf_score or r.similarity_score or 0.0), 4),
        }
        for r in (results or [])[:10]
    ]
    return {
        "recruiter_id": recruiter_id,
        "query": query or "",
        "filters_json": json.dumps(filters or {}),
        "results_json": json.dumps(top10),
        "latency_ms": int(latency_ms or 0),
    }


def _looks_like_uuid(value: Any) -> bool:
    try:
        _uuid.UUID(str(value))
        return True
    except (TypeError, ValueError, AttributeError):
        return False


async def _write_search_history_batch(pool, items: list[dict[str, Any]]) -> None:
    if not items:
        return
    try:
        await pool.executemany(
            """INSERT INTO search_history (recruiter_id, query, filters_json, results_json, latency_ms)
               VALUES ($1, $2, $3::jsonb, $4::jsonb, $5)""",
            [
                (
                    item["recruiter_id"],
                    item["query"],
                    item["filters_json"],
                    item["results_json"],
                    item["latency_ms"],
                )
                for item in items
            ],
        )
    except Exception as exc:
        logger.debug("search_history logging skipped: %s", exc)


async def _log_search_history(
    recruiter_id: str | None,
    query: str,
    filters: dict,
    results: list,
    latency_ms: int,
) -> None:
    """Compatibility path for places that still want a direct async write."""
    item = _search_history_item(recruiter_id, query, filters, results, latency_ms)
    if item is None:
        return
    await _write_search_history_batch(await get_pool(), [item])


def _enqueue_search_history(
    app_obj: FastAPI,
    *,
    recruiter_id: str | None,
    query: str,
    filters: dict,
    results: list,
    latency_ms: int,
) -> None:
    item = _search_history_item(recruiter_id, query, filters, results, latency_ms)
    if item is None:
        return
    queue = getattr(app_obj.state, "search_history_queue", None)
    if queue is None:
        return
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        logger.warning("search_history queue full; dropping non-critical history write")


async def _search_history_worker(app_obj: FastAPI) -> None:
    queue: asyncio.Queue = app_obj.state.search_history_queue
    pool = app_obj.state.pool
    batch: list[dict[str, Any]] = []
    try:
        while True:
            item = await queue.get()
            if item is None:
                queue.task_done()
                break

            batch.append(item)
            queue.task_done()

            while len(batch) < _SEARCH_HISTORY_BATCH_SIZE:
                try:
                    item = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if item is None:
                    queue.task_done()
                    await _write_search_history_batch(pool, batch)
                    batch.clear()
                    return
                batch.append(item)
                queue.task_done()

            await _write_search_history_batch(pool, batch)
            batch.clear()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("search_history worker stopped: %s", exc)
    finally:
        if batch:
            await _write_search_history_batch(pool, batch)


async def _stop_search_history_worker(app_obj: FastAPI) -> None:
    queue = getattr(app_obj.state, "search_history_queue", None)
    worker = getattr(app_obj.state, "search_history_worker", None)
    if queue is None or worker is None:
        return
    try:
        queue.put_nowait(None)
    except asyncio.QueueFull:
        worker.cancel()
    try:
        await asyncio.wait_for(worker, timeout=2.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        worker.cancel()


ADMIN_UI_FILE = Path(__file__).resolve().parent / "static" / "admin.html"
SETTINGS_UI_FILE = Path(__file__).resolve().parent / "static" / "settings.html"
TALENT_UI_FILE = Path(__file__).resolve().parent / "static" / "talent.html"
TALENT_SETTINGS_UI_FILE = Path(__file__).resolve().parent / "static" / "talent-settings.html"
TALENT_TOOL_RUNNER_UI_FILE = Path(__file__).resolve().parent / "static" / "talent-tool-runner.html"
JOURNEY_UI_FILE = Path(__file__).resolve().parent / "static" / "project-story" / "index.html"
LIVE_RAG_UI_FILE = Path(__file__).resolve().parent / "static" / "live-rag" / "index.html"
MODEL_ENV_FILE = Path(".env")
API_KEY_ENV_FIELDS = {
    "openai_api_key": "OPENAI_API_KEY",
    "google_api_key": "GOOGLE_API_KEY",
    "groq_api_key":   "GROQ_API_KEY",
    "cohere_api_key": "COHERE_API_KEY",
    "voyage_api_key": "VOYAGE_API_KEY",
    "jina_api_key":   "JINA_API_KEY",
}
# ══════════════════════════════════════════
# LIFESPAN
# ══════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await get_pool()
    search_pool = await get_search_pool() if settings.search_database_url else pool
    embedder = get_embedder()
    app.state.pool = pool
    app.state.search_pool = search_pool
    init_langfuse()
    init_otel(app)
    await _cache.start()
    app.state.reranker_cache = {}
    app.state.model_warmups = {}
    app.state.search_history_queue = asyncio.Queue(maxsize=_SEARCH_HISTORY_QUEUE_MAX)
    app.state.search_history_worker = asyncio.create_task(_search_history_worker(app))
    app.state.search_engine = HybridSearchEngine(
        pool,
        embedder=embedder,
        reranker_cache=app.state.reranker_cache,
        search_pool=search_pool,
    )
    app.state.ingestion = IngestionPipeline(pool, embedder=embedder)
    try:
        await _warmup_db_pool(app, pool, label="primary")
        if search_pool is not pool:
            await _warmup_db_pool(app, search_pool, label="search")
    except Exception:
        logger.exception("DB pool warmup failed; first parallel search may pay connection setup cost")
    try:
        await _warmup_active_embedder(app)
    except Exception:
        logger.exception("Embedder warmup at startup failed; first request will pay cold-load cost")
    if settings.evict_local_reranker_after_use:
        app.state.model_warmups["reranker"] = {
            "status": "skipped",
            "reason": "Configured for per-request eviction on a memory-constrained deployment.",
        }
    else:
        try:
            await _warmup_reranker(app, settings.reranker_model or "local-fast")
        except Exception:
            logger.exception("Reranker warmup at startup failed; first request will pay cold-load cost")
    logger.info("Hybrid Search API started")
    try:
        yield
    finally:
        await _stop_search_history_worker(app)
        shutdown_langfuse()
        await _cache.close()
        await close_pool()
        logger.info("Hybrid Search API stopped")


app = FastAPI(
    title="Hybrid Search API",
    description="Two-stage search: structured SQL filtering → semantic vector similarity",
    version="0.2.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_static_dir = Path(__file__).resolve().parent / "static"
_PUBLIC_DEMO_PATHS = {"/journey", "/live-rag", "/live-rag/api/tools", "/health"}
_PUBLIC_DEMO_PREFIXES = ("/static/project-story/", "/static/live-rag/")


@app.middleware("http")
async def optional_demo_auth(request: Request, call_next):
    """Protect the hosted product demo while leaving the public journal readable."""
    username = _os.environ.get("APP_DEMO_USERNAME", "").strip()
    password = _os.environ.get("APP_DEMO_PASSWORD", "")
    if not username or not password:
        return await call_next(request)

    path = request.url.path
    if path in _PUBLIC_DEMO_PATHS or path.startswith(_PUBLIC_DEMO_PREFIXES):
        return await call_next(request)

    supplied_username = ""
    supplied_password = ""
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Basic "):
        try:
            decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
            supplied_username, supplied_password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            pass

    authenticated = hmac.compare_digest(supplied_username, username) and hmac.compare_digest(
        supplied_password,
        password,
    )
    if not authenticated:
        return Response(
            status_code=401,
            content="Talent Atlas demo access required",
            headers={"WWW-Authenticate": 'Basic realm="Talent Atlas demo", charset="UTF-8"'},
        )
    return await call_next(request)


class _NoCacheStaticFiles(StaticFiles):
    """Serve static assets with no-store so browsers never run stale JS/CSS.

    The recruiter UI (talent.js/css, agent-panel.js) is edited frequently; default
    StaticFiles caching made browsers keep running old code after deploys/edits.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response


app.mount("/static", _NoCacheStaticFiles(directory=_static_dir), name="static")


# ══════════════════════════════════════════
# REQUEST / RESPONSE MODELS
# ══════════════════════════════════════════

class SearchRequest(BaseModel):
    """Search request — accepts a free-text query, a job description, or both."""
    query: str = Field(default="", description="Free-text semantic search query")
    jd: Optional[str] = Field(default=None, description="Job description paste — activates HyDE mode")
    mode: Literal["no-llm", "fast", "quality", "agent-quality"] = Field(
        default="quality",
        description="Search mode: no-llm, fast, quality, or agent-quality (structured agent plan + stronger ranking). Use the include_rank_explanation toggle for explanations."
    )
    # ─── Structured filters (override LLM-extracted values) ──────────────────
    location: Optional[str] = Field(
        default=None,
        description="Broad location text matched against location, city, or country",
    )
    country: Optional[str] = None
    city: Optional[str] = None
    min_age: Optional[int] = None
    max_age: Optional[int] = None
    skills: Optional[List[str]] = None
    should: dict[str, Any] = Field(
        default_factory=dict,
        description="Soft preferences: skills, themes, roles, locations. These influence retrieval/ranking but are not hard filters.",
    )
    skill_weights: Optional[Dict[str, float]] = Field(
        default=None,
        description="Per-skill importance weights (0-100 or 0-1). Multiplies that skill's "
                    "contribution to the exact-skill retrieval score.",
    )
    interests: Optional[List[str]] = None
    min_years_exp: Optional[int] = None
    max_years_exp: Optional[int] = None
    min_salary: Optional[int] = None
    max_salary: Optional[int] = None
    # ─── AND / OR mode ────────────────────
    skills_match: Literal["and", "or"] = Field(
        default="or",
        description="'and' = must have ALL skills (strict), 'or' = at least ONE (loose)"
    )
    interests_match: Literal["and", "or"] = Field(default="or")
    # ─── Search options ───────────────────
    session_id: Optional[str] = Field(default=None, description="Client-generated session ID for observability tracing")
    recruiter_id: Optional[str] = Field(
        default=None,
        description="If provided, loads recruiter_preferences row and applies weight overrides",
    )
    status: Optional[List[str]] = Field(
        default=None,
        description="Filter by candidate status: active, archived, hired, rejected",
    )
    config_overrides: dict[str, Any] = Field(
        default_factory=dict,
        description="Per-request pipeline toggle / knob overrides (see docs/toggle.md)",
    )
    keyword_policy: Literal["auto", "skip", "force"] = Field(
        default="auto",
        description="Keyword/FTS branch policy. auto lets the backend skip broad expensive keyword searches; force still has a timeout.",
    )
    keyword_timeout_ms: Optional[int] = Field(
        default=None,
        ge=100,
        le=5000,
        description="Optional per-request timeout for the keyword/FTS branch.",
    )
    top_k: int = Field(default=7, ge=1, le=1000)
    use_rrf: bool = Field(
        default=True,
        description="Combine keyword + semantic search via Reciprocal Rank Fusion"
    )
    enable_reranking: bool = Field(
        default=False,
        description="Optionally re-rank standard search results with the configured cross-encoder"
    )
    reranker: Optional[str] = Field(
        default=None,
        description="Optional reranker override: local-fast, local-balanced, local-best, cohere"
    )
    include_rank_explanation: bool = Field(
        default=True,
        description="Attach ranking confidence and explanation from the current search pipeline"
    )
    include_fit_analysis: Optional[bool] = Field(
        default=None,
        description="Deprecated alias for include_rank_explanation"
    )
    include_ai_insights: bool = Field(
        default=False,
        description="UI toggle for requesting a separate AI insight pass over the top results"
    )
    max_chunks_per_candidate: int = Field(
        default=3,
        description="Max supporting chunks per candidate (for cross-document context)"
    )
    llm_provider: Optional[str] = Field(
        default=None,
        description="LLM provider override for the planner: openai, gemini, groq"
    )
    llm_model: Optional[str] = Field(
        default=None,
        description="LLM model override for the planner"
    )

class ModelSettingsUpdate(BaseModel):
    """Admin model settings update. Embedding changes are staged as pending."""
    pending_embedding_provider: Optional[str] = None
    pending_embedding_model: Optional[str] = None
    pending_embedding_dimensions: Optional[int] = Field(default=None, ge=1)
    reranker_model: Optional[str] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    fast_llm_provider: Optional[str] = None
    fast_llm_model: Optional[str] = None
    quality_llm_provider: Optional[str] = None
    quality_llm_model: Optional[str] = None
    insights_llm_provider: Optional[str] = None
    insights_llm_model: Optional[str] = None
    fast_thinking_level: Optional[str] = None
    quality_thinking_level: Optional[str] = None
    insights_thinking_level: Optional[str] = None
    api_keys: dict[str, str] = Field(default_factory=dict)
    use_personalization: Optional[bool] = None


class LLMValidationRequest(BaseModel):
    """Validate a specific LLM provider/model pair from the settings UI."""
    provider: Optional[str] = None
    model: Optional[str] = None
    thinking_level: Optional[str] = None


class CandidateResult(BaseModel):
    """A single candidate in search results."""
    candidate_id: str
    impression_id: Optional[str] = None
    full_name: str
    email: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    years_exp: Optional[int] = None
    skills: List[str] = []
    best_chunk: str
    similarity_score: float
    doc_type: str
    document_title: Optional[str] = None
    supporting_chunks: List[dict] = []
    rerank_score: Optional[float] = None
    rrf_score: Optional[float] = None
    rank_score: Optional[float] = None
    ranking_explanation: Optional[dict[str, Any]] = None


class SearchResponse(BaseModel):
    """Search response with results and performance metadata."""
    query: str
    mode: Literal["no-llm", "fast", "quality", "agent-quality"] = "quality"
    filters_applied: dict
    results: List[CandidateResult]
    total_results: int
    candidate_ids: List[str] = Field(
        default_factory=list,
        description="Ordered candidate IDs known from retrieval; may be longer than enriched results.",
    )
    deferred_candidate_ids: List[str] = Field(
        default_factory=list,
        description="Candidate IDs not fully enriched in the initial response.",
    )
    latency_ms: float
    timings_ms: dict[str, float] = Field(default_factory=dict)
    phase_timings: dict[str, float] = Field(
        default_factory=dict,
        description="Per-phase latency: query_understanding_ms, retrieval_ms, ranking_ms",
    )
    retrieval_policy: dict[str, Any] = Field(
        default_factory=dict,
        description="Backend retrieval decisions, including keyword/FTS run/skip reason and timeout.",
    )
    planner_spec: Optional[dict] = Field(
        default=None,
        description="Serialised CanonicalSearchSpec produced by the LLM planner",
    )
    clarify: Optional[str] = Field(
        default=None,
        description="Clarifying question when query is ambiguous — no results returned",
    )
    relaxations_applied: List[dict] = Field(
        default_factory=list,
        description="Constraints that were relaxed to find results",
    )
    dropped_items: List[dict] = Field(
        default_factory=list,
        description="Things the validator pulled out of the LLM output "
                    "(hallucinated skills, forbidden fields). Useful for the "
                    "UI to tell the user what was filtered.",
    )
    personalization_applied: bool = Field(
        default=False,
        description="True when recruiter outcome history influenced ranking",
    )
    personalization_offer: Optional[dict] = Field(
        default=None,
        description="LLM's 'want me to remember this for future searches?' "
                    "offer. Shape: {hint, question}. Null when nothing to offer.",
    )
    langfuse_trace_id: Optional[str] = None
    langfuse_trace_url: Optional[str] = None
    ai_insights: Optional[AIInsightResponse] = Field(
        default=None,
        description="Inline AI evidence matrix, present when include_ai_insights=True",
    )

class AIInsightMatrixRow(BaseModel):
    """One candidate row in the AI evidence matrix."""
    candidate_id: str
    full_name: str
    fit_score: int = Field(ge=0, le=100)
    recommendation: str = ""
    strengths: List[str] = Field(default_factory=list)
    evidence: List[str] = Field(default_factory=list)
    gaps: List[str] = Field(default_factory=list)
    interview_probe: str = ""
    grounding_notes: str = ""


class AIInsightResponse(BaseModel):
    """Separate AI insight generated from the current top search results."""
    status: Literal["ok", "unavailable", "error"]
    provider: str
    model: str
    best_candidate_id: Optional[str] = None
    summary: str = ""
    comparative_reasoning: str = ""
    matrix: List[AIInsightMatrixRow] = Field(default_factory=list)
    timings_ms: dict[str, float] = Field(default_factory=dict)
    error: Optional[str] = None


class IngestRequest(BaseModel):
    candidate_id: str
    doc_type: str = Field(..., description="transcript, certification, resume, bio, cover_letter, other")
    title: str
    raw_text: str
    chunk_strategy: str = Field(default="sliding_window", description="sliding_window or paragraph")


class CandidateCreate(BaseModel):
    full_name: str
    email: Optional[str] = None
    age: Optional[int] = Field(default=None, ge=16, le=100)
    location: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    interests: List[str] = Field(default_factory=list)
    skills: List[str] = Field(default_factory=list)
    years_exp: int = Field(default=0, ge=0)
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None


class CandidateUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    age: Optional[int] = Field(default=None, ge=16, le=100)
    location: Optional[str] = None
    city: Optional[str] = None
    country: Optional[str] = None
    interests: Optional[List[str]] = None
    skills: Optional[List[str]] = None
    years_exp: Optional[int] = Field(default=None, ge=0)
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    status: Optional[Literal["active", "archived", "hired", "rejected"]] = None


# ══════════════════════════════════════════
# ENDPOINTS
# ══════════════════════════════════════════

@app.get("/", include_in_schema=False)
async def root_ui_redirect():
    """Open the Talent UI from the browser root."""
    return RedirectResponse(url="/talent")


@app.get("/favicon.ico", include_in_schema=False)
async def empty_favicon():
    """Avoid a noisy browser-console 404 for the local demo."""
    return Response(status_code=204)


@app.get("/ui", include_in_schema=False)
async def admin_ui():
    """Serve the lightweight local admin UI."""
    if not ADMIN_UI_FILE.exists():
        raise HTTPException(status_code=404, detail="Admin UI file not found")
    return FileResponse(ADMIN_UI_FILE, media_type="text/html")


@app.get("/settings", include_in_schema=False)
async def settings_ui():
    """Serve the model settings UI."""
    if not SETTINGS_UI_FILE.exists():
        raise HTTPException(status_code=404, detail="Settings UI file not found")
    return FileResponse(SETTINGS_UI_FILE, media_type="text/html")


@app.get("/talent", include_in_schema=False)
async def talent_ui():
    """Serve the recruiter-facing agent-first talent search UI."""
    if not TALENT_UI_FILE.exists():
        raise HTTPException(status_code=404, detail="Talent UI file not found")
    return FileResponse(TALENT_UI_FILE, media_type="text/html")


@app.get("/journey", include_in_schema=False)
async def project_journey_ui():
    """Serve the public-safe Talent Atlas project evolution journal."""
    if not JOURNEY_UI_FILE.exists():
        raise HTTPException(status_code=404, detail="Project journey file not found")
    return FileResponse(JOURNEY_UI_FILE, media_type="text/html")


@app.get("/live-rag", include_in_schema=False)
async def live_rag_ui():
    """Serve the streaming Live RAG evaluation cockpit."""
    if not LIVE_RAG_UI_FILE.exists():
        raise HTTPException(status_code=404, detail="Live RAG UI file not found")
    return FileResponse(LIVE_RAG_UI_FILE, media_type="text/html")


@app.get("/live-rag/api/tools")
async def live_rag_tools():
    """Return the exact tools, triggers and guardrails shown by the demo UI."""
    return {"tools": TOOL_REGISTRY, "count": len(TOOL_REGISTRY), "state_scope": "session-only"}


@app.websocket("/live-rag/ws")
async def live_rag_socket(websocket: WebSocket):
    """Full-duplex transcript-to-evidence stream with superseded-work cancellation."""
    await websocket.accept()
    session = LiveRAGSession(app.state.search_engine)
    active_task: asyncio.Task | None = None
    send_lock = asyncio.Lock()

    async def emit(event_type: str, payload: dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_json({
                "type": event_type,
                "session_id": session.session_id,
                "timestamp_ms": round((time.perf_counter() - session.started_at) * 1000, 1),
                **payload,
            })

    await emit("session.started", {
        "message": "Ephemeral session created. No state will survive this socket.",
        "tool_count": len(TOOL_REGISTRY),
    })
    try:
        while True:
            message = await websocket.receive_json()
            message_type = message.get("type")
            if message_type == "reset":
                if active_task and not active_task.done():
                    active_task.cancel()
                session = LiveRAGSession(app.state.search_engine)
                await emit("session.started", {
                    "message": "Session reset. Previous transcript, evidence and citations were discarded.",
                    "tool_count": len(TOOL_REGISTRY),
                })
                continue
            if message_type != "transcript.chunk":
                await emit("error", {"message": "Unsupported event type", "received": message_type})
                continue

            text = str(message.get("text") or "").strip()
            if not text:
                continue
            await emit("transcript.delta", {
                "text": text,
                "is_final": bool(message.get("is_final")),
                "source_elapsed_ms": message.get("elapsed_ms"),
            })
            if active_task and not active_task.done():
                active_task.cancel()
            active_task = asyncio.create_task(session.process(
                text,
                is_final=bool(message.get("is_final")),
                emit=emit,
            ))
    except WebSocketDisconnect:
        if active_task and not active_task.done():
            active_task.cancel()
    except Exception as exc:
        logger.exception("Live RAG socket failed")
        try:
            await emit("error", {"message": str(exc)})
        except Exception:
            pass


@app.websocket("/talent/realtime/ws")
async def talent_realtime_socket(websocket: WebSocket):
    """Bidirectional voice transport backed by the revision-aware search harness."""
    await websocket.accept()
    runtime = RealtimeAgentSession(app.state.search_engine)
    send_lock = asyncio.Lock()
    bridge: GeminiLiveBridge | None = None
    bridge_task: asyncio.Task | None = None

    async def send_events() -> None:
        while True:
            event = await runtime.events.get()
            async with send_lock:
                await websocket.send_json(event.model_dump(mode="json"))

    async def on_bridge_event(event_type: str, payload: dict[str, Any]) -> None:
        revision_id = runtime.current_plan.revision_id if runtime.current_plan else None
        runtime.graph.emit(event_type, payload=payload, revision_id=revision_id)

    async def on_bridge_audio(audio: bytes) -> None:
        async with send_lock:
            await websocket.send_bytes(audio)

    async def start_bridge() -> GeminiLiveBridge:
        nonlocal bridge, bridge_task
        if bridge is not None:
            return bridge
        factory = getattr(app.state, "realtime_transport_factory", None)
        model = _os.environ.get("GEMINI_LIVE_MODEL", "gemini-3.8-live")
        connect_args = {
            "api_key": settings.google_api_key,
            "model": model,
            "system_instruction": REALTIME_SYSTEM_PROMPT,
            "tools": runtime.tools.tool_declarations(),
        }
        if factory is None:
            if not settings.google_api_key:
                raise RuntimeError("GOOGLE_API_KEY is required for realtime voice")
            transport = await GoogleGenAILiveTransport.connect(**connect_args)
        else:
            created = factory(**connect_args)
            transport = await created if hasattr(created, "__await__") else created
        bridge = GeminiLiveBridge(
            transport,
            runtime.tools,
            on_event=on_bridge_event,
            on_audio=on_bridge_audio,
        )
        bridge_task = asyncio.create_task(bridge.run())
        runtime.graph.emit("session.ready", payload={
            "model": model,
            "message": "Realtime voice connected. Interruptions and search revisions are active.",
            "tool_count": len(runtime.tools.tool_declarations()),
        })
        return bridge

    event_task = asyncio.create_task(send_events())
    runtime.graph.emit("session.connected", payload={
        "session_scope": "ephemeral",
        "audio_input_hz": 16000,
        "audio_output_hz": 24000,
    })
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            audio = message.get("bytes")
            if audio is not None:
                active_bridge = await start_bridge()
                await active_bridge.send_audio(audio)
                continue
            raw_text = message.get("text")
            if raw_text is None:
                continue
            try:
                command = json.loads(raw_text)
            except json.JSONDecodeError:
                command = {"type": "text.input", "text": raw_text}
            command_type = command.get("type")
            if command_type == "session.start":
                await start_bridge()
            elif command_type == "session.stop":
                break
            elif command_type == "session.reset":
                await runtime.close()
                runtime.graph.emit("session.reset", payload={"message": "Active retrieval was cancelled."})
            elif command_type == "trace.snapshot":
                runtime.graph.emit("trace.snapshot", payload=runtime.snapshot())
            elif command_type == "text.input":
                text = str(command.get("text") or "").strip()
                if text:
                    active_bridge = await start_bridge()
                    await active_bridge.send_text(text)
            else:
                runtime.graph.emit("error", payload={
                    "message": "Unsupported realtime event type",
                    "received": command_type,
                })
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.exception("Talent realtime socket failed")
        runtime.graph.emit("error", payload={"message": str(exc), "error_type": type(exc).__name__})
        await asyncio.sleep(0)
    finally:
        await runtime.close()
        if bridge is not None:
            await bridge.close()
        if bridge_task is not None:
            bridge_task.cancel()
            await asyncio.gather(bridge_task, return_exceptions=True)
        event_task.cancel()
        await asyncio.gather(event_task, return_exceptions=True)


@app.get("/talent/settings", include_in_schema=False)
async def talent_settings_ui():
    """Serve the recruiter-facing talent UI settings page."""
    if not TALENT_SETTINGS_UI_FILE.exists():
        raise HTTPException(status_code=404, detail="Talent settings UI file not found")
    return FileResponse(TALENT_SETTINGS_UI_FILE, media_type="text/html")


@app.get("/talent/tool-runner", include_in_schema=False)
async def talent_tool_runner_ui():
    """Serve a focused page for replaying a visible agent tool call."""
    if not TALENT_TOOL_RUNNER_UI_FILE.exists():
        raise HTTPException(status_code=404, detail="Talent tool runner UI file not found")
    return FileResponse(TALENT_TOOL_RUNNER_UI_FILE, media_type="text/html")

@app.post("/search", response_model=SearchResponse)
async def search(req: SearchRequest, background_tasks: BackgroundTasks = None):
    """
    Smart hybrid search.

    Accepts a free-text query, a job description paste (jd), or both.
    Runs the full LLM-planner → SQL → dense/BM25/skill → RRF → rerank pipeline.
    Every stage is toggleable via the `mode` field (no-llm / fast / quality).
    """
    background_tasks = background_tasks or BackgroundTasks()
    # Pre-flight sampling: skip tracing entirely for happy-path requests when
    # sample_rate < 1.0. Errors and slow requests are always sampled post-hoc
    # via the metadata/scores gate below, but we can't know latency up front —
    # so we use the raw rate here and let the post-hoc gate catch stragglers.
    from pipeline.observability import should_sample as _should_sample
    from pipeline import settings as _settings
    _pre_sample = (not _obs_enabled()) or _should_sample(
        latency_ms=0, status_code=200, route="/search"
    )
    with (_obs_start_span(
        "search.request",
        input={"query": req.query, "mode": req.mode, "top_k": req.top_k},
    ) if _pre_sample else _NullContext()):
        if _obs_enabled() and _obs_client() is not None:
            import hashlib
            from pipeline import settings as _settings
            config_hash = str(hash(str(req.config_overrides)))
            overrides = dict(req.config_overrides or {})
            cache_backend = "redis" if getattr(_cache, "_redis_enabled", False) else "memory"
            retrieval_cache_enabled = bool(
                overrides.get("use_retrieval_cache", settings.search_use_retrieval_cache)
            )
            attr_ctx = _obs_trace_attributes(
                user_id=req.recruiter_id, session_id=req.session_id,
                tags=[
                    req.mode,
                    "search",
                    f"config:{config_hash}",
                    f"cache:{cache_backend}",
                    f"retrieval-cache:{'on' if retrieval_cache_enabled else 'off'}",
                    f"keyword:{req.keyword_policy}",
                    f"bm25:{settings.bm25_backend}",
                ],
            )
            user_id_hash = hashlib.sha256(req.recruiter_id.encode()).hexdigest()[:12] if req.recruiter_id else None
            _obs_update_current_span(metadata={
                "environment": _settings.langfuse_environment,
                "release": _settings.langfuse_release,
                "route": "/search",
                "search_mode": req.mode,
                "ranking_version": "1.0",
                "user_id_hash": user_id_hash,
            })
        else:
            attr_ctx = _NullContext()

        with attr_ctx:
            start = time.perf_counter()
            timings_ms: dict[str, float] = {}
            cache_before = _cache.cache_snapshot()

            engine: HybridSearchEngine = app.state.search_engine

            # Build explicit_filters dict from structured request fields
            explicit_filters: dict[str, Any] = {}
            if req.country:           explicit_filters["country"]        = req.country
            if req.city:              explicit_filters["city"]           = req.city
            if req.min_years_exp is not None: explicit_filters["min_years_exp"] = req.min_years_exp
            if req.max_years_exp is not None: explicit_filters["max_years_exp"] = req.max_years_exp
            if req.min_salary is not None:    explicit_filters["min_salary"]    = req.min_salary
            if req.max_salary is not None:    explicit_filters["max_salary"]    = req.max_salary
            if req.skills is not None: explicit_filters["skills"]         = req.skills
            if req.skills is not None: explicit_filters["skills_match"]   = req.skills_match
            if req.should:
                explicit_filters["should"] = req.should
                explicit_filters.setdefault("skills", [])
            if req.skill_weights:     explicit_filters["skill_weights"]  = req.skill_weights
            if req.status:            explicit_filters["status"]         = req.status

            _obs_update_current_span(input={
                "query": req.query,
                "jd_present": bool(req.jd),
                "mode": req.mode,
                "top_k": req.top_k,
                "filters": explicit_filters,
                "keyword": {
                    "policy_override": req.keyword_policy,
                    "timeout_ms": req.keyword_timeout_ms,
                    "backend": settings.bm25_backend,
                },
                "llm": {
                    "provider_override": req.llm_provider,
                    "model_override": req.llm_model,
                },
                "search_database": "separate" if settings.search_database_url else "primary",
                "payload_version": "search-request/v1",
            })

            stage_start = time.perf_counter()
            if hasattr(engine, "smart_search"):
                try:
                    # Merge per-request LLM overrides into config so the
                    # planner actually uses them instead of the global setting.
                    config = dict(req.config_overrides or {})
                    if req.llm_provider:
                        config["llm_provider"] = req.llm_provider
                    if req.llm_model:
                        config["llm_model"] = req.llm_model
                    if req.keyword_policy != "auto":
                        config["keyword_policy"] = req.keyword_policy
                    if req.keyword_timeout_ms is not None:
                        config["keyword_timeout_ms"] = req.keyword_timeout_ms
                    search_resp = await engine.smart_search(
                        query=req.query.strip(),
                        jd=req.jd,
                        explicit_filters=explicit_filters or None,
                        mode=req.mode,
                        top_k=req.top_k,
                        recruiter_id=req.recruiter_id,
                        config_overrides=config,
                    )
                except RuntimeError as exc:
                    if "LLM planner" in str(exc):
                        raise HTTPException(status_code=502, detail=f"LLM error: {exc}") from exc
                    raise
                timings_ms["search_pipeline"] = round((time.perf_counter() - stage_start) * 1000, 2)
            else:
                raw_results = await engine.search(
                    query=req.query.strip(),
                    filters=_build_filters(req),
                    top_k=req.top_k,
                    use_rrf=req.use_rrf,
                    max_chunks_per_candidate=req.max_chunks_per_candidate,
                )
                timings_ms["search_pipeline"] = round((time.perf_counter() - stage_start) * 1000, 2)
                if req.enable_reranking:
                    stage_start = time.perf_counter()
                    raw_results = await _apply_optional_reranker(req, req.query.strip(), raw_results)
                    timings_ms["rerank"] = round((time.perf_counter() - stage_start) * 1000, 2)
                search_resp = SimpleNamespace(
                    results=raw_results,
                    clarify=None,
                    relaxations_applied=[],
                )

            # Clarify-only response: planner asked for clarification and pulled
            # nothing useful from the input. When clarify is paired with real
            # results (partial-extraction fallback in smart_search) we fall through
            # and surface clarify as a soft notice alongside the results.
            if search_resp.clarify and not search_resp.results:
                elapsed_ms = (time.perf_counter() - start) * 1000
                return SearchResponse(
                    query=req.query,
                    filters_applied=explicit_filters,
                    results=[],
                    total_results=0,
                    latency_ms=round(elapsed_ms, 2),
                    timings_ms={"total": round(elapsed_ms, 2)},
                    retrieval_policy=getattr(search_resp, "retrieval_policy", {}),
                    clarify=search_resp.clarify,
                    relaxations_applied=[],
                    dropped_items=list(getattr(getattr(search_resp, "spec", None), "dropped_items", []) or []),
                    personalization_offer=getattr(getattr(search_resp, "spec", None), "personalization_offer", None),
                    langfuse_trace_id=_obs_trace_id(),
                    langfuse_trace_url=_obs_trace_url(),
                )

            include_explanation = _should_include_ranking_explanation(req)
            stage_start = time.perf_counter()
            spec = getattr(search_resp, "spec", None)
            results = [
                _to_candidate_result(r, rank_position=i + 1 if include_explanation else None, spec=spec)
                for i, r in enumerate(search_resp.results)
            ]
            timings_ms["result_formatting"] = round((time.perf_counter() - stage_start) * 1000, 2)

            search_elapsed_ms = (time.perf_counter() - start) * 1000
            timings_ms["search_total"] = round(search_elapsed_ms, 2)
            if search_elapsed_ms > 400:
                logger.warning("Search latency %.1fms above 400ms target (mode=%s)", search_elapsed_ms, req.mode)

            trace_id = _obs_trace_id()
            trace_url = _obs_trace_url(trace_id)

            # Cache with stable key so /search/insights never reruns the search
            insight_inputs = [
                candidate_insight_input_from_result(r)
                for r in search_resp.results
            ]
            await _cache.set(
                _insights_cache_key(req),
                _insight_inputs_to_cache_payload(insight_inputs),
            )

            # Inline AI insights when requested — runs as a child of this span
            inline_insights: Optional[AIInsightResponse] = None
            if req.include_ai_insights and results:
                compare_limit = min(len(results), 5)
                candidates = [candidate_insight_input_from_result(r) for r in results[:compare_limit]]
                service = AIInsightService(
                    provider=settings.insights_llm_provider or None,
                    model=settings.insights_llm_model or None,
                    thinking_level=settings.insights_thinking_level or None,
                )
                insight = await service.generate(
                    query=req.query.strip(),
                    filters_applied=_filters_dict(req),
                    candidates=candidates,
                )
                insight_timings = dict(insight.timings_ms or {})
                for key, value in insight_timings.items():
                    timings_ms[f"insights_{key}"] = value
                inline_insights = _to_ai_insight_response(insight, insight_timings)
                if getattr(service, "verify_grounding", False):
                    background_tasks.add_task(
                        service.run_grounding_check, insight, candidates, _obs_trace_id()
                    )

            total_elapsed_ms = (time.perf_counter() - start) * 1000
            timings_ms["total"] = round(total_elapsed_ms, 2)
            cache_after = _cache.cache_snapshot()
            cache_request_delta = _cache.cache_delta(cache_before, cache_after)

            # Emit metadata + scores only for sampled requests. Use the same
            # timing keys as the response so the root trace, scores, and API
            # JSON tell the same story.
            from pipeline.observability import should_sample
            sampled = should_sample(latency_ms=total_elapsed_ms, status_code=200, route="/search")

            if _obs_enabled() and sampled:
                retrieval_policy = getattr(search_resp, "retrieval_policy", {}) or {}
                retrieval_timing_summary = retrieval_policy.get("timing_summary", {}) or {}
                retrieval_metadata = {
                    "candidate_count": len(results),
                    "query_original": req.query,
                    "query_rewritten": getattr(getattr(search_resp, "spec", None), "semantic_query", None),
                    "filters_applied": explicit_filters,
                    "top_result_ids": [r.candidate_id for r in results[:5]],
                    "top_scores": [round(r.rank_score or r.rrf_score or 0.0, 2) for r in results[:5]],
                    "score_breakdown": [getattr(r, "ranking_signals", []) for r in search_resp.results[:3]],
                    "retrieval_policy": retrieval_policy,
                    "retrieval_timing_summary": retrieval_timing_summary,
                    "latency_breakdown_ms": dict(timings_ms),
                    "cache": cache_after,
                    "cache_request_delta": cache_request_delta,
                    "cache_backend": cache_after.get("backend"),
                    "cache_namespace": cache_after.get("namespace"),
                    "use_cache": bool((req.config_overrides or {}).get("use_cache", True)),
                    "use_retrieval_cache": bool(
                        (req.config_overrides or {}).get(
                            "use_retrieval_cache",
                            settings.search_use_retrieval_cache,
                        )
                    ),
                    "redis_enabled": bool(getattr(_cache, "_redis_enabled", False)),
                    "has_inline_insights": inline_insights is not None,
                }
                if inline_insights is not None:
                    retrieval_metadata["insights"] = {
                        "status": inline_insights.status,
                        "provider": inline_insights.provider,
                        "model": inline_insights.model,
                        "best_candidate_id": inline_insights.best_candidate_id,
                    }
                if _obs_client() is not None:
                    try:
                        _obs_client().update_current_span(
                            output={
                                "total_results": len(results),
                                "top_candidates": [r.candidate_id for r in results[:5]],
                                "has_inline_insights": inline_insights is not None,
                                "latency_breakdown_ms": dict(timings_ms),
                                "retrieval_timing_summary": retrieval_timing_summary,
                                "cache_summary": cache_after.get("summary", {}),
                                "cache_namespaces": cache_after.get("namespaces", {}),
                                "cache_request_delta": cache_request_delta.get("summary", {}),
                                "cache_namespaces_delta": cache_request_delta.get("namespaces", {}),
                            },
                            metadata=retrieval_metadata,
                        )
                    except Exception as exc:
                        logger.debug("Langfuse update_current_span failed: %s", exc)

                from pipeline.observability import record_score, ScoreName
                trace_id = _obs_client().get_current_trace_id() if _obs_client() is not None else None
                if trace_id:
                    record_score(trace_id=trace_id, name=ScoreName.REQUEST_LATENCY_MS, value=total_elapsed_ms)
                    record_score(trace_id=trace_id, name=ScoreName.SEARCH_LATENCY_MS, value=search_elapsed_ms)
                    cache_delta_summary = cache_request_delta.get("summary", {}) or {}
                    record_score(
                        trace_id=trace_id,
                        name=ScoreName.CACHE_HIT_RATE_REQUEST,
                        value=float(cache_delta_summary.get("hit_rate", 0.0) or 0.0),
                    )
                    record_score(
                        trace_id=trace_id,
                        name=ScoreName.CACHE_L1_HITS_REQUEST,
                        value=float(cache_delta_summary.get("l1_hits", 0) or 0),
                    )
                    record_score(
                        trace_id=trace_id,
                        name=ScoreName.CACHE_L2_HITS_REQUEST,
                        value=float(cache_delta_summary.get("l2_hits", 0) or 0),
                    )
                    record_score(
                        trace_id=trace_id,
                        name=ScoreName.CACHE_MISSES_REQUEST,
                        value=float(cache_delta_summary.get("misses", 0) or 0),
                    )
                    phase_timings = getattr(search_resp, "phase_timings", {}) or {}
                    if "query_understanding_ms" in phase_timings:
                        record_score(
                            trace_id=trace_id,
                            name=ScoreName.SEARCH_QUERY_UNDERSTANDING_LATENCY_MS,
                            value=phase_timings["query_understanding_ms"],
                        )
                    if "retrieval_ms" in phase_timings:
                        record_score(
                            trace_id=trace_id,
                            name=ScoreName.SEARCH_RETRIEVAL_LATENCY_MS,
                            value=phase_timings["retrieval_ms"],
                        )
                    if "ranking_ms" in phase_timings:
                        record_score(
                            trace_id=trace_id,
                            name=ScoreName.SEARCH_RANKING_LATENCY_MS,
                            value=phase_timings["ranking_ms"],
                        )
                    branch_timings = (
                        retrieval_policy
                    ).get("timings_ms", {}) or {}
                    branch_scores = {
                        "count_ms": ScoreName.SEARCH_COUNT_LATENCY_MS,
                        "dense_ms": ScoreName.SEARCH_DENSE_LATENCY_MS,
                        "keyword_ms": ScoreName.SEARCH_KEYWORD_LATENCY_MS,
                        "skill_ms": ScoreName.SEARCH_SKILL_LATENCY_MS,
                        "enrichment_ms": ScoreName.SEARCH_ENRICHMENT_LATENCY_MS,
                    }
                    for key, score_name in branch_scores.items():
                        if key in branch_timings:
                            record_score(trace_id=trace_id, name=score_name, value=branch_timings[key])
                    if retrieval_timing_summary:
                        if "largest_pool_wait_ms" in retrieval_timing_summary:
                            record_score(
                                trace_id=trace_id,
                                name=ScoreName.SEARCH_DB_POOL_WAIT_MAX_MS,
                                value=retrieval_timing_summary["largest_pool_wait_ms"],
                                comment=str(retrieval_timing_summary.get("largest_pool_wait_branch") or ""),
                            )
                        if "largest_db_roundtrip_ms" in retrieval_timing_summary:
                            record_score(
                                trace_id=trace_id,
                                name=ScoreName.SEARCH_DB_ROUNDTRIP_MAX_MS,
                                value=retrieval_timing_summary["largest_db_roundtrip_ms"],
                                comment=str(retrieval_timing_summary.get("largest_db_roundtrip_branch") or ""),
                            )
                        if "total_db_roundtrip_ms" in retrieval_timing_summary:
                            record_score(
                                trace_id=trace_id,
                                name=ScoreName.SEARCH_DB_ROUNDTRIP_TOTAL_MS,
                                value=retrieval_timing_summary["total_db_roundtrip_ms"],
                            )
                        if "total_app_overhead_ms" in retrieval_timing_summary:
                            record_score(
                                trace_id=trace_id,
                                name=ScoreName.SEARCH_DB_APP_OVERHEAD_TOTAL_MS,
                                value=retrieval_timing_summary["total_app_overhead_ms"],
                            )
                    record_score(trace_id=trace_id, name=ScoreName.SEARCH_RESULT_COUNT, value=len(results))
                    record_score(trace_id=trace_id, name=ScoreName.SEARCH_ZERO_RESULTS, value=1 if not results else 0)
                    if inline_insights is not None and "total" in inline_insights.timings_ms:
                        record_score(
                            trace_id=trace_id,
                            name=ScoreName.INSIGHTS_LATENCY_MS,
                            value=inline_insights.timings_ms["total"],
                        )
                    if inline_insights is not None and "llm" in inline_insights.timings_ms:
                        record_score(
                            trace_id=trace_id,
                            name=ScoreName.LLM_LATENCY_MS,
                            value=inline_insights.timings_ms["llm"],
                            comment="inline search insights",
                        )
                    if results:
                        top_score = results[0].rank_score or results[0].rrf_score or results[0].similarity_score or 0.0
                        record_score(trace_id=trace_id, name=ScoreName.SEARCH_TOP_SCORE, value=top_score)

            _enqueue_search_history(
                app,
                recruiter_id=req.recruiter_id,
                query=req.query or "",
                filters=explicit_filters,
                results=search_resp.results,
                latency_ms=int(round(total_elapsed_ms)),
            )

            return SearchResponse(
                query=req.query,
                mode=req.mode,
                filters_applied=explicit_filters,
                results=results,
                total_results=max(
                    len(results),
                    len(getattr(search_resp, "candidate_ids", []) or []),
                ),
                candidate_ids=list(getattr(search_resp, "candidate_ids", []) or [r.candidate_id for r in search_resp.results]),
                deferred_candidate_ids=list(getattr(search_resp, "deferred_candidate_ids", []) or []),
                latency_ms=round(total_elapsed_ms, 2),
                timings_ms=timings_ms,
                phase_timings=getattr(search_resp, "phase_timings", {}),
                retrieval_policy=getattr(search_resp, "retrieval_policy", {}),
                planner_spec=getattr(search_resp, "spec_dict", None),
                clarify=getattr(search_resp, "clarify", None),
                relaxations_applied=search_resp.relaxations_applied,
                dropped_items=list(getattr(getattr(search_resp, "spec", None), "dropped_items", []) or []),
                personalization_applied=getattr(search_resp, "personalization_applied", False),
                personalization_offer=getattr(getattr(search_resp, "spec", None), "personalization_offer", None),
                langfuse_trace_id=trace_id,
                langfuse_trace_url=trace_url,
                ai_insights=inline_insights,
            )

class SimilarRequest(BaseModel):
    """'More like this' — find candidates similar to a chosen one."""
    candidate_id: str
    top_k: int = Field(default=10, ge=1, le=100)
    include_rank_explanation: bool = True


class CandidateBatchRequest(BaseModel):
    candidate_ids: List[str] = Field(default_factory=list)
    limit: int = Field(default=50, ge=1, le=50)


@app.post("/search/similar", response_model=SearchResponse)
async def search_similar(req: SimilarRequest):
    """Find candidates similar to a chosen one via vector kNN over their chunk embeddings."""
    start = time.perf_counter()
    pool = app.state.pool

    # Average the source candidate's chunk embeddings into a single centroid vector.
    # Restricted to active candidates and excludes the source candidate itself.
    sql = """
        WITH src AS (
            SELECT AVG(embedding)::vector AS centroid
            FROM document_chunks
            WHERE candidate_id = $1::uuid
        ),
        ranked AS (
            SELECT
                dc.id::text            AS chunk_id,
                dc.candidate_id::text  AS candidate_id,
                dc.content,
                dc.document_id::text   AS document_id,
                cd.doc_type,
                cd.title               AS document_title,
                (dc.embedding <=> (SELECT centroid FROM src)) AS distance,
                ROW_NUMBER() OVER (
                    PARTITION BY dc.candidate_id
                    ORDER BY dc.embedding <=> (SELECT centroid FROM src)
                ) AS rn
            FROM document_chunks dc
            JOIN candidate_documents cd ON cd.id = dc.document_id
            JOIN candidates c           ON c.id = dc.candidate_id
            WHERE dc.candidate_id != $1::uuid
              AND c.status = 'active'
        )
        SELECT chunk_id, candidate_id, content, document_id,
               doc_type, document_title, distance
        FROM ranked
        WHERE rn = 1
        ORDER BY distance
        LIMIT $2
    """
    try:
        rows = await pool.fetch(sql, req.candidate_id, req.top_k)
    except Exception as exc:
        logger.error("similar search failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"similar search failed: {exc}") from exc

    if not rows:
        elapsed_ms = (time.perf_counter() - start) * 1000
        return SearchResponse(
            query=f"more-like:{req.candidate_id}",
            filters_applied={"source_candidate_id": req.candidate_id},
            results=[],
            total_results=0,
            latency_ms=round(elapsed_ms, 2),
            timings_ms={"total": round(elapsed_ms, 2)},
            clarify=None,
            relaxations_applied=[],
        )

    # Enrich with candidate profile fields.
    cand_ids = [r["candidate_id"] for r in rows]
    profiles = await pool.fetch(
        """SELECT id::text AS candidate_id, full_name, email, city, country,
                  salary_min, salary_max, years_exp, skills
           FROM candidates WHERE id = ANY($1::uuid[])""",
        cand_ids,
    )
    pmap = {p["candidate_id"]: p for p in profiles}

    results: list[CandidateResult] = []
    for r in rows:
        p = pmap.get(r["candidate_id"], {})
        distance = float(r["distance"] or 0.0)
        sim = max(0.0, min(1.0, 1.0 - distance))
        results.append(CandidateResult(
            candidate_id=r["candidate_id"],
            full_name=p.get("full_name") or "",
            email=p.get("email"),
            city=p.get("city"),
            country=p.get("country"),
            salary_min=p.get("salary_min"),
            salary_max=p.get("salary_max"),
            years_exp=int(p.get("years_exp") or 0) if p.get("years_exp") is not None else None,
            skills=list(p.get("skills") or []),
            best_chunk=r["content"] or "",
            similarity_score=round(sim, 4),
            doc_type=r["doc_type"] or "",
            document_title=r["document_title"],
            supporting_chunks=[],
            rerank_score=None,
            rrf_score=None,
            rank_score=round(sim, 4),
            ranking_explanation=None,
        ))

    elapsed_ms = (time.perf_counter() - start) * 1000
    return SearchResponse(
        query=f"more-like:{req.candidate_id}",
        filters_applied={"source_candidate_id": req.candidate_id},
        results=results,
        total_results=len(results),
        latency_ms=round(elapsed_ms, 2),
        timings_ms={"total": round(elapsed_ms, 2)},
        clarify=None,
        relaxations_applied=[],
    )


class OutcomeRequest(BaseModel):
    impression_id: str
    action: Literal["viewed", "saved", "contacted", "shortlisted", "archived", "rejected", "flagged_hallucination"]
    reason: Optional[str] = None


class OutcomeLogItem(BaseModel):
    candidate_id: str
    candidate_name: str
    action: str
    reason: Optional[str] = None
    occurred_at: str


class OutcomeHistoryResponse(BaseModel):
    items: list[OutcomeLogItem]


def _extract_json_object(raw: str) -> str:
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("No JSON object found")
    return raw[start:end + 1]


async def _infer_outcome_observation(
    *,
    action: str,
    reason: str,
    candidate: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Use an LLM to decide whether a reason implies a durable preference.

    Returns an unconfirmed observation candidate or None for one-off feedback.
    The caller still confirmation-gates it via recruiter_memory.kind=observation.
    """
    if action not in {"shortlisted", "rejected"} or not reason:
        return None
    candidate = dict(candidate)

    fallback_category = "other"
    lowered = reason.lower()
    if any(w in lowered for w in ("react", "python", "java", "skill", "skills", "frontend", "backend")):
        fallback_category = "skill"
    elif any(w in lowered for w in ("remote", "hybrid", "onsite")):
        fallback_category = "work_style"
    elif any(w in lowered for w in ("senior", "junior", "experience", "years")):
        fallback_category = "seniority"
    elif any(w in lowered for w in ("salary", "compensation", "expensive", "budget")):
        fallback_category = "salary"
    elif any(w in lowered for w in ("berlin", "london", "city", "location", "country")):
        fallback_category = "location"

    try:
        from pipeline import settings as _settings
        from pipeline.observability import (
            get_async_openai,
            start_span as _obs_start_span,
            update_current_span as _obs_update_current_span,
        )
        provider = getattr(_settings, "llm_provider", "groq")
        model = getattr(_settings, "llm_model", "")
        api_key = _settings.openai_api_key
        base_url = None
        if provider == "groq":
            api_key = _settings.groq_api_key
            base_url = "https://api.groq.com/openai/v1"
        if not api_key:
            raise RuntimeError("No OpenAI-compatible key configured")
        AsyncOpenAI = get_async_openai()
        supports_langfuse_name = getattr(AsyncOpenAI, "__module__", "").startswith("langfuse.")
        client = AsyncOpenAI(api_key=api_key, base_url=base_url) if base_url else AsyncOpenAI(api_key=api_key)
        messages = [
            {
                "role": "system",
                "content": (
                    "You classify recruiter accept/reject reasons into durable hiring preferences. "
                    "Return ONLY JSON. If the reason is one-off, about this candidate only, too vague, "
                    "or not useful for future ranking, return {\"create\": false}. "
                    "Otherwise return {\"create\": true, \"category\": one of "
                    "[\"skill\",\"location\",\"seniority\",\"company_stage\",\"work_style\",\"salary\",\"other\"], "
                    "\"content\": a concise observation beginning with Usually/Often/Prefers/Avoids, "
                    "\"confidence\": number 0.0-0.95}."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({
                    "action": action,
                    "reason": reason,
                    "candidate": {
                        "id": str(candidate.get("candidate_id") or ""),
                        "name": candidate.get("full_name") or "",
                        "city": candidate.get("city") or "",
                        "country": candidate.get("country") or "",
                        "skills": candidate.get("skills") or [],
                        "years_exp": candidate.get("years_exp"),
                        "salary_min": candidate.get("salary_min"),
                        "salary_max": candidate.get("salary_max"),
                    },
                }),
            },
        ]
        with _obs_start_span(
            "feedback.infer_preference",
            input={"action": action, "reason": reason[:240], "fallback_category": fallback_category},
            metadata={"provider": provider, "model": model or "llama-3.3-70b-versatile"},
        ):
            create_kwargs = {
                "model": model or "llama-3.3-70b-versatile",
                "messages": messages,
                "temperature": 0,
                "max_tokens": 300,
                "response_format": {"type": "json_object"},
            }
            if supports_langfuse_name:
                create_kwargs["name"] = "feedback.infer_preference.model"
            resp = await client.chat.completions.create(**create_kwargs)
            data = json.loads(_extract_json_object(resp.choices[0].message.content or "{}"))
            if not data.get("create"):
                _obs_update_current_span(output={"create": False})
                return None
            category = data.get("category") if data.get("category") in {
                "skill", "location", "seniority", "company_stage", "work_style", "salary", "other"
            } else fallback_category
            content = " ".join(str(data.get("content") or "").split())[:240]
            confidence = float(data.get("confidence") or 0.0)
            if not content or confidence < 0.55:
                _obs_update_current_span(output={"create": False, "confidence": confidence})
                return None
            result = {"category": category, "content": content, "confidence": min(confidence, 0.95)}
            _obs_update_current_span(output=result)
            return result
    except Exception:
        # Conservative fallback: only create an observation for explicit
        # preference language, never for generic "good/bad candidate" notes.
        preference_markers = ("prefer", "prioritize", "like", "avoid", "must have", "too expensive", "remote only")
        if not any(m in lowered for m in preference_markers):
            return None
        prefix = "Prefers" if action == "shortlisted" else "Avoids"
        return {
            "category": fallback_category,
            "content": f"{prefix} candidates where: {reason[:180]}",
            "confidence": 0.6,
        }


async def _maybe_create_observation_from_outcome_reason(
    pool: Any,
    *,
    impression_id: str | None = None,
    recruiter_id: str | None = None,
    candidate_id: str | None = None,
    action: str,
    reason: str | None,
) -> dict[str, Any] | None:
    if not reason:
        return None
    if impression_id:
        row = await pool.fetchrow(
            """
            SELECT i.recruiter_id, i.candidate_id, c.full_name, c.city, c.country,
                   c.skills, c.years_exp, c.salary_min, c.salary_max
            FROM search_impressions i
            JOIN candidates c ON c.id = i.candidate_id
            WHERE i.id = $1::uuid
            """,
            impression_id,
        )
    elif recruiter_id and candidate_id:
        row = await pool.fetchrow(
            """
            SELECT $1::uuid AS recruiter_id, c.id AS candidate_id, c.full_name,
                   c.city, c.country, c.skills, c.years_exp, c.salary_min, c.salary_max
            FROM candidates c
            WHERE c.id = $2::uuid
            """,
            recruiter_id,
            candidate_id,
        )
    else:
        row = None
    if not row:
        return None
    inferred = await _infer_outcome_observation(
        action=action,
        reason=reason,
        candidate=row,
    )
    if not inferred:
        return None
    content_key = f"outcome:{inferred['category']}:{_re.sub(r'[^a-z0-9]+', '-', inferred['content'].lower()).strip('-')[:80]}"
    await pool.execute(
        """
        INSERT INTO recruiter_memory
          (recruiter_id, kind, category, content, content_key, source,
           confidence, evidence_count, evidence)
        VALUES
          ($1::uuid, 'observation', $2, $3, $4, 'agent',
           $5, 1, $6::jsonb)
        ON CONFLICT (recruiter_id, kind, content_key) WHERE status='active'
        DO UPDATE SET
          confidence = GREATEST(recruiter_memory.confidence, EXCLUDED.confidence),
          evidence_count = recruiter_memory.evidence_count + 1,
          last_evidence_at = NOW(),
          evidence = recruiter_memory.evidence || EXCLUDED.evidence
        """,
        str(row["recruiter_id"]),
        inferred["category"],
        inferred["content"],
        content_key,
        float(inferred["confidence"]),
        json.dumps([{
            "type": "outcome_reason",
            "action": action,
            "reason": reason,
            "candidate_id": str(row["candidate_id"]),
            "impression_id": impression_id,
        }]),
    )
    return inferred


class OutcomeReasonRequest(BaseModel):
    candidate_id: str
    action: Literal["shortlisted", "rejected"]
    reason: str


@app.post("/api/recruiter/{recruiter_id}/outcome-reason")
async def record_candidate_outcome_reason(recruiter_id: str, req: OutcomeReasonRequest):
    reason = " ".join((req.reason or "").split())[:500]
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required")
    result = await _maybe_create_observation_from_outcome_reason(
        app.state.pool,
        recruiter_id=recruiter_id,
        candidate_id=req.candidate_id,
        action=req.action,
        reason=reason,
    )
    return {"observation": result}


class SearchHistoryEntry(BaseModel):
    id: int
    query: str | None = None
    filters_json: dict = {}
    results_json: list = []
    latency_ms: int | None = None
    timestamp: str


class SearchHistoryResponse(BaseModel):
    entries: list[SearchHistoryEntry]


@app.get("/search/history", response_model=SearchHistoryResponse)
async def get_search_history(recruiter_id: str, limit: int = 50):
    """Return the most recent searches performed by a recruiter."""
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT id, query, filters_json, results_json, latency_ms,
                  timestamp::text AS timestamp
           FROM search_history
           WHERE recruiter_id = $1
           ORDER BY timestamp DESC
           LIMIT $2""",
        recruiter_id, min(limit, 200),
    )
    return SearchHistoryResponse(entries=[dict(r) for r in rows])


@app.post("/outcomes", status_code=204)
async def record_outcome(req: OutcomeRequest):
    """Record a recruiter action on a shown candidate (for feedback loop / LTR)."""
    pool = app.state.pool
    try:
        reason = " ".join((req.reason or "").split())[:500] or None
        if reason:
            status = await pool.execute(
                """UPDATE search_outcomes
                   SET reason = $3
                   WHERE id = (
                     SELECT id FROM search_outcomes
                     WHERE impression_id = $1::uuid AND action = $2
                     ORDER BY occurred_at DESC
                     LIMIT 1
                   )""",
                req.impression_id, req.action, reason,
            )
            if status == "UPDATE 0":
                await pool.execute(
                    """INSERT INTO search_outcomes (impression_id, action, reason)
                       VALUES ($1::uuid, $2, $3)""",
                    req.impression_id, req.action, reason,
                )
        else:
            await pool.execute(
                """INSERT INTO search_outcomes (impression_id, action, reason)
                   VALUES ($1::uuid, $2, NULL)""",
                req.impression_id, req.action,
            )

        inferred_observation = await _maybe_create_observation_from_outcome_reason(
            pool,
            impression_id=req.impression_id,
            action=req.action,
            reason=reason,
        )
        
        # Phase C: Post outcome score back to Langfuse trace
        row = await pool.fetchrow(
            "SELECT langfuse_trace_id, candidate_id, recruiter_id, position FROM search_impressions WHERE id = $1::uuid",
            req.impression_id
        )
        if row and row["recruiter_id"]:
            await _cache.invalidate(_cache.recruiter_profile_key(str(row["recruiter_id"])))
        if row and row["langfuse_trace_id"]:
            from pipeline.observability import record_score, ScoreName
            trace_id = row["langfuse_trace_id"]
            # Action literals match the search_outcomes CHECK constraint
            # (migration 004): viewed, saved, contacted, shortlisted,
            # archived, rejected.
            action = req.action.lower()

            # RECRUITER_ACTION is CATEGORICAL — store the action string itself
            # so dashboards can break down by action type.
            # Identify which candidate this verdict is about — the trace is
            # shared across all candidates in the search, so without this the
            # score is ambiguous when several candidates are acted on.
            cand_comment = f"{row['candidate_id']} | pos={row['position']}" if "candidate_id" in row else str(req.impression_id)
            if reason:
                cand_comment = f"{cand_comment} | reason={reason}"
            if inferred_observation:
                cand_comment = f"{cand_comment} | observation={inferred_observation['content']}"

            record_score(
                trace_id=trace_id,
                name=ScoreName.RECRUITER_ACTION,
                value=action,
                comment=cand_comment,
            )

            # POSITIVE_OUTCOME is the numeric/boolean signal: did the recruiter
            # act favourably on this candidate?
            POSITIVE = {"saved", "contacted", "shortlisted"}
            NEGATIVE = {"rejected", "archived", "flagged_hallucination"}
            if action in POSITIVE:
                record_score(trace_id=trace_id, name=ScoreName.POSITIVE_OUTCOME, value=1.0, comment=cand_comment)
            elif action in NEGATIVE:
                record_score(trace_id=trace_id, name=ScoreName.POSITIVE_OUTCOME, value=0.0, comment=cand_comment)
            # 'viewed' is neutral — no POSITIVE_OUTCOME score emitted.

    except Exception as exc:
        logger.warning("Outcome recording failed: %s", exc)


@app.get("/outcomes", response_model=OutcomeHistoryResponse)
async def list_outcomes(recruiter_id: str):
    """Fetch history logs of accepted/rejected/viewed candidates for a recruiter."""
    pool = app.state.pool
    try:
        rows = await pool.fetch(
            """
            SELECT c.id as candidate_id, c.full_name as candidate_name, o.action, o.reason, o.occurred_at
            FROM search_outcomes o
            JOIN search_impressions i ON o.impression_id = i.id
            JOIN candidates c ON i.candidate_id = c.id
            WHERE i.recruiter_id = $1::uuid
            ORDER BY o.occurred_at DESC
            LIMIT 200
            """,
            recruiter_id,
        )
        items = [
            OutcomeLogItem(
                candidate_id=str(r["candidate_id"]),
                candidate_name=r["candidate_name"],
                action=r["action"],
                reason=r["reason"],
                occurred_at=r["occurred_at"].isoformat(),
            )
            for r in rows
        ]
        return OutcomeHistoryResponse(items=items)
    except Exception as exc:
        logger.warning("Failed to fetch outcome history: %s", exc)
        return OutcomeHistoryResponse(items=[])


class RelevanceRequest(BaseModel):
    """A recruiter's relevance verdict on a shown candidate.

    Distinct from /outcomes (hiring-workflow actions): this answers "did this
    result match what I searched for?" — a clean retrieval-quality signal.
    """
    impression_id: str
    relevant: Optional[bool] = None   # True = 👍, False = 👎, None = clear the verdict
    recruiter_id: Optional[str] = None


class AgentChatContext(BaseModel):
    """Context passed alongside a chat message — previous search state."""
    query: Optional[str] = None
    filters: Optional[Dict[str, Any]] = None
    # Tolerant: the UI may send nulls/mixed values; we coerce + filter at use.
    result_ids: Optional[List[Any]] = None
    candidate_ids: Optional[List[Any]] = None
    candidate_summaries: Optional[List[Dict[str, Any]]] = None
    session_messages: Optional[List[Dict[str, Any]]] = None
    session_summary: Optional[str] = None
    hints: Optional[List[Any]] = None


class AgentChatRequest(BaseModel):
    """Request body for the agent chat endpoint."""
    recruiter_id: str
    session_id: str
    message: str
    context: Optional[AgentChatContext] = None
    model: Optional[str] = None


_OBS_CONFIRM_YES = {"yes", "y", "yeah", "yep", "sure", "ok", "okay", "please do", "do it", "remember it"}
_OBS_CONFIRM_NO = {"no", "n", "nope", "dont", "don't", "do not", "not now", "ignore it", "dismiss it"}
_OBS_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "candidates", "candidate",
    "experience", "find", "for", "has", "have", "i", "in", "it", "me",
    "of", "often", "please", "recruiter", "show", "that", "the", "to",
    "with", "you",
}


def _agent_text_event(text: str):
    msg_id = str(_uuid.uuid4())
    from pipeline import agent_stream as _agent_stream
    yield _agent_stream.text_start(msg_id)
    yield _agent_stream.text_delta(msg_id, text)
    yield _agent_stream.text_end(msg_id)


def _normalise_confirmation_answer(message: str) -> bool | None:
    text = _re.sub(r"[^a-zA-Z' ]+", " ", message.lower()).strip()
    text = _re.sub(r"\s+", " ", text)
    if text in _OBS_CONFIRM_YES:
        return True
    if text in _OBS_CONFIRM_NO:
        return False
    return None


def _tokens(value: Any) -> set[str]:
    raw = " ".join(str(v) for v in value.values()) if isinstance(value, dict) else str(value)
    return {
        t for t in _re.findall(r"[a-z0-9+#.-]+", raw.lower())
        if len(t) >= 3 and t not in _OBS_STOPWORDS
    }


def _pick_relevant_observation(message: str, ctx: AgentChatContext, observations: list[Any], asked: set[str]):
    context_tokens = _tokens(message) | _tokens(ctx.query or "") | _tokens(ctx.filters or {})
    best = None
    best_overlap = 0
    for obs in observations:
        obs_id = str(getattr(obs, "id", ""))
        if obs_id in asked:
            continue
        if float(getattr(obs, "confidence", 0.0) or 0.0) < 0.7:
            continue
        obs_tokens = _tokens(getattr(obs, "content", ""))
        overlap = len(context_tokens & obs_tokens)
        if overlap > best_overlap:
            best = obs
            best_overlap = overlap
    return best if best_overlap > 0 else None


def _observation_question(content: str) -> str:
    cleaned = content.strip().rstrip(".")
    if cleaned.lower().startswith("often "):
        rest = cleaned[6:].strip()
        words = rest.split(" ", 1)
        if words and words[0].endswith("s") and len(words[0]) > 3:
            words[0] = words[0][:-1]
        cleaned = "I noticed you often " + " ".join(words)
    else:
        cleaned = "I noticed " + cleaned[0].lower() + cleaned[1:] if cleaned else "I noticed a recurring preference"
    return f"{cleaned}. Want me to remember that as a preference for future searches?"


@app.post("/relevance", status_code=204)
async def record_relevance(req: RelevanceRequest):
    """Record (or clear) a per-candidate relevance judgment for retrieval-quality eval."""
    pool = app.state.pool
    try:
        if req.relevant is None:
            # Clear the verdict. Postgres is the source of truth; any previously
            # posted Langfuse score is left as-is (scores are append-only).
            await pool.execute(
                "DELETE FROM relevance_judgments WHERE impression_id = $1::uuid",
                req.impression_id,
            )
            return

        await pool.execute(
            """INSERT INTO relevance_judgments (impression_id, relevant, judged_by)
               VALUES ($1::uuid, $2, $3::uuid)
               ON CONFLICT (impression_id)
               DO UPDATE SET relevant = EXCLUDED.relevant,
                             judged_by = EXCLUDED.judged_by,
                             judged_at = NOW()""",
            req.impression_id, req.relevant, req.recruiter_id,
        )

        # Mirror to Langfuse as a score on the search trace, tagged with the
        # specific candidate so per-candidate relevance is recoverable.
        row = await pool.fetchrow(
            "SELECT langfuse_trace_id, candidate_id, position FROM search_impressions WHERE id = $1::uuid",
            req.impression_id,
        )
        if row and row["langfuse_trace_id"]:
            from pipeline.observability import record_score, ScoreName
            record_score(
                trace_id=row["langfuse_trace_id"],
                name=ScoreName.RETRIEVAL_RELEVANCE,
                value=1.0 if req.relevant else 0.0,
                comment=f"{row['candidate_id']} | pos={row['position']}",
            )
    except Exception as exc:
        logger.warning("Relevance recording failed: %s", exc)


@app.get("/metrics")
async def get_metrics():
    """Process-local metrics snapshot — counters, latency percentiles, cache stats."""
    return metrics_snapshot()


@app.post("/search/insights", response_model=AIInsightResponse)
async def search_insights(req: SearchRequest, background_tasks: BackgroundTasks = None):
    """Generate a separate AI evidence matrix for the current top search results."""
    background_tasks = background_tasks or BackgroundTasks()
    start = time.perf_counter()
    from pipeline.observability import start_span as _ins_start_span
    insights_span = _ins_start_span(
        "search.insights", input={"query": req.query, "top_k": req.top_k})

    with insights_span:
        compare_limit = min(req.top_k, 10)
        stage_start = time.perf_counter()
        candidates = await _candidate_inputs_for_insights(req, compare_limit)
        timings_ms = {"search": round((time.perf_counter() - stage_start) * 1000, 2)}

        service = AIInsightService(
            provider=settings.insights_llm_provider or None,
            model=settings.insights_llm_model or None,
            thinking_level=settings.insights_thinking_level or None,
        )
        insight = await service.generate(
            query=req.query.strip(),
            filters_applied=_filters_dict(req),
            candidates=candidates[:compare_limit],
        )
        timings_ms.update(insight.timings_ms)
        timings_ms["total"] = round((time.perf_counter() - start) * 1000, 2)

        _obs_update_current_span(metadata={
            "insight_status": insight.status,
            "provider": insight.provider,
            "model": insight.model,
        })
        trace_id = _obs_trace_id()
        if trace_id:
            from pipeline.observability import record_score, ScoreName
            if "llm" in insight.timings_ms:
                record_score(trace_id=trace_id, name=ScoreName.LLM_LATENCY_MS,
                             value=insight.timings_ms["llm"])
            record_score(trace_id=trace_id, name=ScoreName.LLM_FALLBACK_USED,
                         value=1.0 if insight.status != "ok" else 0.0)

    # Hallucination grounding runs after response is sent — never blocks the user
    if getattr(service, "verify_grounding", False):
        background_tasks.add_task(
            service.run_grounding_check, insight, candidates[:compare_limit], trace_id
        )
    background_tasks.add_task(shutdown_langfuse)
    return _to_ai_insight_response(insight, timings_ms)


@app.post("/ingest")
async def ingest(req: IngestRequest):
    """Ingest a document: chunk → embed → store vectors."""
    pipeline: IngestionPipeline = app.state.ingestion
    result = await pipeline.ingest(
        candidate_id=req.candidate_id,
        doc_type=req.doc_type,
        title=req.title,
        raw_text=req.raw_text,
        chunk_strategy=req.chunk_strategy,
    )
    return result


@app.post("/candidates")
async def create_candidate(req: CandidateCreate):
    """Create a new candidate record."""
    pool = app.state.pool
    row = await pool.fetchrow(
        """
        INSERT INTO candidates (full_name, email, age, location, city, country,
                                interests, skills, years_exp, salary_min, salary_max)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
        RETURNING id, full_name, created_at
        """,
        req.full_name, req.email, req.age, req.location, req.city, req.country,
        req.interests, req.skills, req.years_exp, req.salary_min, req.salary_max,
    )
    return {"id": str(row["id"]), "full_name": row["full_name"], "created_at": str(row["created_at"])}


@app.get("/candidates/{candidate_id}")
async def get_candidate(candidate_id: str):
    """Get candidate details including document count."""
    pool = app.state.pool
    row = await pool.fetchrow("SELECT * FROM candidates WHERE id = $1", candidate_id)
    if not row:
        raise HTTPException(status_code=404, detail="Candidate not found")

    doc_count = await pool.fetchval(
        "SELECT COUNT(*) FROM candidate_documents WHERE candidate_id = $1", candidate_id,
    )
    chunk_count = await pool.fetchval(
        "SELECT COUNT(*) FROM document_chunks WHERE candidate_id = $1", candidate_id,
    )

    return {
        **dict(row),
        "id": str(row["id"]),
        "document_count": doc_count,
        "chunk_count": chunk_count,
    }


@app.get("/models")
async def available_models():
    """List all available embedding providers and models."""
    return list_available_models()


@app.get("/health")
async def health():
    """Health check with database connectivity and latency measurement."""
    pool = app.state.pool
    start = time.perf_counter()
    db_version = await pool.fetchval("SELECT version()")
    db_latency_ms = (time.perf_counter() - start) * 1000

    candidate_count = await pool.fetchval("SELECT COUNT(*) FROM candidates")
    chunk_count = await pool.fetchval("SELECT COUNT(*) FROM document_chunks")

    return {
        "status": "healthy",
        "database": {
            "version": db_version,
            "latency_ms": round(db_latency_ms, 2),
            "candidates": candidate_count,
            "chunks": chunk_count,
        },
        "config": {
            "embedding_provider": settings.embedding_provider,
            "embedding_model": settings.embedding_model,
            "embedding_dimensions": settings.embedding_dimensions,
            "hnsw_ef_search": settings.hnsw_ef_search,
            "bm25_backend": settings.bm25_backend,
            "search_database": "separate" if settings.search_database_url else "primary",
        },
    }


# ══════════════════════════════════════════
# ADMIN / UI SUPPORT ENDPOINTS
# ══════════════════════════════════════════

@app.get("/admin/stats")
async def admin_stats():
    """Compact dashboard data for the local admin UI."""
    pool = app.state.pool
    start = time.perf_counter()
    await pool.fetchval("SELECT 1")
    db_latency_ms = (time.perf_counter() - start) * 1000

    candidate_count = await pool.fetchval("SELECT COUNT(*) FROM candidates")
    active_count = await pool.fetchval("SELECT COUNT(*) FROM candidates WHERE status = 'active'")
    document_count = await pool.fetchval("SELECT COUNT(*) FROM candidate_documents")
    chunk_count = await pool.fetchval("SELECT COUNT(*) FROM document_chunks")

    return {
        "status": "healthy",
        "database": {
            "latency_ms": round(db_latency_ms, 2),
            "candidates": candidate_count,
            "active_candidates": active_count,
            "documents": document_count,
            "chunks": chunk_count,
        },
        "config": {
            "embedding_provider": settings.embedding_provider,
            "embedding_model": settings.embedding_model,
            "embedding_dimensions": settings.embedding_dimensions,
            "default_top_k": settings.default_top_k,
            "hnsw_ef_search": settings.hnsw_ef_search,
            "bm25_backend": settings.bm25_backend,
            "search_database": "separate" if settings.search_database_url else "primary",
        },
    }


@app.get("/admin/model-settings")
async def admin_model_settings():
    """Return model configuration and masked secret status for the settings page."""
    return _model_settings_payload(app)


@app.post("/admin/model-settings")
async def update_model_settings(req: ModelSettingsUpdate):
    """Persist model settings to .env and apply runtime-safe values."""
    _validate_model_settings_update(req)

    env_updates: dict[str, str] = {}
    if req.pending_embedding_provider is not None:
        env_updates["PENDING_EMBEDDING_PROVIDER"] = req.pending_embedding_provider
        settings.pending_embedding_provider = req.pending_embedding_provider
    if req.pending_embedding_model is not None:
        env_updates["PENDING_EMBEDDING_MODEL"] = req.pending_embedding_model
        settings.pending_embedding_model = req.pending_embedding_model
    if req.pending_embedding_dimensions is not None:
        env_updates["PENDING_EMBEDDING_DIMENSIONS"] = str(req.pending_embedding_dimensions)
        settings.pending_embedding_dimensions = req.pending_embedding_dimensions
    if req.reranker_model is not None:
        env_updates["RERANKER_MODEL"] = req.reranker_model
        settings.reranker_model = req.reranker_model
    if req.llm_provider is not None:
        env_updates["LLM_PROVIDER"] = req.llm_provider
        settings.llm_provider = req.llm_provider
    if req.llm_model is not None:
        env_updates["LLM_MODEL"] = req.llm_model
        settings.llm_model = req.llm_model
    if req.fast_llm_provider is not None:
        env_updates["FAST_LLM_PROVIDER"] = req.fast_llm_provider
        settings.fast_llm_provider = req.fast_llm_provider
    if req.fast_llm_model is not None:
        env_updates["FAST_LLM_MODEL"] = req.fast_llm_model
        settings.fast_llm_model = req.fast_llm_model
    if req.quality_llm_provider is not None:
        env_updates["QUALITY_LLM_PROVIDER"] = req.quality_llm_provider
        settings.quality_llm_provider = req.quality_llm_provider
    if req.quality_llm_model is not None:
        env_updates["QUALITY_LLM_MODEL"] = req.quality_llm_model
        settings.quality_llm_model = req.quality_llm_model
    if req.insights_llm_provider is not None:
        env_updates["INSIGHTS_LLM_PROVIDER"] = req.insights_llm_provider
        settings.insights_llm_provider = req.insights_llm_provider
    if req.insights_llm_model is not None:
        env_updates["INSIGHTS_LLM_MODEL"] = req.insights_llm_model
        settings.insights_llm_model = req.insights_llm_model
    if req.fast_thinking_level is not None:
        env_updates["FAST_THINKING_LEVEL"] = req.fast_thinking_level
        settings.fast_thinking_level = req.fast_thinking_level
    if req.quality_thinking_level is not None:
        env_updates["QUALITY_THINKING_LEVEL"] = req.quality_thinking_level
        settings.quality_thinking_level = req.quality_thinking_level
    if req.insights_thinking_level is not None:
        env_updates["INSIGHTS_THINKING_LEVEL"] = req.insights_thinking_level
        settings.insights_thinking_level = req.insights_thinking_level
    if req.use_personalization is not None:
        env_updates["USE_PERSONALIZATION"] = str(req.use_personalization).lower()
        settings.use_personalization = req.use_personalization

    for key_name, secret in req.api_keys.items():
        if key_name not in API_KEY_ENV_FIELDS or not secret:
            continue
        env_key = API_KEY_ENV_FIELDS[key_name]
        env_updates[env_key] = secret
        setattr(settings, key_name, secret)

    if env_updates:
        _write_env_values(MODEL_ENV_FILE, env_updates)

    return _model_settings_payload(app)


@app.post("/admin/model-settings/warmup")
async def warmup_model_settings():
    """Warm local model paths without making remote LLM calls."""
    _ensure_model_state(app)
    statuses = {}
    statuses["embedder"] = await _warmup_active_embedder(app)
    statuses["reranker"] = await _warmup_reranker(app, settings.reranker_model)
    return {"status": "ok", "warmups": statuses}


@app.post("/admin/model-settings/validate-llm")
async def validate_llm_model_settings(req: LLMValidationRequest):
    """Make a tiny remote LLM call for the selected provider/model row."""
    return await _validate_llm_model(
        provider=req.provider or None,
        model=req.model or None,
        thinking_level=req.thinking_level or None,
    )


@app.get("/admin/filter-options")
async def admin_filter_options():
    """Distinct database values used to populate search and table filters."""
    pool = app.state.pool

    countries = await pool.fetch(
        """
        SELECT DISTINCT country AS value
        FROM candidates
        WHERE country IS NOT NULL AND country <> ''
        ORDER BY country
        """
    )
    cities = await pool.fetch(
        """
        SELECT DISTINCT city AS value
        FROM candidates
        WHERE city IS NOT NULL AND city <> ''
        ORDER BY city
        """
    )
    locations = await pool.fetch(
        """
        SELECT DISTINCT country, city
        FROM candidates
        WHERE country IS NOT NULL OR city IS NOT NULL
        ORDER BY country NULLS LAST, city NULLS LAST
        """
    )
    location_values = await pool.fetch(
        """
        SELECT DISTINCT location AS value
        FROM candidates
        WHERE location IS NOT NULL AND location <> ''
        ORDER BY location
        """
    )
    skills = await pool.fetch(
        """
        SELECT DISTINCT skill.value AS value
        FROM candidates c
        CROSS JOIN LATERAL unnest(c.skills) AS skill(value)
        WHERE skill.value <> ''
        ORDER BY skill.value
        """
    )
    interests = await pool.fetch(
        """
        SELECT DISTINCT interest.value AS value
        FROM candidates c
        CROSS JOIN LATERAL unnest(c.interests) AS interest(value)
        WHERE interest.value <> ''
        ORDER BY interest.value
        """
    )
    doc_types = await pool.fetch(
        """
        SELECT DISTINCT doc_type AS value
        FROM candidate_documents
        WHERE doc_type IS NOT NULL AND doc_type <> ''
        ORDER BY doc_type
        """
    )
    statuses = await pool.fetch(
        """
        SELECT DISTINCT status AS value
        FROM candidates
        WHERE status IS NOT NULL AND status <> ''
        ORDER BY status
        """
    )
    ranges = await pool.fetchrow(
        """
        SELECT
            MIN(years_exp)::int AS min_years_exp,
            MAX(years_exp)::int AS max_years_exp,
            MIN(age)::int AS min_age,
            MAX(age)::int AS max_age
        FROM candidates
        """
    )

    return {
        "countries": _option_values(countries),
        "cities": _option_values(cities),
        "locations": [
            {"country": row["country"], "city": row["city"]}
            for row in locations
            if row["country"] or row["city"]
        ],
        "location_values": _option_values(location_values),
        "skills": _skill_option_values(skills),
        "interests": _option_values(interests),
        "doc_types": _option_values(doc_types),
        "statuses": _option_values(statuses),
        "ranges": dict(ranges) if ranges else {},
    }


@app.get("/admin/candidates")
async def admin_list_candidates(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    q: Optional[str] = None,
    name_email: Optional[str] = None,
    city: Optional[str] = None,
    country: Optional[str] = None,
    skills: Optional[str] = None,
    status: Optional[Literal["active", "archived", "hired", "rejected"]] = None,
):
    """List candidates with document/chunk counts for the admin UI."""
    pool = app.state.pool
    where_sql, params = _candidate_list_filters(
        q=q,
        status=status,
        name_email=name_email,
        city=city,
        country=country,
        skills=_split_csv(skills),
    )

    total = await pool.fetchval(
        f"SELECT COUNT(*) FROM candidates c {where_sql}",
        *params,
    )

    limit_param = len(params) + 1
    offset_param = len(params) + 2
    rows = await pool.fetch(
        f"""
        SELECT
            c.*,
            COALESCE(doc_counts.document_count, 0)::int AS document_count,
            COALESCE(chunk_counts.chunk_count, 0)::int AS chunk_count
        FROM candidates c
        LEFT JOIN (
            SELECT candidate_id, COUNT(*)::int AS document_count
            FROM candidate_documents
            GROUP BY candidate_id
        ) doc_counts ON doc_counts.candidate_id = c.id
        LEFT JOIN (
            SELECT candidate_id, COUNT(*)::int AS chunk_count
            FROM document_chunks
            GROUP BY candidate_id
        ) chunk_counts ON chunk_counts.candidate_id = c.id
        {where_sql}
        ORDER BY c.created_at DESC
        LIMIT ${limit_param} OFFSET ${offset_param}
        """,
        *params,
        limit,
        offset,
    )

    return {
        "items": [_row_to_admin_candidate(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.patch("/admin/candidates/{candidate_id}")
async def admin_update_candidate(candidate_id: str, req: CandidateUpdate):
    """Update a candidate record from the local admin UI."""
    pool = app.state.pool
    sql, params = _build_candidate_update(candidate_id, req)
    row = await pool.fetchrow(sql, *params)
    if not row:
        raise HTTPException(status_code=404, detail="Candidate not found")

    await _cache.invalidate(_cache.candidate_profile_key(candidate_id))
    counts = await _candidate_counts(pool, candidate_id)
    return _row_to_admin_candidate({**dict(row), **counts})


@app.delete("/admin/candidates/{candidate_id}")
async def admin_delete_candidate(candidate_id: str):
    """Delete a candidate and cascading documents/chunks."""
    pool = app.state.pool
    row = await pool.fetchrow(
        "DELETE FROM candidates WHERE id = $1 RETURNING id, full_name",
        candidate_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Candidate not found")

    await _cache.invalidate(_cache.candidate_profile_key(candidate_id))
    return {
        "deleted": True,
        "id": str(row["id"]),
        "full_name": row["full_name"],
    }


@app.get("/admin/documents")
async def admin_list_documents(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    candidate_id: Optional[str] = None,
    doc_type: Optional[Literal["transcript", "certification", "resume", "bio", "cover_letter", "other"]] = None,
    q: Optional[str] = None,
):
    """List documents without returning full raw text by default."""
    pool = app.state.pool
    where_sql, params = _document_list_filters(
        candidate_id=candidate_id,
        doc_type=doc_type,
        q=q,
    )

    total = await pool.fetchval(
        f"""
        SELECT COUNT(*)
        FROM candidate_documents cd
        JOIN candidates c ON c.id = cd.candidate_id
        {where_sql}
        """,
        *params,
    )

    limit_param = len(params) + 1
    offset_param = len(params) + 2
    rows = await pool.fetch(
        f"""
        SELECT
            cd.id,
            cd.candidate_id,
            c.full_name AS candidate_name,
            cd.doc_type,
            cd.title,
            LEFT(cd.raw_text, 420) AS raw_text_preview,
            LENGTH(cd.raw_text)::int AS char_count,
            COALESCE(chunk_counts.chunk_count, 0)::int AS chunk_count,
            cd.created_at
        FROM candidate_documents cd
        JOIN candidates c ON c.id = cd.candidate_id
        LEFT JOIN (
            SELECT document_id, COUNT(*)::int AS chunk_count
            FROM document_chunks
            GROUP BY document_id
        ) chunk_counts ON chunk_counts.document_id = cd.id
        {where_sql}
        ORDER BY cd.created_at DESC
        LIMIT ${limit_param} OFFSET ${offset_param}
        """,
        *params,
        limit,
        offset,
    )

    return {
        "items": [_row_to_admin_document(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/admin/documents/{document_id}")
async def admin_get_document(document_id: str):
    """Fetch one document with full raw text for inspection/editing workflows."""
    pool = app.state.pool
    row = await pool.fetchrow(
        """
        SELECT
            cd.*,
            c.full_name AS candidate_name,
            COALESCE(chunk_counts.chunk_count, 0)::int AS chunk_count
        FROM candidate_documents cd
        JOIN candidates c ON c.id = cd.candidate_id
        LEFT JOIN (
            SELECT document_id, COUNT(*)::int AS chunk_count
            FROM document_chunks
            GROUP BY document_id
        ) chunk_counts ON chunk_counts.document_id = cd.id
        WHERE cd.id = $1
        """,
        document_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Document not found")

    return {
        "id": str(row["id"]),
        "candidate_id": str(row["candidate_id"]),
        "candidate_name": row["candidate_name"],
        "doc_type": row["doc_type"],
        "title": row["title"],
        "raw_text": row["raw_text"],
        "char_count": len(row["raw_text"]),
        "chunk_count": row["chunk_count"],
        "created_at": _json_datetime(row["created_at"]),
    }


@app.delete("/admin/documents/{document_id}")
async def admin_delete_document(document_id: str):
    """Delete a document and its chunks."""
    pool = app.state.pool
    row = await pool.fetchrow(
        "SELECT id, title FROM candidate_documents WHERE id = $1",
        document_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Document not found")

    pipeline: IngestionPipeline = app.state.ingestion
    chunk_count = await pipeline.delete_document(document_id)
    return {
        "deleted": True,
        "id": str(row["id"]),
        "title": row["title"],
        "chunks_deleted": chunk_count,
    }


@app.get("/admin/chunks")
async def admin_list_chunks(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    candidate_id: Optional[str] = None,
    document_id: Optional[str] = None,
    q: Optional[str] = None,
):
    """List chunk previews without returning embeddings."""
    pool = app.state.pool
    where_sql, params = _chunk_list_filters(
        candidate_id=candidate_id,
        document_id=document_id,
        q=q,
    )

    total = await pool.fetchval(
        f"""
        SELECT COUNT(*)
        FROM document_chunks dc
        JOIN candidates c ON c.id = dc.candidate_id
        JOIN candidate_documents cd ON cd.id = dc.document_id
        {where_sql}
        """,
        *params,
    )

    limit_param = len(params) + 1
    offset_param = len(params) + 2
    rows = await pool.fetch(
        f"""
        SELECT
            dc.id,
            dc.candidate_id,
            c.full_name AS candidate_name,
            dc.document_id,
            cd.doc_type,
            cd.title AS document_title,
            dc.chunk_index,
            LEFT(dc.content, 420) AS content_preview,
            dc.token_count,
            dc.created_at
        FROM document_chunks dc
        JOIN candidates c ON c.id = dc.candidate_id
        JOIN candidate_documents cd ON cd.id = dc.document_id
        {where_sql}
        ORDER BY dc.created_at DESC, dc.chunk_index ASC
        LIMIT ${limit_param} OFFSET ${offset_param}
        """,
        *params,
        limit,
        offset,
    )

    return {
        "items": [_row_to_admin_chunk(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# ══════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════

def _model_settings_payload(app_obj: FastAPI) -> dict[str, Any]:
    _ensure_model_state(app_obj)
    active_embedding = {
        "provider": settings.embedding_provider,
        "model": settings.embedding_model,
        "dimensions": settings.embedding_dimensions,
    }
    pending_embedding = _pending_embedding_payload(active_embedding)
    return {
        "embedding": {
            "active": active_embedding,
            "pending": pending_embedding,
        },
        "reranker": {
            "model": settings.reranker_model,
            "cached": settings.reranker_model in app_obj.state.reranker_cache,
        },
        "features": {
            "use_personalization": settings.use_personalization,
        },
        "api_keys": _api_key_statuses(),
        "llm": {
            "provider": settings.llm_provider,
            "model":    settings.llm_model,
            "fast_provider":         settings.fast_llm_provider,
            "fast_model":            settings.fast_llm_model,
            "fast_thinking":         settings.fast_thinking_level,
            "quality_provider":      settings.quality_llm_provider,
            "quality_model":         settings.quality_llm_model,
            "quality_thinking":      settings.quality_thinking_level,
            "insights_provider":     settings.insights_llm_provider,
            "insights_model":        settings.insights_llm_model,
            "insights_thinking":     settings.insights_thinking_level,
        },
        "catalogs": {
            "embeddings": list_available_models(),
            "rerankers": {
                key: {"model": value[0], "description": value[1]}
                for key, value in RERANKER_CATALOG.items()
            },
            "llm_models": _llm_catalog_for_settings(),
        },
        "warmups": app_obj.state.model_warmups,
        "warnings": [
            "Embedding model changes are staged until document vectors are rebuilt.",
            "Local rerankers can be warmed for future re-rank experiments.",
        ],
    }


def _pending_embedding_payload(active_embedding: dict[str, Any]) -> dict[str, Any] | None:
    provider = settings.pending_embedding_provider
    model = settings.pending_embedding_model
    dimensions = settings.pending_embedding_dimensions
    if not any([provider, model, dimensions]):
        return None
    pending = {
        "provider": provider,
        "model": model,
        "dimensions": dimensions,
    }
    pending["requires_rebuild"] = any(
        pending.get(key) != active_embedding.get(key)
        for key in ["provider", "model", "dimensions"]
    )
    return pending


def _api_key_statuses() -> dict[str, dict[str, Any]]:
    statuses = {}
    for key_name in API_KEY_ENV_FIELDS:
        value = getattr(settings, key_name, "")
        statuses[key_name] = {
            "configured": _secret_is_configured(value),
            "masked": _mask_secret(value),
        }
    return statuses


def _llm_catalog_for_settings() -> dict[str, dict[str, dict[str, Any]]]:
    """Return live supported provider models, falling back to curated models."""
    catalog = list_llm_models()
    for provider in ("gemini", "groq"):
        catalog[provider] = _llm_models_for_provider(provider)
    return catalog


def _llm_models_for_provider(provider: str) -> dict[str, dict[str, Any]]:
    if provider == "gemini":
        available = _available_gemini_llm_models()
    elif provider == "groq":
        available = _available_groq_llm_models()
    else:
        available = {}
    return available or dict(list_llm_models().get(provider, {}))


def _available_gemini_llm_models() -> dict[str, dict[str, Any]]:
    if not _secret_is_configured(settings.google_api_key):
        return {}
    try:
        import httpx

        response = httpx.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": settings.google_api_key},
            timeout=5.0,
        )
        response.raise_for_status()
        models: dict[str, dict[str, Any]] = {}
        for item in response.json().get("models", []):
            methods = item.get("supportedGenerationMethods") or item.get("supported_actions") or []
            model_id = str(item.get("name") or "").removeprefix("models/")
            if "generateContent" not in methods or not _is_gemini_llm_model(model_id):
                continue
            models[model_id] = {
                "description": "Available via configured Gemini API key.",
                "supports_thinking": bool(item.get("thinking")) and gemini_model_supports_thinking(model_id),
            }
        return models
    except Exception as exc:
        logger.warning("Gemini model listing failed: %s", exc)
        return {}


def _available_groq_llm_models() -> dict[str, dict[str, Any]]:
    if not _secret_is_configured(settings.groq_api_key):
        return {}
    try:
        import httpx

        response = httpx.get(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {settings.groq_api_key}"},
            timeout=5.0,
        )
        response.raise_for_status()
        models: dict[str, dict[str, Any]] = {}
        for item in response.json().get("data", []):
            model_id = str(item.get("id") or "")
            if not item.get("active", True) or not _is_groq_llm_model(model_id):
                continue
            bits = ["Available via configured Groq API key."]
            if item.get("context_window"):
                bits.append(f"Context: {item['context_window']}.")
            if item.get("max_completion_tokens"):
                bits.append(f"Max output: {item['max_completion_tokens']}.")
            models[model_id] = {"description": " ".join(bits)}
        return models
    except Exception as exc:
        logger.warning("Groq model listing failed: %s", exc)
        return {}


def _is_gemini_llm_model(model_id: str) -> bool:
    blocked = (
        "embedding",
        "imagen",
        "veo",
        "audio",
        "tts",
        "image",
        "robotics",
        "computer-use",
        "antigravity",
        "deep-research",
        "nano-banana",
        "lyria",
    )
    return bool(model_id) and not any(part in model_id for part in blocked)


def _is_groq_llm_model(model_id: str) -> bool:
    blocked = ("whisper", "prompt-guard", "orpheus", "safeguard")
    return bool(model_id) and not any(part in model_id for part in blocked)


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


def _secret_is_configured(value: str) -> bool:
    clean = value.strip()
    return bool(clean) and not clean.endswith("...")


def _validate_model_settings_update(req: ModelSettingsUpdate) -> None:
    embedding_catalog = list_available_models()
    if req.pending_embedding_provider:
        if req.pending_embedding_provider not in embedding_catalog:
            raise HTTPException(status_code=400, detail="Unknown embedding provider")
        if req.pending_embedding_model:
            provider_models = embedding_catalog[req.pending_embedding_provider]
            if req.pending_embedding_model not in provider_models:
                raise HTTPException(status_code=400, detail="Unknown embedding model for provider")
    if req.reranker_model and req.reranker_model not in RERANKER_CATALOG:
        raise HTTPException(status_code=400, detail="Unknown reranker model")

    llm_catalog = _llm_catalog_for_settings()
    for prov_field, model_field in (
        ("llm_provider",          "llm_model"),
        ("fast_llm_provider",     "fast_llm_model"),
        ("quality_llm_provider",  "quality_llm_model"),
        ("insights_llm_provider", "insights_llm_model"),
    ):
        prov  = getattr(req, prov_field)
        model = getattr(req, model_field)
        if prov and prov not in llm_catalog:
            raise HTTPException(status_code=400, detail=f"Unknown LLM provider: {prov}")
        if model and prov and model not in llm_catalog.get(prov, {}):
            raise HTTPException(status_code=400, detail=f"Unknown LLM model {model!r} for provider {prov!r}")

    valid_thinking = {"low", "medium", "high", ""}
    for field in ("fast_thinking_level", "quality_thinking_level", "insights_thinking_level"):
        value = getattr(req, field)
        if value is not None and value.lower() not in valid_thinking:
            raise HTTPException(status_code=400, detail=f"{field} must be one of low/medium/high")

    for key_name in req.api_keys:
        if key_name not in API_KEY_ENV_FIELDS:
            raise HTTPException(status_code=400, detail=f"Unknown API key field: {key_name}")


def _write_env_values(path: Path, updates: Mapping[str, str]) -> None:
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    used_keys: set[str] = set()
    lines: list[str] = []

    for line in existing_lines:
        if "=" not in line or line.lstrip().startswith("#"):
            lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            lines.append(f"{key}={updates[key]}")
            used_keys.add(key)
        else:
            lines.append(line)

    for key, value in updates.items():
        if key not in used_keys:
            lines.append(f"{key}={value}")

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _ensure_model_state(app_obj: FastAPI) -> None:
    if not hasattr(app_obj.state, "reranker_cache"):
        app_obj.state.reranker_cache = {}
    if not hasattr(app_obj.state, "model_warmups"):
        app_obj.state.model_warmups = {}


def _get_cached_reranker(app_obj: FastAPI, choice: str):
    _ensure_model_state(app_obj)
    if choice not in app_obj.state.reranker_cache:
        app_obj.state.reranker_cache[choice] = get_reranker(choice)
    return app_obj.state.reranker_cache[choice]


async def _warmup_db_pool(app_obj: FastAPI, pool: Any, *, label: str) -> dict[str, Any]:
    _ensure_model_state(app_obj)
    target = min(4, max(1, int(settings.db_pool_max or 4)))
    start = time.perf_counter()

    async def _ping() -> None:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")

    await asyncio.gather(*(_ping() for _ in range(target)))
    status = {
        "status": "ok",
        "connections_touched": target,
        "latency_ms": round((time.perf_counter() - start) * 1000, 2),
    }
    app_obj.state.model_warmups[f"db_pool_{label}"] = status
    logger.info(
        "%s DB pool warmup completed (%d connections, %.1f ms)",
        label,
        target,
        status["latency_ms"],
    )
    return status


async def _warmup_active_embedder(app_obj: FastAPI) -> dict[str, Any]:
    start = time.perf_counter()
    if settings.embedding_provider != "local":
        status = {
            "status": "skipped",
            "reason": "Active embedding provider is remote; warmup avoids embedding API calls.",
            "provider": settings.embedding_provider,
            "model": settings.embedding_model,
        }
        app_obj.state.model_warmups["embedder"] = status
        return status
    try:
        engine = getattr(app_obj.state, "search_engine", None)
        embedder = engine.embedder if engine is not None else get_embedder()
        await embedder.embed(["warmup"])
        status = {
            "status": "ok",
            "provider": settings.embedding_provider,
            "model": settings.embedding_model,
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
        }
    except Exception as exc:
        status = {
            "status": "error",
            "provider": settings.embedding_provider,
            "model": settings.embedding_model,
            "error": str(exc),
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
        }
    app_obj.state.model_warmups["embedder"] = status
    return status


async def _warmup_reranker(app_obj: FastAPI, choice: str) -> dict[str, Any]:
    start = time.perf_counter()
    try:
        reranker = _get_cached_reranker(app_obj, choice)
        await reranker.warmup()
        status = {
            "status": "ok",
            "model": choice,
            "cached": True,
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
        }
    except Exception as exc:
        status = {
            "status": "error",
            "model": choice,
            "error": str(exc),
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
        }
    app_obj.state.model_warmups["reranker"] = status
    return status


async def _validate_llm_model(
    provider: str | None = None,
    model: str | None = None,
    thinking_level: str | None = None,
) -> dict[str, Any]:
    start = time.perf_counter()
    provider = provider or settings.llm_provider
    model = model or settings.llm_model
    if provider not in list_llm_models():
        raise HTTPException(status_code=400, detail=f"Unknown LLM provider: {provider}")
    llm_catalog = {provider: _llm_models_for_provider(provider)}
    model_info = llm_catalog.get(provider, {}).get(model, {}) if model else {}
    if model and model not in llm_catalog.get(provider, {}):
        raise HTTPException(status_code=400, detail=f"Unknown LLM model {model!r} for provider {provider!r}")
    if not model:
        from pipeline.llm_models import default_llm_model_for_provider

        model = default_llm_model_for_provider(provider)
    try:
        if provider == "gemini":
            if not _secret_is_configured(settings.google_api_key):
                raise RuntimeError("Gemini API key is not configured")
            text = _call_gemini_generate_content_rest(
                api_key=settings.google_api_key,
                model=model,
                prompt="Return OK.",
                temperature=0.0,
                max_output_tokens=32,
                thinking_level=thinking_level if model_info.get("supports_thinking") is True else None,
            )
            ok = bool(text)
        elif provider in {"openai", "groq", "deepseek"}:
            # All three are OpenAI-compatible; differ only by key + base_url.
            _compat = {
                "openai":   (settings.openai_api_key, None, "OpenAI"),
                "groq":     (settings.groq_api_key, "https://api.groq.com/openai/v1", "Groq"),
                "deepseek": (getattr(settings, "deepseek_api_key", "") or "", "https://api.deepseek.com", "DeepSeek"),
            }
            api_key, base_url, label = _compat[provider]
            if not _secret_is_configured(api_key):
                raise RuntimeError(f"{label} API key is not configured")
            from openai import AsyncOpenAI

            client_kwargs: dict[str, Any] = {"api_key": api_key}
            if base_url:
                client_kwargs["base_url"] = base_url
            client = AsyncOpenAI(**client_kwargs)
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Return OK."}],
                temperature=0.0,
                max_tokens=8,
            )
            ok = bool(response.choices[0].message.content)
        else:
            raise RuntimeError(f"Unsupported LLM provider: {provider}")

        return {
            "status": "ok" if ok else "error",
            "provider": provider,
            "model": model,
            "thinking_level": thinking_level,
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
            "note": "Remote LLM validation includes network latency and may incur token cost.",
        }
    except Exception as exc:
        return {
            "status": "error",
            "provider": provider,
            "model": model,
            "thinking_level": thinking_level,
            "error": str(exc),
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
            "note": "Remote LLM validation includes network latency and may incur token cost.",
        }


def _call_gemini_generate_content_rest(
    *,
    api_key: str,
    model: str,
    prompt: str,
    temperature: float,
    max_output_tokens: int,
    thinking_level: str | None = None,
) -> str:
    """Tiny Gemini REST call used by settings validation.

    The google-genai SDK can surface opaque runtime messages for validation
    failures. The REST response has stable JSON error payloads, which makes the
    settings page much more useful when a key or model is wrong.
    """
    import httpx

    model_id = model.removeprefix("models/")
    generation_config: dict[str, Any] = {
        "temperature": temperature,
        "maxOutputTokens": max_output_tokens,
    }
    if thinking_level:
        generation_config["thinkingConfig"] = {"thinkingLevel": thinking_level.upper()}

    response = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent",
        params={"key": api_key},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": generation_config,
        },
        timeout=15.0,
    )
    try:
        response.raise_for_status()
    except Exception as exc:
        raise RuntimeError(_gemini_error_message(response)) from exc

    payload = response.json()
    text = _gemini_text_from_response(payload)
    if text:
        return text
    raise RuntimeError(_gemini_empty_response_message(payload))


def _gemini_text_from_response(payload: Mapping[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        return ""
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    return "".join(
        part.get("text", "")
        for part in parts
        if isinstance(part, Mapping) and isinstance(part.get("text"), str)
    )


def _gemini_error_message(response: Any) -> str:
    try:
        payload = response.json()
    except Exception:
        text = getattr(response, "text", "")
        return text or "Gemini validation request failed"
    error = payload.get("error") if isinstance(payload, Mapping) else None
    if isinstance(error, Mapping):
        message = error.get("message")
        status = error.get("status")
        if message and status:
            return f"{status}: {message}"
        if message:
            return str(message)
    return "Gemini validation request failed"


def _gemini_empty_response_message(payload: Mapping[str, Any]) -> str:
    prompt_feedback = payload.get("promptFeedback")
    if isinstance(prompt_feedback, Mapping) and prompt_feedback.get("blockReason"):
        return f"Gemini response blocked: {prompt_feedback['blockReason']}"
    candidates = payload.get("candidates") or []
    if candidates and isinstance(candidates[0], Mapping):
        finish_reason = candidates[0].get("finishReason") or candidates[0].get("finish_reason")
        if finish_reason:
            return f"Gemini returned no text (finish reason: {finish_reason})"
    return "Gemini returned no text"


def _build_filters(req: SearchRequest) -> SearchFilters:
    return SearchFilters(
        location=req.location,
        country=req.country,
        city=req.city,
        min_age=req.min_age,
        max_age=req.max_age,
        skills=req.skills,
        interests=req.interests,
        min_years_exp=req.min_years_exp,
        max_years_exp=req.max_years_exp,
        min_salary=req.min_salary,
        max_salary=req.max_salary,
        skills_match=req.skills_match,
        interests_match=req.interests_match,
    )


def _filters_dict(req: SearchRequest) -> dict:
    return req.model_dump(
        exclude={"query", "top_k", "use_rrf", "include_rank_explanation", "include_fit_analysis", "include_ai_insights", "max_chunks_per_candidate",
                 "enable_reranking", "reranker", "llm_provider", "llm_model"},
        exclude_none=True,
    )


def _insights_cache_key(req: SearchRequest) -> str:
    """Stable cache key based only on fields that affect which candidates come back.

    Deliberately excludes top_k, include_ai_insights, include_rank_explanation,
    session_id, and llm_provider/model. Recruiter/config fields stay in the key
    because personalization and overrides can change ranking and candidates.
    """
    from pipeline import cache as _cache
    stable = req.model_dump(include={
        "query", "jd", "mode", "country", "city",
        "min_years_exp", "max_years_exp", "min_salary", "max_salary",
        "skills", "should", "skill_weights", "status",
        "skills_match", "interests_match", "keyword_policy", "keyword_timeout_ms",
        "use_rrf", "enable_reranking", "reranker", "max_chunks_per_candidate",
        "recruiter_id", "config_overrides",
    }, exclude_none=True)
    import json as _json
    return _cache.insights_key(_json.dumps(stable, sort_keys=True))


def _insight_inputs_to_cache_payload(items: list[CandidateInsightInput]) -> list[dict[str, Any]]:
    return [
        {
            "candidate_id": item.candidate_id,
            "full_name": item.full_name,
            "city": item.city,
            "country": item.country,
            "salary_min": item.salary_min,
            "salary_max": item.salary_max,
            "years_exp": item.years_exp,
            "skills": list(item.skills or []),
            "best_chunk": item.best_chunk,
            "similarity_score": item.similarity_score,
            "doc_type": item.doc_type,
            "document_title": item.document_title,
            "supporting_chunks": list(item.supporting_chunks or []),
            "rerank_score": item.rerank_score,
            "rank_score": item.rank_score,
        }
        for item in items
    ]


def _insight_inputs_from_cache_payload(payload: list[dict[str, Any]]) -> list[CandidateInsightInput]:
    return [CandidateInsightInput(**item) for item in payload]


async def _candidate_inputs_for_insights(req: SearchRequest, top_k: int):
    ckey = _insights_cache_key(req)
    cached = await _cache.get(ckey)

    if cached is not None:
        return _insight_inputs_from_cache_payload(cached[:top_k])

    # Cache cold (standalone /search/insights call with no prior /search)
    engine: HybridSearchEngine = app.state.search_engine
    filters = _build_filters(req)
    query_text = req.query.strip()

    if query_text:
        raw_results = await engine.search(
            query=query_text, filters=filters, top_k=top_k,
            use_rrf=req.use_rrf, max_chunks_per_candidate=req.max_chunks_per_candidate,
        )
        if req.enable_reranking:
            raw_results = await _apply_optional_reranker(req, query_text, raw_results)
    else:
        raw_results = await _filter_only_search(engine, filters, top_k)

    insight_inputs = [candidate_insight_input_from_result(r) for r in raw_results]
    await _cache.set(ckey, _insight_inputs_to_cache_payload(insight_inputs))
    return insight_inputs[:top_k]


def _to_ai_insight_response(
    insight: PipelineAIInsightResult,
    timings_ms: dict[str, float],
) -> AIInsightResponse:
    return AIInsightResponse(
        status=insight.status,
        provider=insight.provider,
        model=insight.model,
        best_candidate_id=insight.best_candidate_id,
        summary=insight.summary,
        comparative_reasoning=insight.comparative_reasoning,
        matrix=[
            AIInsightMatrixRow(
                candidate_id=row.candidate_id,
                full_name=row.full_name,
                fit_score=row.fit_score,
                recommendation=row.recommendation,
                strengths=row.strengths,
                evidence=row.evidence,
                gaps=row.gaps,
                interview_probe=row.interview_probe,
                grounding_notes=row.grounding_notes,
            )
            for row in insight.matrix
        ],
        timings_ms=timings_ms,
        error=insight.error,
    )


def _to_candidate_result(
    r: SearchResult,
    rank_position: int | None = None,
    spec: Any = None,
) -> CandidateResult:
    return CandidateResult(
        candidate_id=r.candidate_id,
        impression_id=getattr(r, "impression_id", None),
        full_name=r.full_name,
        email=r.email,
        city=r.city,
        country=r.country,
        salary_min=r.salary_min,
        salary_max=r.salary_max,
        years_exp=r.years_exp,
        skills=r.skills,
        best_chunk=r.best_chunk,
        similarity_score=round(r.similarity_score, 4),
        doc_type=r.doc_type,
        document_title=r.document_title,
        supporting_chunks=r.supporting_chunks,
        rerank_score=round(r.rerank_score, 4) if r.rerank_score is not None else None,
        rrf_score=round(r.fused_rrf_score, 6) if r.fused_rrf_score else None,
        rank_score=round(r.feature_score, 4),
        ranking_explanation=(
            getattr(r, "explanation", None) or getattr(r, "ranking_explanation", None) or build_ranking_explanation(r, rank_position, spec=spec)
            if rank_position is not None
            else None
        ),
    )


def _should_include_ranking_explanation(req: SearchRequest) -> bool:
    if "include_rank_explanation" in req.model_fields_set:
        return req.include_rank_explanation
    if req.include_fit_analysis is not None:
        return req.include_fit_analysis
    return req.include_rank_explanation


async def _apply_optional_reranker(
    req: SearchRequest,
    query_text: str,
    results: list[SearchResult],
) -> list[SearchResult]:
    if not results:
        return results
    choice = req.reranker or settings.reranker_model
    if choice not in RERANKER_CATALOG:
        raise HTTPException(status_code=400, detail=f"Unknown reranker: {choice}")
    reranker = _get_cached_reranker(app, choice)
    return await reranker.rerank(query_text, results)


def _attach_ranking_explanations(
    results: list[CandidateResult],
) -> None:
    for index, result in enumerate(results, start=1):
        result.ranking_explanation = build_filter_only_ranking_explanation(result, index)


async def _filter_only_search(
    engine: HybridSearchEngine,
    filters: SearchFilters,
    top_k: int,
) -> list[CandidateResult]:
    """Return candidates that match structured filters without embedding a query."""
    where_sql, params = engine._build_where_clause(filters)

    doc_type_filter = ""
    if filters.doc_types:
        params.append(filters.doc_types)
        doc_type_param = len(params)
        doc_type_filter = f"AND cd.doc_type = ANY(${doc_type_param}::text[])"
        where_sql = (
            f"{where_sql} AND EXISTS ("
            "SELECT 1 FROM candidate_documents cd "
            f"WHERE cd.candidate_id = c.id AND cd.doc_type = ANY(${doc_type_param}::text[])"
            ")"
        )

    limit_param = len(params) + 1
    rows = await engine.pool.fetch(
        f"""
        SELECT
            c.id AS candidate_id,
            c.full_name,
            c.email,
            c.city,
            c.country,
            c.salary_min,
            c.salary_max,
            c.years_exp,
            c.skills,
            COALESCE(chunk.content, '') AS best_chunk,
            cd.doc_type,
            cd.title AS document_title
        FROM candidates c
        LEFT JOIN LATERAL (
            SELECT cd.id, cd.doc_type, cd.title, cd.created_at
            FROM candidate_documents cd
            WHERE cd.candidate_id = c.id {doc_type_filter}
            ORDER BY cd.created_at DESC
            LIMIT 1
        ) cd ON TRUE
        LEFT JOIN LATERAL (
            SELECT dc.content
            FROM document_chunks dc
            WHERE dc.document_id = cd.id
            ORDER BY dc.chunk_index ASC
            LIMIT 1
        ) chunk ON TRUE
        WHERE {where_sql}
        ORDER BY c.updated_at DESC, c.created_at DESC
        LIMIT ${limit_param}
        """,
        *params,
        top_k,
    )
    return [_row_to_filter_candidate_result(row) for row in rows]


def _row_to_filter_candidate_result(row: Mapping[str, Any]) -> CandidateResult:
    data = dict(row)
    return CandidateResult(
        candidate_id=str(data["candidate_id"]),
        full_name=data["full_name"],
        email=data.get("email"),
        city=data.get("city"),
        country=data.get("country"),
        salary_min=data.get("salary_min"),
        salary_max=data.get("salary_max"),
        years_exp=data.get("years_exp"),
        skills=data.get("skills") or [],
        best_chunk=data.get("best_chunk") or "No document chunks available for this candidate.",
        similarity_score=0,
        doc_type=data.get("doc_type") or "candidate",
        document_title=data.get("document_title"),
        supporting_chunks=[],
        rank_score=0,
    )


def _option_values(rows: list[Mapping[str, Any]]) -> list[str]:
    return [row["value"] for row in rows if row["value"]]


def _skill_option_values(rows: list[Mapping[str, Any]]) -> list[str]:
    return sorted(clean_skill_list([str(row["value"]) for row in rows if row["value"]]))


def _candidate_list_filters(
    q: Optional[str],
    status: Optional[str],
    name_email: Optional[str] = None,
    city: Optional[str] = None,
    country: Optional[str] = None,
    skills: Optional[list[str]] = None,
) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []

    clean_q = q.strip() if q else ""
    if clean_q:
        params.append(f"%{clean_q}%")
        placeholder = f"${len(params)}"
        conditions.append(
            "("
            f"c.full_name ILIKE {placeholder} OR "
            f"c.email ILIKE {placeholder} OR "
            f"c.city ILIKE {placeholder} OR "
            f"c.country ILIKE {placeholder} OR "
            f"array_to_string(c.skills, ', ') ILIKE {placeholder}"
            ")"
        )

    clean_name_email = name_email.strip() if name_email else ""
    if clean_name_email:
        params.append(f"%{clean_name_email}%")
        placeholder = f"${len(params)}"
        conditions.append(
            "("
            f"c.full_name ILIKE {placeholder} OR "
            f"c.email ILIKE {placeholder}"
            ")"
        )

    clean_city = city.strip() if city else ""
    if clean_city:
        params.append(f"%{clean_city}%")
        conditions.append(f"c.city ILIKE ${len(params)}")

    clean_country = country.strip() if country else ""
    if clean_country:
        params.append(f"%{clean_country}%")
        conditions.append(f"c.country ILIKE ${len(params)}")

    if skills:
        params.append(skills)
        conditions.append(f"c.skills && ${len(params)}::text[]")

    if status:
        params.append(status)
        conditions.append(f"c.status = ${len(params)}")

    if not conditions:
        return "", params
    return f"WHERE {' AND '.join(conditions)}", params


def _split_csv(value: Optional[str]) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _document_list_filters(
    candidate_id: Optional[str],
    doc_type: Optional[str],
    q: Optional[str],
) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []

    if candidate_id:
        params.append(candidate_id)
        conditions.append(f"cd.candidate_id = ${len(params)}")

    if doc_type:
        params.append(doc_type)
        conditions.append(f"cd.doc_type = ${len(params)}")

    clean_q = q.strip() if q else ""
    if clean_q:
        params.append(f"%{clean_q}%")
        placeholder = f"${len(params)}"
        conditions.append(
            "("
            f"cd.title ILIKE {placeholder} OR "
            f"cd.raw_text ILIKE {placeholder} OR "
            f"c.full_name ILIKE {placeholder}"
            ")"
        )

    if not conditions:
        return "", params
    return f"WHERE {' AND '.join(conditions)}", params


def _chunk_list_filters(
    candidate_id: Optional[str],
    document_id: Optional[str],
    q: Optional[str],
) -> tuple[str, list[Any]]:
    conditions: list[str] = []
    params: list[Any] = []

    if candidate_id:
        params.append(candidate_id)
        conditions.append(f"dc.candidate_id = ${len(params)}")

    if document_id:
        params.append(document_id)
        conditions.append(f"dc.document_id = ${len(params)}")

    clean_q = q.strip() if q else ""
    if clean_q:
        params.append(f"%{clean_q}%")
        placeholder = f"${len(params)}"
        conditions.append(
            "("
            f"dc.content ILIKE {placeholder} OR "
            f"cd.title ILIKE {placeholder} OR "
            f"cd.doc_type ILIKE {placeholder} OR "
            f"c.full_name ILIKE {placeholder}"
            ")"
        )

    if not conditions:
        return "", params
    return f"WHERE {' AND '.join(conditions)}", params


def _build_candidate_update(candidate_id: str, req: CandidateUpdate) -> tuple[str, list[Any]]:
    values = req.model_dump(exclude_unset=True)
    if not values:
        raise HTTPException(status_code=400, detail="No candidate fields supplied")

    if "full_name" in values and values["full_name"] is None:
        raise HTTPException(status_code=400, detail="full_name cannot be null")

    assignments: list[str] = []
    params: list[Any] = []
    for field, value in values.items():
        params.append(value)
        assignments.append(f"{field} = ${len(params)}")

    params.append(candidate_id)
    sql = f"""
        UPDATE candidates
        SET {', '.join(assignments)}
        WHERE id = ${len(params)}
        RETURNING *
    """
    return sql, params


async def _candidate_counts(pool: Any, candidate_id: str) -> dict[str, int]:
    row = await pool.fetchrow(
        """
        SELECT
            (SELECT COUNT(*)::int FROM candidate_documents WHERE candidate_id = $1)
                AS document_count,
            (SELECT COUNT(*)::int FROM document_chunks WHERE candidate_id = $1)
                AS chunk_count
        """,
        candidate_id,
    )
    return dict(row)


def _row_to_admin_candidate(row: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": str(data["id"]),
        "full_name": data["full_name"],
        "email": data["email"],
        "age": data["age"],
        "location": data["location"],
        "city": data["city"],
        "country": data["country"],
        "interests": data["interests"] or [],
        "skills": data["skills"] or [],
        "years_exp": data["years_exp"],
        "salary_min": data["salary_min"],
        "salary_max": data["salary_max"],
        "status": data["status"],
        "document_count": data.get("document_count", 0),
        "chunk_count": data.get("chunk_count", 0),
        "created_at": _json_datetime(data["created_at"]),
        "updated_at": _json_datetime(data["updated_at"]),
    }


def _row_to_admin_document(row: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": str(data["id"]),
        "candidate_id": str(data["candidate_id"]),
        "candidate_name": data["candidate_name"],
        "doc_type": data["doc_type"],
        "title": data["title"],
        "preview": _preview(data["raw_text_preview"]),
        "char_count": data["char_count"],
        "chunk_count": data["chunk_count"],
        "created_at": _json_datetime(data["created_at"]),
    }


def _row_to_admin_chunk(row: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(row)
    return {
        "id": str(data["id"]),
        "candidate_id": str(data["candidate_id"]),
        "candidate_name": data["candidate_name"],
        "document_id": str(data["document_id"]),
        "doc_type": data["doc_type"],
        "document_title": data["document_title"],
        "chunk_index": data["chunk_index"],
        "preview": _preview(data["content_preview"]),
        "token_count": data["token_count"],
        "created_at": _json_datetime(data["created_at"]),
    }


def _preview(value: Optional[str], max_chars: int = 320) -> str:
    return " ".join((value or "").split())[:max_chars]


def _json_datetime(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


async def _ensure_agent_sessions_table(pool: Any) -> None:
    await pool.execute(
        """
        CREATE TABLE IF NOT EXISTS agent_chat_sessions (
            recruiter_id uuid NOT NULL,
            session_id text NOT NULL,
            title text NOT NULL DEFAULT 'Untitled session',
            summary text NOT NULL DEFAULT '',
            messages_json jsonb NOT NULL DEFAULT '[]'::jsonb,
            context_json jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            ended_at timestamptz,
            PRIMARY KEY (recruiter_id, session_id)
        )
        """
    )
    await pool.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_agent_chat_sessions_recruiter_updated
        ON agent_chat_sessions (recruiter_id, updated_at DESC)
        """
    )


class AgentSessionSaveRequest(BaseModel):
    recruiter_id: str
    title: Optional[str] = None
    summary: Optional[str] = None
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    context: Dict[str, Any] = Field(default_factory=dict)
    ended: bool = False


class AgentSessionSummaryRequest(BaseModel):
    recruiter_id: str
    messages: List[Dict[str, Any]] = Field(default_factory=list)
    context: Dict[str, Any] = Field(default_factory=dict)
    model: Optional[str] = None


class TalentQueryDbRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=4000)


def _session_title_from_messages(messages: list[dict[str, Any]]) -> str:
    for msg in messages:
        if msg.get("role") == "user" and msg.get("content"):
            text = " ".join(str(msg["content"]).split())
            return text[:52] + ("..." if len(text) > 52 else "")
    return "Talent search session"


def _coerce_jsonb_value(value: Any, default: Any) -> Any:
    """Normalize asyncpg JSON/JSONB values across driver codecs.

    Some local asyncpg setups return jsonb as decoded Python values, others as
    strings. Session restore needs a stable shape either way.
    """
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value


def _coerce_session_messages(value: Any) -> list[dict[str, Any]]:
    parsed = _coerce_jsonb_value(value, [])
    if isinstance(parsed, dict):
        parsed = parsed.get("messages") or parsed.get("items") or []
    if not isinstance(parsed, list):
        return []
    return [msg for msg in parsed if isinstance(msg, dict)]


def _coerce_session_context(value: Any) -> dict[str, Any]:
    parsed = _coerce_jsonb_value(value, {})
    return parsed if isinstance(parsed, dict) else {}


def _pydantic_history_from_session_messages(messages: Any) -> list[Any]:
    """Hydrate PydanticAI history from the persisted Talent UI transcript.

    The UI stores plain user/assistant rows for durability. If the server loses
    its in-memory PydanticAI session, this rebuilds enough model history for a
    continued session to behave like a continuation rather than a fresh chat.
    Tool-call internals stay in the UI workspace snapshot; model history only
    needs conversational user/assistant text.
    """
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

    history: list[Any] = []
    for msg in _coerce_session_messages(messages):
        role = msg.get("role")
        content = " ".join(str(msg.get("content") or "").split())
        if not content:
            continue
        if role == "user":
            history.append(ModelRequest(parts=[UserPromptPart(content=content)]))
        elif role == "assistant":
            history.append(ModelResponse(parts=[TextPart(content=content)]))
    return history[-32:]


def _session_summary_from_messages(messages: list[dict[str, Any]], context: dict[str, Any]) -> str:
    last_user = ""
    last_assistant = ""
    for msg in messages:
        content = " ".join(str(msg.get("content", "")).split())
        if not content:
            continue
        if msg.get("role") == "user":
            last_user = content
        elif msg.get("role") == "assistant":
            last_assistant = content
    parts: list[str] = []
    if last_user:
        parts.append("Asked for: " + last_user[:220])
    if last_assistant:
        parts.append("Last answer: " + last_assistant[:320])
    candidate_names = [
        str(c.get("name") or c.get("full_name") or c.get("id"))
        for c in (context.get("candidate_summaries") or [])
        if isinstance(c, dict)
    ]
    if candidate_names:
        parts.append("context candidates: " + ", ".join(candidate_names[:8]))
    if not parts:
        return ""
    return " ".join(parts)[:900]


def _normalise_summary_model(model: str | None) -> tuple[str, str]:
    provider = getattr(settings, "llm_provider", "deepseek") or "deepseek"
    model_name = getattr(settings, "llm_model", "") or "deepseek-chat"
    if model:
        if ":" in model:
            provider, model_name = model.split(":", 1)
        else:
            model_name = model
    if provider == "deepseek":
        aliases = {
            "deepseek-v4-flash": "deepseek-chat",
            "deepseek-v4-pro": "deepseek-chat",
            "deepseek-v3": "deepseek-chat",
            "deepseek-r1": "deepseek-reasoner",
        }
        model_name = aliases.get(model_name, model_name or "deepseek-chat")
    return provider, model_name


def _summary_llm_client_config(model: str | None = None) -> tuple[str, str, str | None, str]:
    provider, model_name = _normalise_summary_model(model)
    if provider == "deepseek":
        return (
            getattr(settings, "deepseek_api_key", "") or _os.environ.get("DEEPSEEK_API_KEY", ""),
            model_name or "deepseek-chat",
            "https://api.deepseek.com",
            "DeepSeek",
        )
    if provider == "groq":
        return (
            getattr(settings, "groq_api_key", "") or _os.environ.get("GROQ_API_KEY", ""),
            model_name or "llama-3.3-70b-versatile",
            "https://api.groq.com/openai/v1",
            "Groq",
        )
    if provider == "openai":
        return (
            getattr(settings, "openai_api_key", "") or _os.environ.get("OPENAI_API_KEY", ""),
            model_name or "gpt-4.1-nano",
            None,
            "OpenAI",
        )
    # Prefer DeepSeek for summaries when the active provider is not
    # OpenAI-compatible here (e.g. Gemini) but the project has a DeepSeek key.
    deepseek_key = getattr(settings, "deepseek_api_key", "") or _os.environ.get("DEEPSEEK_API_KEY", "")
    if deepseek_key:
        return deepseek_key, "deepseek-chat", "https://api.deepseek.com", "DeepSeek"
    return "", model_name, None, provider


async def _llm_summarize_agent_session(
    messages: list[dict[str, Any]],
    context: dict[str, Any],
    model: str | None = None,
) -> dict[str, str]:
    fallback = {
        "title": _session_title_from_messages(messages),
        "summary": _session_summary_from_messages(messages, context),
    }
    try:
        api_key, model_name, base_url, provider_label = _summary_llm_client_config(model)
        if not api_key:
            return fallback
        from pipeline.observability import (
            get_async_openai,
            start_span as _obs_start_span,
            update_current_span as _obs_update_current_span,
        )
        client_cls = get_async_openai()
        supports_langfuse_name = getattr(client_cls, "__module__", "").startswith("langfuse.")
        client = client_cls(api_key=api_key, base_url=base_url) if base_url else client_cls(api_key=api_key)
        payload = {
            "messages": messages[-24:],
            "context": context,
        }
        with _obs_start_span(
            "session.summarize",
            input={"messages_count": len(messages), "context_keys": sorted(context.keys())[:20]},
            metadata={"provider": provider_label, "model": model_name},
        ):
            create_kwargs = {
                "model": model_name,
                "temperature": 0,
                "max_tokens": 360,
                "response_format": {"type": "json_object"},
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Summarize a recruiting copilot session. Return JSON only: "
                            "{\"title\": short descriptive title under 48 chars, "
                            "\"summary\": compact context summary for continuing the search later}. "
                            f"Use {provider_label} to produce a concise product-quality history item, not a raw transcript."
                        ),
                    },
                    {"role": "user", "content": json.dumps(payload, default=str)},
                ],
            }
            if supports_langfuse_name:
                create_kwargs["name"] = "session.summarize.model"
            resp = await client.chat.completions.create(**create_kwargs)
            data = json.loads(_extract_json_object(resp.choices[0].message.content or "{}"))
            title = " ".join(str(data.get("title") or fallback["title"]).split())[:80]
            summary = " ".join(str(data.get("summary") or fallback["summary"]).split())[:2200]
            result = {"title": title or fallback["title"], "summary": summary}
            _obs_update_current_span(output={"title": result["title"], "summary_length": len(result["summary"])})
            return result
    except Exception as exc:
        logger.debug("agent session LLM summary failed: %s", exc)
        return fallback


async def _extract_upload_text(upload: UploadFile) -> str:
    name = upload.filename or "document.txt"
    suffix = Path(name).suffix.lower()
    data = await upload.read()
    if suffix in {"", ".txt", ".md", ".text"}:
        return data.decode("utf-8", errors="replace")
    if suffix == ".pdf":
        try:
            import io
            import pdfplumber
            with pdfplumber.open(io.BytesIO(data)) as pdf:
                return "\n\n".join((page.extract_text() or "") for page in pdf.pages)
        except ImportError as exc:
            raise HTTPException(status_code=415, detail="PDF upload requires pdfplumber") from exc
    if suffix == ".docx":
        try:
            import io
            from docx import Document
            document = Document(io.BytesIO(data))
            return "\n".join(p.text for p in document.paragraphs)
        except ImportError as exc:
            raise HTTPException(status_code=415, detail="DOCX upload requires python-docx") from exc
    raise HTTPException(status_code=415, detail="Supported uploads: .txt, .md, .pdf, .docx")


@app.post("/talent/api/candidates/batch")
async def talent_get_candidates_batch(req: CandidateBatchRequest):
    from pipeline.agent_tools import do_get_candidate_details

    result = await do_get_candidate_details(
        app.state.pool,
        [str(candidate_id) for candidate_id in (req.candidate_ids or [])],
        limit=req.limit,
    )
    return result


@app.get("/talent/api/candidates/{candidate_id}")
async def talent_get_candidate(candidate_id: str):
    pool = app.state.pool
    row = await pool.fetchrow(
        """
        SELECT c.*,
               COALESCE(doc_counts.document_count, 0)::int AS document_count,
               COALESCE(chunk_counts.chunk_count, 0)::int AS chunk_count
        FROM candidates c
        LEFT JOIN (
          SELECT candidate_id, COUNT(*)::int AS document_count
          FROM candidate_documents GROUP BY candidate_id
        ) doc_counts ON doc_counts.candidate_id = c.id
        LEFT JOIN (
          SELECT candidate_id, COUNT(*)::int AS chunk_count
          FROM document_chunks GROUP BY candidate_id
        ) chunk_counts ON chunk_counts.candidate_id = c.id
        WHERE c.id = $1::uuid
        """,
        candidate_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return _row_to_admin_candidate(row)


@app.get("/talent/api/candidates/{candidate_id}/documents")
async def talent_get_candidate_documents(candidate_id: str, limit: int = Query(default=40, ge=1, le=100)):
    pool = app.state.pool
    rows = await pool.fetch(
        """
        SELECT
            cd.id,
            cd.candidate_id,
            c.full_name AS candidate_name,
            cd.doc_type,
            cd.title,
            LEFT(cd.raw_text, 420) AS raw_text_preview,
            LENGTH(cd.raw_text)::int AS char_count,
            COALESCE(chunk_counts.chunk_count, 0)::int AS chunk_count,
            cd.created_at
        FROM candidate_documents cd
        JOIN candidates c ON c.id = cd.candidate_id
        LEFT JOIN (
            SELECT document_id, COUNT(*)::int AS chunk_count
            FROM document_chunks
            GROUP BY document_id
        ) chunk_counts ON chunk_counts.document_id = cd.id
        WHERE cd.candidate_id = $1::uuid
        ORDER BY cd.created_at DESC
        LIMIT $2
        """,
        candidate_id,
        limit,
    )
    return {"items": [_row_to_admin_document(row) for row in rows], "total": len(rows)}


@app.get("/talent/api/documents/{document_id}")
async def talent_get_document(document_id: str):
    return await admin_get_document(document_id)


@app.post("/talent/api/candidates/{candidate_id}/documents/upload")
async def talent_upload_candidate_document(
    candidate_id: str,
    file: UploadFile = File(...),
    doc_type: str = Form(default="resume"),
    title: Optional[str] = Form(default=None),
    chunk_strategy: str = Form(default="sliding_window"),
):
    allowed_doc_types = {"transcript", "certification", "resume", "bio", "cover_letter", "other"}
    if doc_type not in allowed_doc_types:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported document type '{doc_type}'. Use one of: {', '.join(sorted(allowed_doc_types))}",
        )
    raw_text = " ".join((await _extract_upload_text(file)).split())
    if not raw_text:
        raise HTTPException(status_code=400, detail="Uploaded file did not contain readable text")
    pipeline: IngestionPipeline = app.state.ingestion
    result = await pipeline.ingest(
        candidate_id=candidate_id,
        doc_type=doc_type,
        title=title or file.filename or "Uploaded document",
        raw_text=raw_text,
        chunk_strategy=chunk_strategy,
    )
    return result


@app.post("/talent/api/tools/query-db")
async def talent_query_candidates_db(req: TalentQueryDbRequest):
    """Replay a visible agent DB inspection using the agent's read-only guard."""
    from pipeline.agent_tools import do_query_candidates_db

    return await do_query_candidates_db(app.state.pool, req.sql)


@app.get("/agent/sessions")
async def list_agent_sessions(recruiter_id: str, limit: int = Query(default=30, ge=1, le=100)):
    pool = app.state.pool
    await _ensure_agent_sessions_table(pool)
    rows = await pool.fetch(
        """
        SELECT recruiter_id::text, session_id, title, summary, context_json,
               created_at, updated_at, ended_at
        FROM agent_chat_sessions
        WHERE recruiter_id = $1::uuid
        ORDER BY updated_at DESC
        LIMIT $2
        """,
        recruiter_id,
        limit,
    )
    return {
        "items": [
            {
                "recruiter_id": row["recruiter_id"],
                "session_id": row["session_id"],
                "title": row["title"],
                "summary": row["summary"],
                "context": _coerce_session_context(row["context_json"]),
                "created_at": _json_datetime(row["created_at"]),
                "updated_at": _json_datetime(row["updated_at"]),
                "ended_at": _json_datetime(row["ended_at"]),
            }
            for row in rows
        ]
    }


@app.get("/agent/sessions/{session_id}")
async def get_agent_session(session_id: str, recruiter_id: str):
    pool = app.state.pool
    await _ensure_agent_sessions_table(pool)
    row = await pool.fetchrow(
        """
        SELECT recruiter_id::text, session_id, title, summary, messages_json,
               context_json, created_at, updated_at, ended_at
        FROM agent_chat_sessions
        WHERE recruiter_id = $1::uuid AND session_id = $2
        """,
        recruiter_id,
        session_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Session not found")
    return {
        "recruiter_id": row["recruiter_id"],
        "session_id": row["session_id"],
        "title": row["title"],
        "summary": row["summary"],
        "messages": _coerce_session_messages(row["messages_json"]),
        "context": _coerce_session_context(row["context_json"]),
        "created_at": _json_datetime(row["created_at"]),
        "updated_at": _json_datetime(row["updated_at"]),
        "ended_at": _json_datetime(row["ended_at"]),
    }


@app.post("/agent/sessions/{session_id}/save")
async def save_agent_session(session_id: str, req: AgentSessionSaveRequest):
    pool = app.state.pool
    await _ensure_agent_sessions_table(pool)
    title = req.title or _session_title_from_messages(req.messages)
    summary = req.summary or ""
    row = await pool.fetchrow(
        """
        INSERT INTO agent_chat_sessions
          (recruiter_id, session_id, title, summary, messages_json, context_json, ended_at)
        VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6::jsonb, CASE WHEN $7 THEN now() ELSE NULL END)
        ON CONFLICT (recruiter_id, session_id)
        DO UPDATE SET
          title = EXCLUDED.title,
          summary = CASE
            WHEN EXCLUDED.summary <> '' THEN EXCLUDED.summary
            ELSE agent_chat_sessions.summary
          END,
          messages_json = EXCLUDED.messages_json,
          context_json = EXCLUDED.context_json,
          updated_at = now(),
          ended_at = COALESCE(EXCLUDED.ended_at, agent_chat_sessions.ended_at)
        RETURNING recruiter_id::text, session_id, title, summary, updated_at, ended_at
        """,
        req.recruiter_id,
        session_id,
        title,
        summary,
        json.dumps(req.messages, default=str),
        json.dumps(req.context, default=str),
        bool(req.ended),
    )
    return {
        "recruiter_id": row["recruiter_id"],
        "session_id": row["session_id"],
        "title": row["title"],
        "summary": row["summary"],
        "updated_at": _json_datetime(row["updated_at"]),
        "ended_at": _json_datetime(row["ended_at"]),
    }


@app.post("/agent/sessions/{session_id}/summarize")
async def summarize_agent_session(session_id: str, req: AgentSessionSummaryRequest):
    pool = app.state.pool
    await _ensure_agent_sessions_table(pool)
    summary = await _llm_summarize_agent_session(req.messages, req.context, model=req.model)
    await pool.execute(
        """
        INSERT INTO agent_chat_sessions
          (recruiter_id, session_id, title, summary, messages_json, context_json, ended_at)
        VALUES ($1::uuid, $2, $3, $4, $5::jsonb, $6::jsonb, now())
        ON CONFLICT (recruiter_id, session_id)
        DO UPDATE SET
          title = EXCLUDED.title,
          summary = EXCLUDED.summary,
          messages_json = EXCLUDED.messages_json,
          context_json = EXCLUDED.context_json,
          updated_at = now(),
          ended_at = now()
        """,
        req.recruiter_id,
        session_id,
        summary["title"],
        summary["summary"],
        json.dumps(req.messages, default=str),
        json.dumps(req.context, default=str),
    )
    return summary


# ── Personalization endpoints ──────────────────────────────────────────────

@app.get("/api/recruiter/{recruiter_id}/personalization")
async def get_personalization(recruiter_id: str):
    """Return (enabled, hints[]) — facts from recruiter_memory."""
    pool = app.state.pool
    row = await pool.fetchrow(
        """SELECT personalization_enabled FROM recruiter_preferences
           WHERE recruiter_id = $1::uuid""", recruiter_id)
    enabled = bool(row["personalization_enabled"]) if row else False
    facts = await pool.fetch(
        """SELECT id, content, source, created_at FROM recruiter_memory
           WHERE recruiter_id = $1::uuid AND kind='fact' AND status='active'
           ORDER BY created_at DESC LIMIT 20""", recruiter_id)
    return {"enabled": enabled,
            "hints": [{"id": str(r["id"]), "text": r["content"],
                       "source": r["source"],
                       "created_at": r["created_at"].isoformat()} for r in facts]}


import re as _re_personalization

_HINT_CONTROL_RE = _re_personalization.compile(r"[\x00-\x1f\x7f]")
_HINT_WS_RE      = _re_personalization.compile(r"\s+")


def _clean_hint_text(raw: str) -> str:
    """Sanitize a stored hint: strip control chars, collapse whitespace, trim."""
    cleaned = _HINT_CONTROL_RE.sub(" ", raw)
    cleaned = _HINT_WS_RE.sub(" ", cleaned).strip()
    return cleaned


@app.post("/api/recruiter/{recruiter_id}/hints")
async def add_personalization_hint(recruiter_id: str, body: dict):
    """Append a fact. source defaults to 'manual'. 400 on empty/long/cap."""
    text   = _clean_hint_text(str(body.get("text", "")))
    source = body.get("source") if body.get("source") in {"manual", "suggested"} else "manual"
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 200:
        raise HTTPException(status_code=400, detail="text exceeds 200 chars")

    pool = app.state.pool
    n = await pool.fetchval(
        """SELECT count(*) FROM recruiter_memory
           WHERE recruiter_id = $1::uuid AND kind='fact' AND status='active'""",
        recruiter_id)
    if n >= 20:
        raise HTTPException(status_code=400, detail="hint cap reached (20)")
    await pool.execute(
        """INSERT INTO recruiter_memory
             (recruiter_id, kind, category, content, content_key, source)
           VALUES ($1::uuid, 'fact', 'other', $2, $3 || ':' || MD5(LOWER($2)), $3)
           ON CONFLICT (recruiter_id, kind, content_key) WHERE status='active'
           DO NOTHING""",
        recruiter_id, text, source)
    await _cache.invalidate_many([
        _cache.recruiter_hints_key(recruiter_id),
        _cache.recruiter_prefs_key(recruiter_id),
    ])

    trace_id = body.get("langfuse_trace_id")
    if trace_id and source == "suggested":
        from pipeline.observability import record_score, ScoreName
        record_score(trace_id=trace_id, name=ScoreName.PERSONALIZATION_ACCEPTED,
                     value=1.0, comment=f"Accepted hint: {text}")
    return await get_personalization(recruiter_id)


@app.get("/api/recruiter/{recruiter_id}/memory")
async def get_recruiter_memory(recruiter_id: str):
    """Full memory view: facts + active observations with confidence."""
    pool = app.state.pool
    rows = await pool.fetch(
        """SELECT id, kind, category, content, confidence, evidence_count,
                  source, created_at, last_evidence_at
           FROM recruiter_memory
           WHERE recruiter_id = $1::uuid AND status='active'
           ORDER BY kind, confidence DESC, created_at DESC""", recruiter_id)
    out = {"facts": [], "observations": []}
    for r in rows:
        item = {"id": str(r["id"]), "category": r["category"],
                "content": r["content"], "source": r["source"],
                "confidence": float(r["confidence"]),
                "evidence_count": r["evidence_count"],
                "created_at": r["created_at"].isoformat()}
        out["facts" if r["kind"] == "fact" else "observations"].append(item)
    return out


@app.delete("/api/recruiter/{recruiter_id}/memory/{memory_id}")
async def dismiss_memory_entry(recruiter_id: str, memory_id: str):
    """Dismiss any memory entry by id (facts removed, observations never recreated)."""
    pool = app.state.pool
    res = await pool.execute(
        """UPDATE recruiter_memory SET status='dismissed'
           WHERE id = $1::uuid AND recruiter_id = $2::uuid AND status='active'""",
        memory_id, recruiter_id)
    if res == "UPDATE 0":
        raise HTTPException(status_code=404, detail="memory entry not found")
    await _cache.invalidate(_cache.recruiter_hints_key(recruiter_id))
    return await get_recruiter_memory(recruiter_id)


@app.post("/api/recruiter/{recruiter_id}/memory/profile")
async def trigger_profile(recruiter_id: str):
    """Manually run the behavioral profiler (testing / settings page)."""
    from pipeline.profiler import profile_recruiter
    n = await profile_recruiter(app.state.pool, recruiter_id)
    return {"observations_written": n}


@app.patch("/api/recruiter/{recruiter_id}/personalization")
async def patch_personalization(recruiter_id: str, body: dict):
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="enabled must be a bool")
    pool = app.state.pool
    await pool.execute(
        """INSERT INTO recruiter_preferences (recruiter_id, personalization_enabled)
           VALUES ($1::uuid, $2)
           ON CONFLICT (recruiter_id) DO UPDATE
             SET personalization_enabled = EXCLUDED.personalization_enabled,
                 updated_at = NOW()""",
        recruiter_id, enabled,
    )
    await _cache.invalidate_many([
        _cache.recruiter_hints_key(recruiter_id),
        _cache.recruiter_prefs_key(recruiter_id),
    ])
    return {"enabled": enabled}


@app.post("/api/recruiter/{recruiter_id}/observations/{observation_id}/confirm")
async def confirm_personalization_observation(recruiter_id: str, observation_id: str, body: dict):
    accept = body.get("accept")
    if not isinstance(accept, bool):
        raise HTTPException(status_code=400, detail="accept must be a bool")
    from pipeline.agent_tools import do_confirm_observation
    result = await do_confirm_observation(app.state.pool, recruiter_id, observation_id, accept)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    await _cache.invalidate(_cache.recruiter_hints_key(recruiter_id))
    return result


# ══════════════════════════════════════════
# AGENT ENDPOINTS
# ══════════════════════════════════════════

@app.post("/agent/chat")
async def agent_chat(req: AgentChatRequest):
    """Stream a recruiter-agent response as SSE.

    Runs the pydantic-ai recruiter agent against the session identified by
    (recruiter_id, session_id), appending the user message to the history and
    streaming tool-call and text events back to the caller.
    """
    from pipeline.agent import recruiter_agent, AgentDeps, build_agent_prompt_for_run
    from pipeline import agent_session as _session_store
    from pipeline.agent_run import stream_agent_run

    from pipeline.memory import load_memory
    from pipeline.profiler import profile_recruiter_if_stale
    from pipeline.agent_tools import do_confirm_observation

    pool = app.state.pool
    session = _session_store.get_or_create(req.recruiter_id, req.session_id)
    ctx = req.context or AgentChatContext()
    restored_session_messages = list(ctx.session_messages or [])
    if restored_session_messages:
        last_msg = restored_session_messages[-1]
        if (
            isinstance(last_msg, dict)
            and last_msg.get("role") == "user"
            and " ".join(str(last_msg.get("content") or "").split()) == " ".join(req.message.split())
        ):
            restored_session_messages = restored_session_messages[:-1]
    if not session.messages and restored_session_messages:
        session.messages = _pydantic_history_from_session_messages(restored_session_messages)

    result_ids = [str(x) for x in (ctx.result_ids or []) if x]
    context_candidate_ids = [str(x) for x in (ctx.candidate_ids or []) if x]
    for candidate_id in context_candidate_ids:
        if candidate_id not in result_ids:
            result_ids.append(candidate_id)
    agent_model = req.model or _agent_model

    async def _events():
        with _obs_trace_attributes(
            user_id=req.recruiter_id,
            session_id=req.session_id,
            tags=["agent", "agent:recruiter-sidepanel"],
        ):
            with _obs_start_span(
                "agent.turn",
                input={
                    "message": req.message,
                    "context": {
                        "query": ctx.query or "",
                        "filters": ctx.filters or {},
                        "result_ids_count": len(result_ids),
                        "candidate_ids": context_candidate_ids,
                        "has_session_summary": bool(ctx.session_summary),
                        "restored_session_messages_count": len(restored_session_messages),
                    },
                },
                metadata={
                    "route": "/agent/chat",
                    "agent_model": agent_model,
                    "prompt_name": "recruiter-agent",
                    "prompt_label": settings.agent_prompt_label,
                },
            ):
                # ── Personalization: memory + profiler ───────────────────
                with _obs_start_span("agent.load_personalization", input={"recruiter_id": req.recruiter_id}):
                    memory = await load_memory(pool, req.recruiter_id)
                    # First turn of an opted-in session → refresh behavioral
                    # observations in the background. Disabled recruiters get
                    # no implicit profiling.
                    if (
                        memory.enabled
                        and not session.messages
                        and _looks_like_uuid(req.recruiter_id)
                    ):
                        asyncio.create_task(profile_recruiter_if_stale(pool, req.recruiter_id))
                    _obs_update_current_span(output={
                        "enabled": memory.enabled,
                        "facts": len(memory.facts),
                        "observations": len(memory.observations),
                    })

                if session.pending_observation:
                    answer = _normalise_confirmation_answer(req.message)
                    if answer is not None:
                        pending = session.pending_observation
                        with _obs_start_span(
                            "agent.confirm_observation",
                            input={"observation_id": pending.get("id"), "accept": answer},
                        ):
                            result = await do_confirm_observation(
                                pool,
                                req.recruiter_id,
                                pending["id"],
                                answer,
                            )
                            _obs_update_current_span(output=result)
                        session.pending_observation = None
                        reply = (
                            "Got it — I saved that preference."
                            if answer else
                            "Got it — I dismissed that observation."
                        )
                        for event in _agent_text_event(reply):
                            yield event
                        _obs_update_current_span(output={"status": "personalization_confirmed", "accepted": answer})
                        return
                    else:
                        # Unrecognised reply — treat as implicit skip, let message
                        # pass through to the agent as a normal query.
                        session.pending_observation = None

                surfaced_observation = None
                if memory.enabled and session.pending_observation is None:
                    surfaced_observation = _pick_relevant_observation(
                        req.message,
                        ctx,
                        memory.observations,
                        session.asked_observation_ids,
                    )
                    if surfaced_observation is not None:
                        obs_id = str(surfaced_observation.id)
                        session.pending_observation = {
                            "id": obs_id,
                            "content": surfaced_observation.content,
                        }
                        session.asked_observation_ids.add(obs_id)
                        # Question is appended after the agent response, not here.

                observations = [
                    {
                        "id": o.id,
                        "content": o.content,
                        "category": o.category,
                        "confidence": o.confidence,
                        "evidence_count": o.evidence_count,
                    }
                    for o in memory.observations
                    if surfaced_observation is None or str(o.id) != str(surfaced_observation.id)
                ]
                hints = [f.content for f in memory.facts]
                if ctx.session_summary:
                    hints.append("Previous session summary: " + ctx.session_summary[:1800])
                if ctx.candidate_summaries:
                    for candidate in ctx.candidate_summaries[:8]:
                        name = candidate.get("name") or candidate.get("full_name") or candidate.get("id")
                        bits = [
                            f"name={name}",
                            f"id={candidate.get('id') or candidate.get('candidate_id')}",
                            f"location={candidate.get('city') or ''} {candidate.get('country') or ''}".strip(),
                            f"skills={', '.join((candidate.get('skills') or [])[:8])}",
                        ]
                        hints.append("Selected candidate context: " + "; ".join([b for b in bits if b and not b.endswith("=")]))
                system_prompt, lf_prompt = build_agent_prompt_for_run(
                    query=ctx.query or "",
                    filters=ctx.filters or {},
                    result_count=len(result_ids),
                    hints=hints,
                    observations=observations,
                    label=settings.agent_prompt_label,
                )
                deps = AgentDeps(
                    pool=pool,
                    session=session,
                    recruiter_id=req.recruiter_id,
                    search_engine=app.state.search_engine,
                    query=ctx.query or "",
                    filters=ctx.filters or {},
                    result_ids=result_ids,
                    hints=hints,
                    observations=observations,
                    system_prompt=system_prompt,
                    lf_prompt=lf_prompt,
                )
                try:
                    async for event in stream_agent_run(
                        recruiter_agent,
                        message=req.message,
                        model=agent_model,
                        deps=deps,
                        session=session,
                    ):
                        yield event
                    # After the agent responds, surface any pending observation question.
                    if surfaced_observation is not None:
                        for event in _agent_text_event(
                            _observation_question(surfaced_observation.content)
                        ):
                            yield event
                        _obs_update_current_span(output={
                            "status": "personalization_confirmation_requested",
                            "observation_id": str(surfaced_observation.id),
                        })
                    _obs_update_current_span(output={"status": "completed"})
                except Exception as exc:
                    _obs_update_current_span(output={
                        "status": "error",
                        "error": str(exc)[:200],
                    })
                    raise

    return StreamingResponse(
        _events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/agent/session/clear", status_code=204)
async def agent_session_clear(req: AgentChatRequest):
    """Clear the in-memory agent session for (recruiter_id, session_id).

    Accepts the same AgentChatRequest body for convenience; message and context
    fields are ignored.
    """
    from pipeline import agent_session as _session_store
    _session_store.clear(req.recruiter_id, req.session_id)


class AgentModelRequest(BaseModel):
    model: str


@app.get("/agent/model")
async def get_agent_model():
    return {"model": _agent_model}


@app.post("/agent/model")
async def set_agent_model(req: AgentModelRequest):
    global _agent_model
    _agent_model = req.model
    return {"model": _agent_model}


class PushResultsRequest(BaseModel):
    candidate_ids: list[str]
    query: str = ""
    scores: Optional[dict[str, float]] = None  # candidate_id -> agent score


@app.post("/agent/push-results", response_model=SearchResponse)
async def agent_push_results(req: PushResultsRequest):
    """Hydrate an explicit set of candidate IDs into the main-panel result shape.

    The agent can find candidates in ways the search bar can't (keyword-only
    matches, ad-hoc SQL, etc.), so we render the EXACT candidates it retrieved
    rather than re-running a search. Preserves the agent's ordering.
    """
    start = time.perf_counter()
    pool = app.state.pool
    ids = [c for c in (req.candidate_ids or []) if c][:50]
    scores = req.scores or {}

    if not ids:
        return SearchResponse(
        query=req.query or "agent results",
        filters_applied={"source": "agent_selected", "result_kind": "selected"},
        results=[], total_results=0,
            latency_ms=0.0, timings_ms={"total": 0.0},
            clarify=None, relaxations_applied=[],
        )

    rows = await pool.fetch(
        """
        WITH requested AS (
            SELECT id::uuid AS id, ord
            FROM unnest($1::uuid[]) WITH ORDINALITY AS t(id, ord)
        )
        SELECT
            c.id::text AS candidate_id,
            c.full_name,
            c.email,
            c.city,
            c.country,
            c.salary_min,
            c.salary_max,
            c.years_exp,
            c.skills,
            best.content,
            best.doc_type,
            best.document_title
        FROM requested req
        JOIN candidates c ON c.id = req.id
        LEFT JOIN LATERAL (
            SELECT dc.content, cd.doc_type, cd.title AS document_title
            FROM document_chunks dc
            JOIN candidate_documents cd ON cd.id = dc.document_id
            WHERE dc.candidate_id = c.id
            ORDER BY dc.created_at
            LIMIT 1
        ) best ON TRUE
        ORDER BY req.ord
        """,
        ids,
    )
    row_by_id = {row["candidate_id"]: row for row in rows}

    results: list[CandidateResult] = []
    for cid in ids:  # preserve the agent's ordering
        row = row_by_id.get(cid)
        if not row:
            continue
        score = scores.get(cid)
        results.append(CandidateResult(
            candidate_id=cid,
            full_name=row["full_name"] or "",
            email=row["email"],
            city=row["city"],
            country=row["country"],
            salary_min=row["salary_min"],
            salary_max=row["salary_max"],
            years_exp=int(row["years_exp"]) if row["years_exp"] is not None else None,
            skills=list(row["skills"] or []),
            best_chunk=(row["content"] or "")[:400],
            similarity_score=round(score / 100, 4) if score is not None else 0.0,
            doc_type=(row["doc_type"] or ""),
            document_title=row["document_title"],
            supporting_chunks=[],
            rerank_score=None,
            rrf_score=None,
            rank_score=round(score, 4) if score is not None else None,
            ranking_explanation=None,
        ))

    elapsed_ms = (time.perf_counter() - start) * 1000
    return SearchResponse(
        query=req.query or "agent results",
        filters_applied={"source": "agent_selected", "result_kind": "selected"},
        results=results,
        total_results=len(results),
        latency_ms=round(elapsed_ms, 2),
        timings_ms={"total": round(elapsed_ms, 2)},
        clarify=None,
        relaxations_applied=[],
    )
