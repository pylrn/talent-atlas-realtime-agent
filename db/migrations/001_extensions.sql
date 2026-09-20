-- 001_extensions.sql
-- Enable required PostgreSQL extensions

CREATE EXTENSION IF NOT EXISTS vector;          -- pgvector for embeddings
CREATE EXTENSION IF NOT EXISTS pg_trgm;         -- trigram matching (fuzzy text)
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";     -- UUID generation fallback
