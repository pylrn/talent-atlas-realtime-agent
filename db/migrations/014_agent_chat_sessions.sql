-- Persistent user-facing agent chat sessions for /talent.
CREATE TABLE IF NOT EXISTS agent_chat_sessions (
    recruiter_id uuid NOT NULL,
    session_id text NOT NULL,
    title text NOT NULL DEFAULT 'Untitled session',
    summary text NOT NULL DEFAULT '',
    messages_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    context_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    PRIMARY KEY (recruiter_id, session_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_chat_sessions_recruiter_updated
    ON agent_chat_sessions (recruiter_id, updated_at DESC);
