-- 016_paradedb_pg_search_bm25.sql
-- Optional ParadeDB/pg_search BM25 index for the keyword branch.
--
-- This migration is intentionally safe on the default pgvector Docker image
-- and on managed Postgres instances that do not expose pg_search: it checks
-- pg_available_extensions first and skips itself when pg_search is absent.
-- Run this on:
--   A) a self-managed Postgres/ParadeDB primary, or
--   B) the ParadeDB logical replica when Supabase remains the primary DB.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM pg_available_extensions
        WHERE name = 'pg_search'
    ) THEN
        EXECUTE 'CREATE EXTENSION IF NOT EXISTS pg_search';

        IF NOT EXISTS (
            SELECT 1
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = 'idx_chunks_bm25'
              AND n.nspname = 'public'
        ) THEN
            EXECUTE $idx$
                CREATE INDEX idx_chunks_bm25
                ON document_chunks
                USING bm25 (id, content)
                WITH (key_field = 'id')
            $idx$;
        END IF;
    ELSE
        RAISE NOTICE 'pg_search extension is not available; skipping idx_chunks_bm25';
    END IF;
END $$;
