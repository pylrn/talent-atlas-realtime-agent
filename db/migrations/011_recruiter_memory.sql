-- 011_recruiter_memory.sql
-- Unified per-recruiter memory: confirmed facts (ex personalization_hints)
-- + profiler-derived behavioral observations awaiting confirmation.

CREATE TABLE IF NOT EXISTS recruiter_memory (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    recruiter_id     UUID NOT NULL,
    kind             TEXT NOT NULL CHECK (kind IN ('fact','observation')),
    category         TEXT NOT NULL DEFAULT 'other' CHECK (category IN
                       ('skill','location','seniority','company_stage',
                        'work_style','salary','other')),
    content          TEXT NOT NULL,
    content_key      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active' CHECK (status IN
                       ('active','dismissed','expired','promoted')),
    confidence       REAL NOT NULL DEFAULT 1.0,
    evidence_count   INT  NOT NULL DEFAULT 1,
    evidence         JSONB NOT NULL DEFAULT '[]'::jsonb,
    source           TEXT NOT NULL DEFAULT 'manual' CHECK (source IN
                       ('manual','suggested','agent','profiler')),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_evidence_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_surfaced_at TIMESTAMPTZ
);

-- One ACTIVE row per (recruiter, kind, content_key); dismissed/expired
-- rows keep history so the profiler can skip dismissed keys forever.
CREATE UNIQUE INDEX IF NOT EXISTS recruiter_memory_active_key
    ON recruiter_memory (recruiter_id, kind, content_key)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS recruiter_memory_recruiter
    ON recruiter_memory (recruiter_id, kind, status);

-- Profiler staleness marker.
ALTER TABLE recruiter_preferences
    ADD COLUMN IF NOT EXISTS last_profiled_at TIMESTAMPTZ;

-- Backfill: copy existing hints as confirmed facts. content_key from text.
INSERT INTO recruiter_memory (recruiter_id, kind, category, content, content_key,
                              status, confidence, source, created_at)
SELECT rp.recruiter_id,
       'fact',
       'other',
       LEFT(h->>'text', 200),
       'legacy:' || MD5(LOWER(h->>'text')),
       'active',
       1.0,
       CASE WHEN h->>'source' IN ('manual','suggested','agent')
            THEN h->>'source' ELSE 'manual' END,
       COALESCE((h->>'created_at')::timestamptz, NOW())
FROM recruiter_preferences rp,
     jsonb_array_elements(COALESCE(rp.personalization_hints, '[]'::jsonb)) h
WHERE h->>'text' IS NOT NULL AND h->>'text' <> ''
ON CONFLICT DO NOTHING;
