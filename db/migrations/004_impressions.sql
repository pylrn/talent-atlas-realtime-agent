-- 004_impressions.sql
-- Impression and outcome tables for feedback-loop / LTR training data

-- ══════════════════════════════════════════
-- SEARCH IMPRESSIONS — one row per candidate shown per search
-- ══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS search_impressions (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    search_id     UUID,
    recruiter_id  UUID,
    candidate_id  UUID NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    position      INT  NOT NULL,
    spec_hash     TEXT,
    final_score   REAL,
    shown_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_impressions_candidate  ON search_impressions (candidate_id);
CREATE INDEX IF NOT EXISTS idx_impressions_recruiter  ON search_impressions (recruiter_id, shown_at);
CREATE INDEX IF NOT EXISTS idx_impressions_search     ON search_impressions (search_id);

-- ══════════════════════════════════════════
-- SEARCH OUTCOMES — recruiter actions on impressions
-- ══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS search_outcomes (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    impression_id UUID REFERENCES search_impressions(id) ON DELETE CASCADE,
    action        TEXT NOT NULL CHECK (
                    action IN ('viewed','saved','contacted','shortlisted','archived','rejected')
                  ),
    occurred_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_outcomes_impression ON search_outcomes (impression_id);
CREATE INDEX IF NOT EXISTS idx_outcomes_action     ON search_outcomes (action);

-- ══════════════════════════════════════════
-- RECRUITER PREFERENCES — per-recruiter ranking config
-- ══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS recruiter_preferences (
    recruiter_id                  UUID PRIMARY KEY,
    prioritize_recent_experience  BOOLEAN DEFAULT TRUE,
    prioritize_exact_skill_match  BOOLEAN DEFAULT TRUE,
    diversity                     BOOLEAN DEFAULT TRUE,
    weight_overrides_json         JSONB   DEFAULT '{}'::jsonb,
    updated_at                    TIMESTAMPTZ DEFAULT NOW()
);
