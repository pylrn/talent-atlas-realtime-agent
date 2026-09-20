-- 007_langfuse_trace_ids.sql
-- Store the Langfuse trace id alongside each impression so that recruiter
-- feedback (/outcomes) can post scores back to the originating trace.

ALTER TABLE search_impressions
  ADD COLUMN IF NOT EXISTS langfuse_trace_id TEXT;

CREATE INDEX IF NOT EXISTS idx_impressions_langfuse_trace
  ON search_impressions (langfuse_trace_id)
  WHERE langfuse_trace_id IS NOT NULL;
