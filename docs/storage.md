# Storage Reference

A single-page index of **everything the system persists or caches** — pulled from `docs/current_pipeline_status.md`, `docs/how_it_works.md`, `docs/toggle.md`, `docs/implementation_plan.md`, the SQL migrations in [db/migrations/](../db/migrations/), and [pipeline/cache.py](../pipeline/cache.py).

Updated: 2026-05-24

---

## 1. Storage Layers At A Glance

| Layer | Backend | Lifetime | Purpose |
|---|---|---|---|
| Primary database | PostgreSQL 17 (pgvector) in Docker | Persistent (Docker volume `pgdata`) | Candidates, documents, chunks, embeddings, skills, impressions, outcomes, recruiter prefs |
| Connection pooling | PgBouncer (optional) | Process | Higher connection counts on port `6432` |
| Vector index | pgvector HNSW (on-disk in Postgres) | Persistent | ANN search over chunk embeddings |
| Full-text index | Postgres GIN on `tsvector` | Persistent | BM25-style keyword retrieval |
| In-process cache | LRU in [pipeline/cache.py](../pipeline/cache.py) | Process lifetime (TTL'd) | Plan / HyDE / embedding caches |
| Reranker cache | App-state dict (`_get_cached_reranker`) | Process lifetime | Avoid reloading cross-encoder models |
| Model weights | sentence-transformers / CrossEncoder local cache | Disk (HuggingFace default cache) | Local embedding + rerank models |
| Settings | `.env` file via `POST /admin/model-settings` | Disk | API keys, provider toggles |
| Benchmark reports | `reports/*.json` + `reports/*.html` | Disk | Benchmark artifacts |

---

## 1.1 Measured footprint (live database, 2026-05-24)

Pulled from the running `hiring_platform` DB via [check_db_size.py](../check_db_size.py).

**Total database:** **714 MB** across **30,767 candidates / 28,610 documents / 65,211 chunks**.

| Table | Rows | Data | Indexes | Total | Bytes/row |
|---|---:|---:|---:|---:|---:|
| `document_chunks` | 65,211 | 412 MB | 180 MB | **592 MB** | ~9.5 KB |
| `candidate_documents` | 28,610 | 87 MB | 6.5 MB | **93 MB** | ~3.3 KB |
| `candidates` | 30,767 | 11 MB | 9.9 MB | **21 MB** | ~700 B |

> Note: migrations [004_impressions.sql](../db/migrations/004_impressions.sql) and [005_candidate_skills.sql](../db/migrations/005_candidate_skills.sql) have **not** been applied to this instance — `candidate_skills`, `search_impressions`, `search_outcomes`, and `recruiter_preferences` are 0 bytes here. They run on first init only via `/docker-entrypoint-initdb.d`, so a fresh volume is needed to pick them up.

### Where the 592 MB on `document_chunks` actually goes

| Component | Size | Share |
|---|---:|---:|
| Heap (rows + TOAST) | 412 MB | 70% |
| HNSW vector index (`idx_chunks_embedding`) | **124 MB** | 21% |
| GIN full-text index (`idx_chunks_content_fts`) | **50 MB** | 8% |
| B-tree on `(id)` PK | 2.5 MB | <1% |
| B-tree on `document_id` | 2.0 MB | <1% |
| B-tree on `candidate_id` | 1.8 MB | <1% |

### Average per-chunk content

| Measure | Value |
|---|---:|
| `content` length (bytes) | 2,297 |
| `token_count` | 374 |
| `content_tsv` (bytes) | 2,397 |
| `embedding` (raw on disk) | 1,536 B = 384 × 4 B float |

So a single chunk row is roughly: 2.3 KB text + 2.4 KB tsvector + 1.5 KB embedding ≈ **6.2 KB heap**, plus ~3.3 KB amortized index cost.

### Per-candidate end-to-end

`714 MB / 30,767 candidates` ≈ **23 KB/candidate** total — broken down as ~0.7 KB row + ~3.3 KB document + ~2.1 chunks × 9.5 KB.

Raw `candidate_documents.raw_text` length: avg **4,430 bytes**, min 177, max 66,216.

### Back-of-envelope scaling

| Corpus size | Projected DB total |
|---|---:|
| 30 K candidates (current) | 0.7 GB |
| 100 K candidates | ~2.3 GB |
| 1 M candidates | ~23 GB |
| 10 M candidates | ~230 GB |

HNSW index growth is roughly linear with chunk count and dominated by the 1.5 KB/embedding heap cost plus ~2 KB/embedding in graph edges. At 1 M candidates expect the HNSW alone to approach ~4 GB.

### 1.2 Cloud deployment (Supabase free tier) — measured 2026-06-17

Re-measured full local DB: **736 MB** across **30,792 candidates / 65,260 chunks**
(grown slightly from the 714 MB above). The **Supabase free tier caps a project
at 500 MB**, so the full set does **not** fit.

A **15,000-candidate subset** was migrated to Supabase instead:

| | Full local | Supabase subset |
|---|---:|---:|
| candidates | 30,792 | 15,000 |
| document_chunks | 65,260 | 31,718 |
| **DB size** | **736 MB** | **362 MB** ✅ (< 500 MB) |

- Live as of 2026-06-17 (project `fitssgldrsnjqunbkfrm`, region ap-southeast-2).
- The app points at it via `DATABASE_URL` (Supabase **session pooler**, IPv4).
- To fit the **full** dataset on free tier you'd need to shave ~240 MB
  (`vector(384)→halfvec(384)` −110 MB, drop redundant `candidate_documents.raw_text`
  −66 MB, LZ4 TOAST / drop stored `content_tsv` −50–90 MB) — tight; Pro (8 GB) is easier.
- Migration mechanics + gotchas: [supabase_migration_runbook.md](supabase_migration_runbook.md),
  script `scripts/migrate_subset_to_supabase.sh`.

---

## 2. PostgreSQL — Container & Volume

From `docs/current_pipeline_status.md`:

```text
postgres:
  image: pgvector/pgvector:pg17
  container: hybrid_search_db
  port: 5432

pgbouncer:
  image: edoburu/pgbouncer
  container: hybrid_search_pgbouncer
  port: 6432
  pool_mode: transaction
```

- **Persistence:** Docker named volume `pgdata` (defined in `docker-compose.yml`). Survives container restarts.
- **Migrations:** SQL files in [db/migrations/](../db/migrations/) are mounted at `/docker-entrypoint-initdb.d` and run **on first init only**.
- **Async driver:** `asyncpg`, used by `pipeline/database.py` and `api/main.py`.

### Required extensions ([001_extensions.sql](../db/migrations/001_extensions.sql))

```sql
CREATE EXTENSION IF NOT EXISTS vector;       -- pgvector for embeddings
CREATE EXTENSION IF NOT EXISTS pg_trgm;      -- trigram fuzzy matching
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";  -- UUID generation fallback
```

---

## 3. Database Tables

All schemas from [db/migrations/](../db/migrations/).

### 3.1 `candidates` — structured, filterable profile data
Source: [002_tables.sql](../db/migrations/002_tables.sql)

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `gen_random_uuid()` |
| `full_name` | TEXT NOT NULL | |
| `email` | TEXT UNIQUE | |
| `age` | INTEGER | CHECK 16–100 |
| `location`, `city`, `country` | TEXT | Free-text + structured location split |
| `interests` | TEXT[] | Default `'{}'` |
| `skills` | TEXT[] | Default `'{}'` — canonical skill list |
| `years_exp` | INTEGER | CHECK ≥ 0 |
| `salary_min`, `salary_max` | INTEGER | Range overlap filter target |
| `status` | TEXT | `active` / `archived` / `hired` / `rejected` |
| `created_at`, `updated_at` | TIMESTAMPTZ | `updated_at` auto-bumped by trigger `update_candidates_updated_at` |

### 3.2 `candidate_documents` — raw text
| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `candidate_id` | UUID FK | `ON DELETE CASCADE` |
| `doc_type` | TEXT | `transcript` / `certification` / `resume` / `bio` / `cover_letter` / `other` |
| `title` | TEXT | |
| `raw_text` | TEXT NOT NULL | Full document |
| `file_hash` | TEXT | **SHA-256 of `raw_text`** — used for ingestion dedupe |
| `created_at` | TIMESTAMPTZ | |

### 3.3 `document_chunks` — the searchable layer
| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `candidate_id`, `document_id` | UUID FK | Cascade delete |
| `chunk_index` | INTEGER | |
| `content` | TEXT | The chunk text |
| `embedding` | `VECTOR(384)` | Must match `EMBEDDING_DIMENSIONS`. **1,536 B raw** (384 × 4-byte float) |
| `token_count` | INTEGER | Avg **374** in current corpus |
| `metadata` | JSONB | Default `'{}'` |
| `content_tsv` | `TSVECTOR` GENERATED ALWAYS AS `to_tsvector('english', content)` STORED | BM25 source. Avg **2.4 KB**/row, indexed by GIN (~50 MB at 65 K chunks) |
| `created_at` | TIMESTAMPTZ | |

### 3.4 `candidate_skills` — per-skill recency
Source: [005_candidate_skills.sql](../db/migrations/005_candidate_skills.sql)

| Column | Type | Notes |
|---|---|---|
| `(candidate_id, skill)` | UUID + TEXT PK | Composite |
| `last_used_at` | TIMESTAMPTZ | Powers `skill_recency` feature in the ranker |
| `source` | TEXT | `resume` / `manual` / `inferred` / `backfill` |

A one-shot backfill explodes `candidates.skills[]` into rows with `last_used_at = updated_at` (re-runnable via `ON CONFLICT DO NOTHING`). The TEXT[] on `candidates` remains canonical and is kept in sync by the ingest pipeline.

### 3.5 `search_impressions` — one row per result shown
Source: [004_impressions.sql](../db/migrations/004_impressions.sql)

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `search_id` | UUID | Groups the impression set |
| `recruiter_id` | UUID | |
| `candidate_id` | UUID FK CASCADE | |
| `position` | INT | Rank shown |
| `spec_hash` | TEXT | Ties impression back to the planned spec |
| `final_score` | REAL | Score used for ordering |
| `shown_at` | TIMESTAMPTZ | |

### 3.6 `search_outcomes` — recruiter actions on impressions
| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `impression_id` | UUID FK CASCADE | |
| `action` | TEXT | `viewed` / `saved` / `contacted` / `shortlisted` / `archived` / `rejected` |
| `occurred_at` | TIMESTAMPTZ | |

### 3.7 `recruiter_preferences` — per-recruiter ranking config
| Column | Type | Notes |
|---|---|---|
| `recruiter_id` | UUID PK | |
| `prioritize_recent_experience` | BOOLEAN | Default TRUE |
| `prioritize_exact_skill_match` | BOOLEAN | Default TRUE |
| `diversity` | BOOLEAN | Default TRUE |
| `weight_overrides_json` | JSONB | Default `'{}'` |
| `updated_at` | TIMESTAMPTZ | |

> Important: `ranking_preferences` is **not** LLM-extracted — it always comes from this table or workspace defaults (per `implementation_plan.md` §116).

---

## 4. Indexes ([003_indexes.sql](../db/migrations/003_indexes.sql))

### Structured-filter indexes on `candidates`
- B-tree: `country`, `city`, `(country, city)`, `age`, `years_exp`, `status`, `(salary_min, salary_max)`
- GIN: `skills`, `interests`

### Retrieval indexes on `document_chunks`
- **HNSW** on `embedding` with `vector_cosine_ops`, `m = 16`, `ef_construction = 200` — **124 MB at 65,211 chunks** (~2 KB/embedding in graph edges, on top of the 1.5 KB heap vector)
- **GIN** on `content_tsv` (full-text / BM25) — **50 MB at 65,211 chunks** (~800 B/row)
- B-tree joins: `candidate_id` (1.8 MB), `document_id` (2.0 MB)

### Document indexes on `candidate_documents`
- `candidate_id`, `doc_type`, `file_hash`

### Impressions / outcomes
- `search_impressions`: `(candidate_id)`, `(recruiter_id, shown_at)`, `(search_id)`
- `search_outcomes`: `(impression_id)`, `(action)`

### Skill recency
- `candidate_skills (skill)`
- `candidate_skills (candidate_id, last_used_at DESC)`

> Filter indexes shrink the candidate set; HNSW makes vector ANN fast; the GIN full-text index recovers exact terms (acronyms, tools) that embeddings can blur.

---

## 5. In-Process Cache ([pipeline/cache.py](../pipeline/cache.py))

A simple `OrderedDict`-based LRU (`maxsize=2048`) with per-entry TTL. Versioned by constants in `pipeline/constants.py` so bumping a version invalidates the affected namespace automatically.

| Namespace | Key shape | TTL | Stores |
|---|---|---|---|
| `plan:` | `plan:{PLANNER_VERSION}:{sha256(raw\|filters\|route)[:16]}` | 24 h | LLM-planned `CanonicalSearchSpec` JSON |
| `hyde:` | `hyde:{HYDE_VERSION}:{hash}` | 7 d | HyDE profile string |
| `embed:` | `embed:{EMBEDDING_MODEL_VERSION}:{hash}` | 30 d | Embedding vectors `list[float]` |

- **Stats:** `cache_stats()` returns `{hits, misses, sets}` — exposed for observability.
- **Optional Redis fallback** was scoped in `implementation_plan.md` §0.4; the current implementation uses in-memory LRU only.
- **Toggle:** `use_cache` in `SEARCH_CONFIG` (see `docs/toggle.md` §3). First call ~800 ms LLM cost → cached call ~50 ms.

### Version constants — "invalidate the cache" knobs
From `pipeline/constants.py`:

| Constant | Bump when |
|---|---|
| `PLANNER_VERSION` | Planner prompt or schema changes |
| `EMBEDDING_MODEL_VERSION` | Embedding model changes |
| `HYDE_VERSION` | HyDE prompt or expectations change |

---

## 6. Application-State Caches

| Cache | Location | Lifetime |
|---|---|---|
| Cross-encoder reranker instances | `_get_cached_reranker` (FastAPI app state) | Process | Avoids reloading sentence-transformers / CrossEncoder weights between requests |
| Local embedding model | `SentenceTransformer` instance | Process | First call cold; subsequent calls reuse the loaded model |

Cold-start consequences (per `current_pipeline_status.md` §1105):
- DB + vector index page cache may be cold
- Reranker weights may not yet be loaded
- Embedding model may not yet be loaded

After warmup, repeated rerank/embedding calls are much faster.

---

## 7. Configuration Storage

| What | Where | Read by |
|---|---|---|
| Provider keys, model selections, toggles | `.env` (project root) | `pipeline/__init__.py` (pydantic-settings) |
| Settings persistence endpoint | `POST /admin/model-settings` writes back to `.env` | Admin UI |
| Pending vs. live settings | Pending settings staged separately because embedding-dimension changes require re-embedding documents | Settings UI |

Defaults that affect storage:

| Setting | Default | Storage implication |
|---|---|---|
| `EMBEDDING_PROVIDER` | `local` | No outbound network; model cached on disk |
| `EMBEDDING_MODEL` | `all-MiniLM-L6-v2` | Local HF cache |
| `EMBEDDING_DIMENSIONS` | `384` | Must match `VECTOR(384)` column |
| `CHUNK_SIZE` | `512` tokens | Sets row size in `document_chunks` |
| `CHUNK_OVERLAP` | `64` tokens | More overlap → more chunk rows |
| `MAX_BATCH_SIZE` | `100` | Embedding write batch size |
| `HNSW_EF_SEARCH` | `40` | Runtime ANN tradeoff (no on-disk impact) |

---

## 8. Disk Artifacts Outside Postgres

| Path | Contents | Lifetime |
|---|---|---|
| `.env` | Provider keys, toggles | Persistent, edited by admin UI |
| `reports/*.json` | Benchmark output (e.g. `reports/search_strategy_benchmark.json`) | Persistent until cleared |
| `reports/*.html` | Static report renderings (e.g. `reports/search_strategy_benchmark.html`) | Persistent until cleared |
| HuggingFace cache dir | sentence-transformers + CrossEncoder weights | Default user cache (`~/.cache/huggingface/...`) |

---

## 9. Ingestion-Side Storage Rules

From `current_pipeline_status.md` §218 and the schema:

1. Compute `file_hash = sha256(raw_text)`.
2. If `(candidate_id, file_hash)` already exists in `candidate_documents`, return `duplicate_skipped` — **no new rows written**.
3. Otherwise insert one `candidate_documents` row, then N `document_chunks` rows (`chunk_size=512`, `chunk_overlap=64`, sliding-window default; paragraph alternative also supported).
4. Embeddings are L2-normalized (`normalize_embeddings = true`, `batch_size = 64`) so cosine distance is stable against pgvector's `vector_cosine_ops`.
5. `candidates.skills[]` is kept canonical; `candidate_skills` rows are upserted alongside to drive the skill-recency feature.

---

## 10. Things That Are **Not** Stored

- **No Redis / Elasticsearch / Pinecone.** Postgres holds both structured data and vectors (per `current_pipeline_status.md` §125).
- **No separate vector store.** HNSW lives inside Postgres via pgvector.
- **No queue/broker.** Ingestion is synchronous via the API.
- **No long-term log store** described in docs — observability is in-process counters (`cache_stats`, latency histograms in `how_it_works.md` §292).
- **No outbound storage** for sanitized/raw user queries beyond `search_impressions.spec_hash` (a hash, not the text).

---

## Source documents

- [docs/current_pipeline_status.md](current_pipeline_status.md) — stack table, schema diagram, indexes, configuration defaults, ingestion pipeline
- [docs/how_it_works.md](how_it_works.md) — pipeline step ordering, cache lookup placement
- [docs/toggle.md](toggle.md) — cache TTLs, version constants
- [docs/implementation_plan.md](implementation_plan.md) — original cache + schema design
- [db/migrations/001_extensions.sql](../db/migrations/001_extensions.sql) → [005_candidate_skills.sql](../db/migrations/005_candidate_skills.sql)
- [pipeline/cache.py](../pipeline/cache.py)
