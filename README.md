# Talent Atlas — Interruptible Realtime Hybrid RAG

Talent Atlas is a conversational recruiting search system over PostgreSQL and
pgvector. Gemini Live handles audio and natural conversation while an
application-owned realtime harness validates typed tool calls, creates immutable
search revisions, runs SQL, vector, keyword and exact-skill branches, preserves
superseded work instead of discarding it, serves a repeated plan from a
fingerprint-keyed revision cache with zero retrieval, fuses rankings, reranks a
bounded pool, and returns grounded candidate evidence.

The recruiter stays in the original `/talent` interface. The execution graph is
hidden under **Activity** until someone wants to inspect the fork/join workflow,
the exact bounded results, timings, inputs, evidence or raw event for each node.

```bash
cd /Volumes/MAC/Projects_devolopment/Samsum_rag
source scripts/setup_external_runtime.sh
docker compose up -d postgres
.venv/bin/python scripts/verify_local_corpus.py --expected-candidates 10000 --require-external-root
.venv/bin/python -m uvicorn api.main:app --host 127.0.0.1 --port 8010
# Open http://127.0.0.1:8010/talent
```

`GOOGLE_API_KEY` and `GEMINI_LIVE_MODEL=gemini-3.8-live` are required only for
voice. Typed search remains available without Gemini. The setup script
deliberately selects the SSD-local Docker database and disables remote telemetry.

Validate interruption, branch reuse and guardrails without network access:

```bash
.venv/bin/python scripts/evaluate_realtime_agent.py
```

The product strategy and official acceptance-gate mapping live in
[`docs/hackathon/STRATEGY.md`](docs/hackathon/STRATEGY.md). The supporting
research brief is in
[`docs/hackathon/research/research-report.md`](docs/hackathon/research/research-report.md).

## Existing Search Core

Two-stage search engine for hiring platforms: **structured SQL filtering** → **semantic vector similarity**.

## Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Search Query                         │
│  "Python dev with ML experience near Bangalore"        │
├──────────────────────┬──────────────────────────────────┤
│   Stage 1 (SQL)      │   Stage 2 (Vector)              │
│   country='India'    │   embed(query) <=> chunks        │
│   city='Bangalore'   │   cosine similarity              │
│   'python' ∈ skills  │   HNSW index traversal           │
│   ─── B-tree/GIN ─── │   ─── pgvector HNSW ───          │
├──────────────────────┴──────────────────────────────────┤
│              Ranked Results (< 100ms)                   │
└─────────────────────────────────────────────────────────┘
```

## Quick Start

The app connects to whatever `DATABASE_URL` points at in `.env`. You can run
against **local Docker Postgres** or **Supabase cloud** — pick one.

### Option A — Local Docker Postgres

```bash
# 1. Start PostgreSQL + pgvector
docker compose up -d

# 2. Install Python deps
pip install -e ".[dev]"

# 3. Copy env and add your API key
cp .env.example .env
#    DATABASE_URL=postgresql://hybrid_user:hybrid_pass@localhost:5432/hiring_platform

# 4. Seed test data (100 candidates with documents)
python db/seed.py --candidates 100

# 5. Start the API
uvicorn api.main:app --reload
```

### Option B — Supabase cloud (current default in `.env`)

A 15k-candidate subset is already hosted on Supabase (free tier, ~362 MB). No
Docker needed for the database — just point `.env` at the Supabase **session
pooler** and start the app:

```bash
# .env (uses the IPv4 session pooler on port 5432 — NOT the IPv6 direct host):
# DATABASE_URL=postgresql://postgres.<ref>:<pw>@aws-1-<region>.pooler.supabase.com:5432/postgres

pip install -e ".[dev]"
uvicorn api.main:app --reload          # embeddings still run locally
```

Verify either option: `curl -s http://localhost:8000/health` → expect
`"candidates": 15000` (Supabase) or your seeded count (local).

> Migrating local data to a Supabase free-tier project (subset sizing, the
> IPv6/pooler/timeout gotchas) is documented in
> [docs/supabase_migration_runbook.md](docs/supabase_migration_runbook.md), with
> a ready-to-run script at `scripts/migrate_subset_to_supabase.sh`.

## Embedding Providers

