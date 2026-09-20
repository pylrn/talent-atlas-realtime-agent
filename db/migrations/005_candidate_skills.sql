-- 005_candidate_skills.sql
-- Normalized per-skill table with last_used_at for skill-recency ranking.
--
-- The candidates.skills TEXT[] array stays as the canonical list (kept
-- in sync by the ingest pipeline). This table adds a per-skill timestamp
-- that the feature ranker reads to compute the skill_recency signal.

CREATE TABLE IF NOT EXISTS candidate_skills (
    candidate_id  UUID NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    skill         TEXT NOT NULL,
    last_used_at  TIMESTAMPTZ,
    source        TEXT,                          -- 'resume' | 'manual' | 'inferred'
    PRIMARY KEY (candidate_id, skill)
);

CREATE INDEX IF NOT EXISTS idx_candidate_skills_skill
    ON candidate_skills (skill);

CREATE INDEX IF NOT EXISTS idx_candidate_skills_last_used
    ON candidate_skills (candidate_id, last_used_at DESC);

-- ══════════════════════════════════════════
-- BACKFILL: explode candidates.skills[] into one row per skill,
-- defaulting last_used_at to the candidate's updated_at (best effort).
-- Re-runnable: ON CONFLICT DO NOTHING preserves existing timestamps.
-- ══════════════════════════════════════════
INSERT INTO candidate_skills (candidate_id, skill, last_used_at, source)
SELECT
    c.id,
    LOWER(TRIM(s)),
    COALESCE(c.updated_at, c.created_at, NOW()),
    'backfill'
FROM candidates c
CROSS JOIN LATERAL UNNEST(c.skills) AS s
WHERE c.skills IS NOT NULL AND array_length(c.skills, 1) > 0
ON CONFLICT (candidate_id, skill) DO NOTHING;
