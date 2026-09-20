#!/usr/bin/env bash
#
# Migrate a SUBSET of the local hybrid-search DB into a Supabase (cloud) project.
#
# Why a subset? The full local DB is ~736 MB; the Supabase free tier caps a
# project at 500 MB. ~15,000 candidates lands ~362 MB — verified comfortable.
#
# Copies straight from the local Docker Postgres to Supabase. psql + pg_dump run
# INSIDE the container (which has internet access), so you do NOT need Postgres
# client tools on your Mac.
#
# ── IMPORTANT lessons baked in ────────────────────────────────────────────────
#  * Supabase free-tier DIRECT connection (db.<ref>.supabase.co) is IPv6-only;
#    Docker on Mac can't reach it. We use the SESSION POOLER (IPv4), port 5432.
#  * The session-pooler host prefix for newer projects is `aws-1-<region>`
#    (not aws-0). User is `postgres.<project_ref>`.
#  * libpq/psql mis-parses the pooler URI ("unexpected spaces"), so we pass
#    DISCRETE PG* env vars instead of a connection URI.
#  * The chunk COPY hits Supabase's statement_timeout if the HNSW + FTS indexes
#    are built inline. We DROP those indexes, COPY with statement_timeout=0,
#    then REBUILD them.
#
# Prerequisites:
#   1. Local DB up:  docker compose up -d postgres
#   2. Supabase project created; extensions enabled (the script enables them too):
#        vector, pg_trgm, uuid-ossp
#   3. Edit the SUPABASE_* vars below (region prefix, project ref, password).
#      Find region via the dashboard, or the Supabase MCP list_projects.
#
# Usage:  SUPA_PASS='...' SUBSET=15000 ./scripts/migrate_subset_to_supabase.sh
#
set -euo pipefail

# ── Supabase target (SESSION POOLER) ─────────────────────────────────────────
SUPA_HOST="${SUPA_HOST:-aws-1-ap-south-1.pooler.supabase.com}"
SUPA_PORT="${SUPA_PORT:-5432}"
SUPA_REF="${SUPA_REF:-iirckrmrtlpygviryrfa}"
SUPA_USER="postgres.${SUPA_REF}"
SUPA_DB="${SUPA_DB:-postgres}"
SUPA_PASS="${SUPA_PASS:?Set SUPA_PASS to your Supabase database password}"

SUBSET="${SUBSET:-15000}"
CTN="${CTN:-hybrid_search_db}"

# Local source env (inside the container, postgres is on localhost)
L=(-e PGHOST=localhost -e PGUSER=hybrid_user -e PGPASSWORD=hybrid_pass -e PGDATABASE=hiring_platform)
# Supabase target env
S=(-e PGHOST="$SUPA_HOST" -e PGPORT="$SUPA_PORT" -e PGUSER="$SUPA_USER" -e PGDATABASE="$SUPA_DB" -e PGPASSWORD="$SUPA_PASS" -e PGCONNECT_TIMEOUT=20)

src(){ docker exec "${L[@]}" "$CTN" "$@"; }            # run psql/pg_dump against LOCAL
dst(){ docker exec "${S[@]}" -i "$CTN" "$@"; }          # run psql against SUPABASE

echo "▶ Subset: $SUBSET candidates → $SUPA_HOST ($SUPA_REF)"
docker exec "${L[@]}" "$CTN" pg_isready -U hybrid_user -d hiring_platform >/dev/null \
  || { echo "✗ Local container '$CTN' not ready (docker compose up -d postgres)"; exit 1; }
dst psql -c "select 1" >/dev/null || { echo "✗ Cannot reach Supabase pooler"; exit 1; }

# ── 1. Extensions + schema ───────────────────────────────────────────────────
echo "▶ [1/3] Extensions + schema…"
dst psql -q -c "create extension if not exists vector; create extension if not exists pg_trgm; create extension if not exists \"uuid-ossp\";" >/dev/null
src pg_dump --schema-only --no-owner --no-privileges | dst psql -v ON_ERROR_STOP=0 >/dev/null 2>&1
echo "  ✓ schema applied"

SUB="SELECT id FROM candidates ORDER BY id LIMIT $SUBSET"

# ── 2. Small tables (FK order) ───────────────────────────────────────────────
copy_table(){ # $1 select  $2 target(cols)
  echo "  → ${2%%(*}"
  src psql -At -c "\copy ($1) TO STDOUT WITH (FORMAT csv)" \
    | dst psql -v ON_ERROR_STOP=1 -c "\copy $2 FROM STDIN WITH (FORMAT csv)"
}
echo "▶ [2/3] Copying candidates / skills / documents…"
copy_table "SELECT id,full_name,email,age,location,city,country,interests,skills,years_exp,salary_min,salary_max,status,created_at,updated_at FROM candidates WHERE id IN ($SUB)" \
  "candidates(id,full_name,email,age,location,city,country,interests,skills,years_exp,salary_min,salary_max,status,created_at,updated_at)"
copy_table "SELECT candidate_id,skill,last_used_at,source FROM candidate_skills WHERE candidate_id IN ($SUB)" \
  "candidate_skills(candidate_id,skill,last_used_at,source)"
copy_table "SELECT id,candidate_id,doc_type,title,raw_text,file_hash,created_at FROM candidate_documents WHERE candidate_id IN ($SUB)" \
  "candidate_documents(id,candidate_id,doc_type,title,raw_text,file_hash,created_at)"

# ── 3. document_chunks: drop heavy indexes → COPY (timeout off) → rebuild ─────
# content_tsv is GENERATED — omit it; Postgres recomputes on insert.
echo "▶ [3/3] document_chunks (drop indexes → copy → rebuild)…"
src psql -At -c "\copy (SELECT id,candidate_id,document_id,chunk_index,content,embedding,token_count,metadata,created_at FROM document_chunks WHERE candidate_id IN ($SUB)) TO STDOUT WITH (FORMAT csv)" \
| dst psql -v ON_ERROR_STOP=1 \
    -c "SET statement_timeout=0" \
    -c "TRUNCATE document_chunks" \
    -c "DROP INDEX IF EXISTS idx_chunks_embedding, idx_chunks_content_fts, idx_chunks_candidate_id, idx_chunks_document_id" \
    -c "\copy document_chunks(id,candidate_id,document_id,chunk_index,content,embedding,token_count,metadata,created_at) FROM STDIN WITH (FORMAT csv)" \
    -c "CREATE INDEX idx_chunks_candidate_id ON public.document_chunks USING btree (candidate_id)" \
    -c "CREATE INDEX idx_chunks_document_id ON public.document_chunks USING btree (document_id)" \
    -c "CREATE INDEX idx_chunks_content_fts ON public.document_chunks USING gin (content_tsv)" \
    -c "CREATE INDEX idx_chunks_embedding ON public.document_chunks USING hnsw (embedding vector_cosine_ops) WITH (m='16', ef_construction='200')"

# ── Verify ───────────────────────────────────────────────────────────────────
echo "▶ Verify:"
dst psql -P pager=off \
  -c "SELECT 'candidates' t,count(*) FROM candidates UNION ALL SELECT 'candidate_skills',count(*) FROM candidate_skills UNION ALL SELECT 'candidate_documents',count(*) FROM candidate_documents UNION ALL SELECT 'document_chunks',count(*) FROM document_chunks;" \
  -c "SELECT pg_size_pretty(pg_database_size(current_database())) AS supabase_db_size;"
echo "✓ Done. Point the app at Supabase — see docs/supabase_migration_runbook.md (§5)."