| Provider | Model | Dims | Cost/1M tokens | Notes |
|----------|-------|------|----------------|-------|
| **local** | all-MiniLM-L6-v2 | 384 | $0.00 | Zero cost, ~15ms/batch |
| **local** | bge-small-en-v1.5 | 384 | $0.00 | Strong MTEB scores |
| **gemini** | text-embedding-004 | 768 | $0.00 | Free tier generous |
| **openai** | text-embedding-3-small | 1536 | $0.02 | Best price/quality |
| **voyage** | voyage-3-lite | 512 | $0.02 | Fast + cheap |
| **jina** | jina-embeddings-v3 | 1024 | $0.02 | OpenAI-compatible API |
| **cohere** | embed-english-v3.0 | 1024 | $0.10 | Supports input types |
| **openai** | text-embedding-3-large | 3072 | $0.13 | Highest quality |

Switch provider in `.env`:
```
EMBEDDING_PROVIDER=local
EMBEDDING_MODEL=all-MiniLM-L6-v2
EMBEDDING_DIMENSIONS=384
```

### Benchmark Models
```bash
# Compare all providers you have keys for
python scripts/benchmark.py --providers openai,gemini,local
```

### Benchmark Search Strategies
```bash
# Compare semantic, RRF, rerank, and optional LLM-planned strategies
python3 scripts/benchmark_search_strategies.py
```

The search strategy benchmark writes:

- `reports/search_strategy_benchmark.json`
- `reports/search_strategy_benchmark.html`

It compares `semantic`, `rrf`, `rrf_rerank`, `llm_rrf`, and `llm_rrf_rerank` using manual eval cases plus sampled imported cases from `data/recruitment_dataset/eval_cases_5000.jsonl`.

Reported metrics include Top-1, Hit@3, Hit@10, MRR@10, nDCG@10, p50/p95/p99 latency, stage timings for planning/embedding/retrieval/rerank, planner accuracy where labels exist, and estimated LLM cost per 1,000 searches.

Planner strategies are opt-in benchmark paths. If the selected LLM provider is not configured, the planner safely falls back to the original query and filters.

```bash
# Smaller smoke run
python3 scripts/benchmark_search_strategies.py \
  --strategies rrf,llm_rrf,llm_rrf_rerank \
  --imported-sample-size 10 \
  --top-k 10

# Use OpenAI instead of Gemini for the planner
python3 scripts/benchmark_search_strategies.py --llm-provider openai
```

## API

### Search
```bash
curl -X POST http://localhost:8000/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "experienced ML engineer who has worked with transformer models",
    "location": "Bangalore, India",
    "min_years_exp": 3,
    "min_salary": 90000,
    "skills": ["python", "machine-learning"],
    "top_k": 10
  }'
```

### Ingest a Document
```bash
curl -X POST http://localhost:8000/ingest \
  -H "Content-Type: application/json" \
  -d '{
    "candidate_id": "uuid-here",
    "doc_type": "transcript",
    "title": "Technical Interview — Round 2",
    "raw_text": "Full transcript text here..."
  }'
```

## Project Structure

```
hybrid-search/
├── docker-compose.yml          # Postgres + PgBouncer
├── pyproject.toml              # Python project config
├── .env.example                # Environment template
├── db/
│   ├── migrations/             # SQL schema + indexes
│   └── seed.py                 # Test data generator
├── pipeline/
│   ├── __init__.py             # Config (pydantic-settings)
│   ├── chunker.py              # Token-aware text chunking
│   ├── embedder.py             # 6 providers, 14+ models
│   ├── database.py             # Connection pool manager
│   ├── ingest.py               # Document ingestion pipeline
│   └── search.py               # Two-stage hybrid search engine
├── api/
│   └── main.py                 # FastAPI endpoints
└── scripts/
    └── benchmark.py            # Model comparison tool
```

## Performance Targets

| Metric | Target | How |
|--------|--------|-----|
| Search latency | < 100ms | ef_search=40, connection pooling, pre-filtered search space |
| Embedding cost | $0/month | Use `local` provider with sentence-transformers |
| Index memory | ~960MB/1M vectors | 384-dim with HNSW (all-MiniLM-L6-v2) |
