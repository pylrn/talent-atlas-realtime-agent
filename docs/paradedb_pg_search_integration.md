# ParadeDB pg_search Integration

This repo now supports two keyword-search backends:

- `BM25_BACKEND=fts` keeps the existing Postgres `tsvector` + `ts_rank_cd` path.
- `BM25_BACKEND=paradedb` uses ParadeDB/`pg_search` BM25 ranking via `content ||| ...`
  and `pdb.score(id)`.

The default is still `fts` so local Docker, plain Postgres, and Supabase primary
databases keep working until a ParadeDB target is ready.

## Why This Exists

The old file name said BM25, but the query was really Postgres full-text search:

```sql
WHERE content_tsv @@ to_tsquery(...)
ORDER BY ts_rank_cd(content_tsv, to_tsquery(...))
```

The GIN index helps find matching chunks, but it does not return the best ranked
rows in score order. For broad keyword queries, Postgres can still fetch and score
a large match set before it can satisfy `ORDER BY ... LIMIT`. That is why vague
country-only or generic role searches could jump from hundreds of milliseconds to
several seconds.

ParadeDB's `pg_search` index stores a real BM25 search index inside Postgres. The
keyword branch can ask the index for score-ordered top-k rows, so the branch is
bounded by requested result count much more than by total match-set size.

References:

- ParadeDB full-text overview: https://docs.paradedb.com/documentation/full-text/overview
- ParadeDB match operators: https://docs.paradedb.com/documentation/full-text/match
- ParadeDB index DDL: https://docs.paradedb.com/documentation/indexing/create-index
- Supabase + ParadeDB pattern: https://supabase.com/partners/integrations/paradedb

## Deployment Shapes

### Branch A: Self-Managed Postgres / ParadeDB

Run the app against one database that has both `pgvector` and `pg_search`.

```text
API/backend -> Postgres/ParadeDB
                 candidates
                 candidate_documents
                 document_chunks
                 HNSW vector index
                 BM25 pg_search index
```

Use one DSN:

```env
DATABASE_URL=postgresql://...
SEARCH_DATABASE_URL=
BM25_BACKEND=paradedb
```

### Branch B: Supabase Primary + ParadeDB Search Replica

Supabase remains the system of record for writes. ParadeDB runs as a logical
replica for search reads.

```text
Writes / ingest -> Supabase primary
                      |
                      | logical replication
                      v
Search reads  -> ParadeDB replica
```

Use two DSNs:

```env
DATABASE_URL=postgresql://...supabase...
SEARCH_DATABASE_URL=postgresql://...paradedb...
BM25_BACKEND=paradedb
```

The backend keeps writes, personalization, search history, and impressions on
`DATABASE_URL`. Dense retrieval, keyword retrieval, skill retrieval, filter-only
search, result hydration, and candidate enrichment use `SEARCH_DATABASE_URL` when
it is configured.

The ParadeDB replica must contain these tables:

- `candidates`
- `candidate_documents`
- `document_chunks`

Add other read-heavy helper tables later if you want agent tools such as
`list_skills` to move fully onto the replica.

## Schema Changes

Safe on every database:

- `db/migrations/015_search_performance.sql` adds a functional city index matching
  the runtime city filter expression.

Optional ParadeDB index:

- `db/migrations/016_paradedb_pg_search_bm25.sql` checks `pg_available_extensions`
  for `pg_search`. If the extension is unavailable, it prints a notice and skips.
  If available, it creates:

```sql
CREATE EXTENSION IF NOT EXISTS pg_search;

CREATE INDEX idx_chunks_bm25
ON document_chunks
USING bm25 (id, content)
WITH (key_field = 'id');
```

This means the default local `pgvector/pgvector:pg17` container does not break.

## Runtime Changes

`pipeline/database.py`

- Adds a separate search pool with `get_search_pool()`.
- Falls back to the primary pool when `SEARCH_DATABASE_URL` is empty.
- Supports optional `SEARCH_DB_POOL_MIN` and `SEARCH_DB_POOL_MAX`.

`pipeline/search.py`

- Accepts `search_pool`.
- Runs retrieval/count/hydration/enrichment search reads on `search_pool`.
- Keeps primary-only data on `pool`.
- Adds backend/pool metadata to `retrieval_policy`.

`pipeline/retrieve_bm25.py`

- Keeps the old FTS SQL under `backend="fts"`.
- Adds `backend="paradedb"` SQL:

```sql
WHERE dc.content ||| $query
ORDER BY pdb.score(dc.id) DESC
```

- Broad filters use index-first over-fetch, then candidate filtering.
- Narrow filters use candidate-first filtering so selective hard filters do not
  lose recall.

`pipeline/keyword_policy.py`

- With `BM25_BACKEND=fts`, broad/common keyword queries can still be skipped or
  timeout-capped.
- With `BM25_BACKEND=paradedb`, auto mode runs keyword retrieval for any usable
  keyword query because the BM25 branch is bounded by the index.

`pipeline/retrieve_dense.py`

- Sets `hnsw.ef_search` per dense query to at least `dense_top_k`. This prevents
  asking for 100-200 dense rows while the HNSW candidate list is still capped at
  the global default of 40.

## Expected Latency Shape

With `BM25_BACKEND=fts`:

- Dense is usually bounded.
- Skill is usually bounded.
- Keyword can become the limiting branch when broad terms match many chunks.

With `BM25_BACKEND=paradedb`:

- Keyword p90/p99 should collapse because ranking comes from the BM25 index.
- Dense and network round trips are more likely to become the remaining limiters.
- Broad keyword queries should preserve recall better than skipping FTS entirely.

The exact latency still depends on:

- database region,
- warm vs cold DB/cache state,
- HNSW `ef_search`,
- candidate filter selectivity,
- replication lag if using Supabase + ParadeDB,
- cross-encoder mode.

## Rollout Checklist

1. Stand up ParadeDB or a self-managed Postgres image with `pg_search`.
2. Ensure `pgvector` is available too if dense retrieval should run there.
3. In Supabase Branch B, create logical replication for `candidates`,
   `candidate_documents`, and `document_chunks`.
4. Run the migrations on the ParadeDB target.
5. Set `SEARCH_DATABASE_URL` and keep `BM25_BACKEND=fts` first to validate normal
   search reads against the replica.
6. Flip `BM25_BACKEND=paradedb`.
7. Re-run the no-cache benchmark set and compare:
   - retrieval p50/p90/p99,
   - keyword branch timing,
   - top-5 overlap and obvious target recall,
   - zero-result cases under selective filters.
8. Keep the old GIN `idx_chunks_content_fts` until the ParadeDB backend is proven.

## Failure Modes

`pg_search` missing:

- Migration skips the BM25 index.
- If `BM25_BACKEND=paradedb`, keyword SQL will fail and return no keyword rows.
- Fix: install/run ParadeDB or set `BM25_BACKEND=fts`.

Replica missing tables:

- Search reads fail at count/retrieval/enrichment.
- Fix: include all required tables in the publication/subscription.

Replica lag:

- Recently ingested candidates may not appear in search immediately.
- Fix: monitor subscription lag and keep ingest/status-sensitive reads on primary
  if you need read-after-write behavior.

Too few results after over-fetch:

- This can happen on broad global BM25 hits plus a very selective candidate filter.
- Current code uses candidate-first SQL for narrow filters. If a remaining broad
  filter still drops too many rows, raise `BM25_OVERFETCH_FACTOR` or denormalize
  common filters onto `document_chunks` and include them in the BM25 index.
