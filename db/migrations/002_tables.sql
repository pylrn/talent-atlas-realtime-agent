-- 002_tables.sql
-- Core schema for the hybrid search hiring platform

-- ══════════════════════════════════════════
-- CANDIDATES — structured, filterable data
-- ══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS candidates (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name     TEXT NOT NULL,
    email         TEXT UNIQUE,
    age           INTEGER CHECK (age >= 16 AND age <= 100),
    location      TEXT,
    city          TEXT,
    country       TEXT,
    interests     TEXT[] DEFAULT '{}',
    skills        TEXT[] DEFAULT '{}',
    years_exp     INTEGER DEFAULT 0 CHECK (years_exp >= 0),
    salary_min    INTEGER,
    salary_max    INTEGER,
    status        TEXT DEFAULT 'active' CHECK (status IN ('active', 'archived', 'hired', 'rejected')),
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- Trigger to auto-update `updated_at`
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_candidates_updated_at
    BEFORE UPDATE ON candidates
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ══════════════════════════════════════════
-- CANDIDATE DOCUMENTS — raw unstructured text
-- ══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS candidate_documents (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id  UUID NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    doc_type      TEXT NOT NULL CHECK (doc_type IN ('transcript', 'certification', 'resume', 'bio', 'cover_letter', 'other')),
    title         TEXT,
    raw_text      TEXT NOT NULL,
    file_hash     TEXT,                     -- SHA-256 of raw_text for deduplication
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ══════════════════════════════════════════
-- DOCUMENT CHUNKS + EMBEDDINGS — the search layer
-- ══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS document_chunks (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_id  UUID NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    document_id   UUID NOT NULL REFERENCES candidate_documents(id) ON DELETE CASCADE,
    chunk_index   INTEGER NOT NULL,
    content       TEXT NOT NULL,
    embedding     VECTOR(384),              -- must match default local embedding dimensions
    token_count   INTEGER,
    metadata      JSONB DEFAULT '{}',
    -- Generated tsvector for full-text/keyword search (BM25-style)
    content_tsv   TSVECTOR GENERATED ALWAYS AS (to_tsvector('english', content)) STORED,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
