-- 009_relevance_judgments.sql
-- Per-candidate relevance verdicts (👍/👎) for retrieval-quality evaluation.
-- Distinct from search_outcomes (hiring-workflow actions): a relevance judgment
-- answers "did this result match the query?", independent of the hiring decision.

CREATE TABLE IF NOT EXISTS relevance_judgments (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    impression_id UUID NOT NULL REFERENCES search_impressions(id) ON DELETE CASCADE,
    relevant      BOOLEAN NOT NULL,            -- true = 👍 relevant, false = 👎 not relevant
    judged_by     UUID,                        -- recruiter_id, if known
    judged_at     TIMESTAMPTZ DEFAULT NOW(),
    -- One verdict per (candidate, search). Re-clicking overwrites via upsert.
    UNIQUE (impression_id)
);

CREATE INDEX IF NOT EXISTS idx_relevance_impression ON relevance_judgments (impression_id);
CREATE INDEX IF NOT EXISTS idx_relevance_relevant   ON relevance_judgments (relevant);
