-- 010_search_history.sql
-- Permanent log of user-triggered searches from the main panel.
-- Agent intermediate searches are NOT logged here (in-memory only).

CREATE TABLE IF NOT EXISTS search_history (
    id           BIGSERIAL PRIMARY KEY,
    recruiter_id TEXT        NOT NULL,
    query        TEXT,
    filters_json JSONB       DEFAULT '{}'::jsonb,
    results_json JSONB       DEFAULT '[]'::jsonb,
    latency_ms   INT,
    timestamp    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS search_history_recruiter_ts
    ON search_history (recruiter_id, timestamp DESC);

-- Read-only role for the Tier-3 ad-hoc SQL tool.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_readonly') THEN
        CREATE ROLE agent_readonly NOLOGIN;
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'agent_ro_user') THEN
        CREATE USER agent_ro_user PASSWORD 'changeme' IN ROLE agent_readonly;
    END IF;
END
$$;
