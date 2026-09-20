# Migrating a Subset to Supabase Cloud (Free Tier)

Goal: run this app on Supabase's **free tier** (500 MB DB / 500 MB RAM). The full
local DB is **736 MB**, so we load a **~15,000-candidate subset** (~360 MB) instead
of all 30,792. Embeddings are copied as-is from your local DB — nothing is
re-computed.

> Sizes measured from the live local DB:
> `document_chunks` 592 MB (incl. 124 MB HNSW + 50 MB FTS GIN + 292 MB text),
> `candidate_documents` 93 MB, `candidates` 21 MB, `candidate_skills` 20 MB.

---

## Step 0 — Make sure the local DB is up

```bash
cd "/Volumes/MAC/Projects_devolopment/hybrid search"
docker compose up -d postgres
docker exec hybrid_search_db pg_isready -U hybrid_user -d hiring_platform
```

## Step 1 — Create the Supabase project (manual, ~2 min)

1. https://supabase.com → **New project** (Free plan). Pick a region near you.
2. Set a database password (you'll need it in the connection string).
3. Wait for it to provision.

## Step 2 — Enable the extensions

Supabase **supports pgvector on the free tier.** In the project's **SQL Editor**:

```sql
create extension if not exists vector;
create extension if not exists pg_trgm;
create extension if not exists "uuid-ossp";
```

## Step 3 — Grab the connection string

Dashboard → **Project Settings → Database → Connection string → URI**.

- For the **bulk load**, use the **Direct connection** or **Session pooler**
  (port **5432**). Do **not** use the Transaction pooler (6543) for the load.
- Export it:

```bash
export SUPABASE_DB_URL='postgresql://postgres.xxxx:[email protected]:5432/postgres'
```

## Step 4 — Run the migration script

```bash
SUBSET=15000 ./scripts/migrate_subset_to_supabase.sh
```

What it does (all via psql/pg_dump **inside the container** — no Mac-side Postgres tools needed):
1. Dumps the exact schema (`--schema-only --no-owner --no-privileges`) and applies it to Supabase.
2. Copies a deterministic subset (`ORDER BY id LIMIT 15000`) of
   `candidates → candidate_skills → candidate_documents → document_chunks`
   in FK order, **excluding** the generated `content_tsv` column (Postgres
   recomputes it on insert).
3. Prints row counts and the final Supabase DB size.

✅ Target: final size **< 500 MB**. If it comes back higher, re-run with a smaller
`SUBSET` (e.g. `SUBSET=12000`). If lower and you want more data, bump it up.

> Telemetry tables (`search_impressions`, `search_outcomes`,
> `relevance_judgments`, `recruiter_memory`, `search_history`,
> `agent_chat_sessions`) are intentionally **not** copied — they're runtime data,
> tiny, and the app recreates rows as you use it. The schema for them is still
> created in Step 4.1 so the app works.

## Step 5 — Point the app at Supabase

The app reads `settings.database_url` (default in `pipeline/__init__.py`,
overridable by env). Set it in `.env`:

```bash
DATABASE_URL=postgresql://postgres.xxxx:[email protected]:5432/postgres
```

### ⚠️ Required code change if you use the Transaction pooler (port 6543)

`pipeline/database.py` sets `statement_cache_size=100`. Supabase's **transaction**
pooler does **not** support prepared statements, so that pool will error. Two options:

- **Simplest:** use the **Session pooler** or **Direct** connection (port 5432) in
  `DATABASE_URL` — no code change needed.
- **If you must use the transaction pooler (6543)** (e.g. for many short-lived
  connections), set `statement_cache_size=0` in `pipeline/database.py`:

  ```python
  pool = await asyncpg.create_pool(
      dsn=settings.database_url,
      min_size=settings.db_pool_min,
      max_size=settings.db_pool_max,
      statement_cache_size=0,   # ← required for Supabase transaction pooler
      max_inactive_connection_lifetime=300,
      init=init_connection,
  )
  ```

The `init_connection` hook runs `SET hnsw.ef_search` and `SET jit = on` per
connection — both work fine on Supabase.

## Step 6 — Verify the app against Supabase

```bash
source venv/bin/activate
uvicorn api.main:app --reload --port 8000
curl -s http://localhost:8000/health   # expect candidates≈15000, chunks≈31800
```

Then run a search in the Talent UI (`http://localhost:8000/talent`) and confirm
results + scores come back.

---

## Free-tier caveats to remember

- **Idle pause:** free projects pause after **7 days** of inactivity; the first
  request after wakes it (a few-second cold start).
- **2 active projects** max per org.
- **500 MB RAM shared:** the 124 MB-class HNSW index stays hot enough for the
  subset; fine for dev/demo.
- **Egress 5 GB/mo** — irrelevant for normal dev use.

## If you later want the FULL dataset on free tier

You'd need to shave ~240 MB. Combine: `vector(384)→halfvec(384)` (−110 MB),
drop `candidate_documents.raw_text` (−66 MB), LZ4 TOAST compression / drop the
STORED `content_tsv` (−50–90 MB). It lands ~470–490 MB — tight. Easier to just
use **Supabase Pro** ($25/mo, 8 GB) if the full set must be live.
