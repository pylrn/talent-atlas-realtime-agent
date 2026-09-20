-- 003_indexes.sql
-- All indexes — structured filters + vector similarity + full-text search

-- ══════════════════════════════════════════
-- STAGE 1: Structured filter indexes (B-tree, GIN)
-- ══════════════════════════════════════════
CREATE INDEX IF NOT EXISTS idx_candidates_country    ON candidates (country);
CREATE INDEX IF NOT EXISTS idx_candidates_city       ON candidates (city);
CREATE INDEX IF NOT EXISTS idx_candidates_location   ON candidates (country, city);
CREATE INDEX IF NOT EXISTS idx_candidates_age        ON candidates (age);
CREATE INDEX IF NOT EXISTS idx_candidates_years_exp  ON candidates (years_exp);
CREATE INDEX IF NOT EXISTS idx_candidates_status     ON candidates (status);
CREATE INDEX IF NOT EXISTS idx_candidates_salary     ON candidates (salary_min, salary_max);

-- GIN indexes for array columns (skills, interests)
CREATE INDEX IF NOT EXISTS idx_candidates_skills     ON candidates USING GIN (skills);
CREATE INDEX IF NOT EXISTS idx_candidates_interests  ON candidates USING GIN (interests);

-- ══════════════════════════════════════════
-- STAGE 2: Vector similarity indexes (HNSW)
-- ══════════════════════════════════════════
-- Primary HNSW index for cosine similarity search
-- m=16: connections per node (default is fine for most use cases)
-- ef_construction=200: build quality (higher = better recall, slower build)
CREATE INDEX IF NOT EXISTS idx_chunks_embedding ON document_chunks
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 200);

-- Supporting indexes for efficient joins during search
CREATE INDEX IF NOT EXISTS idx_chunks_candidate_id ON document_chunks (candidate_id);
CREATE INDEX IF NOT EXISTS idx_chunks_document_id  ON document_chunks (document_id);

-- ══════════════════════════════════════════
-- FULL-TEXT SEARCH index (for keyword/BM25 component)
-- ══════════════════════════════════════════
CREATE INDEX IF NOT EXISTS idx_chunks_content_fts ON document_chunks USING GIN (content_tsv);

-- ══════════════════════════════════════════
-- DOCUMENT indexes
-- ══════════════════════════════════════════
CREATE INDEX IF NOT EXISTS idx_docs_candidate_id ON candidate_documents (candidate_id);
CREATE INDEX IF NOT EXISTS idx_docs_type         ON candidate_documents (doc_type);
CREATE INDEX IF NOT EXISTS idx_docs_file_hash    ON candidate_documents (file_hash);
