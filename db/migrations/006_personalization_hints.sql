ALTER TABLE recruiter_preferences
  ADD COLUMN IF NOT EXISTS personalization_enabled BOOLEAN DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS personalization_hints   JSONB   DEFAULT '[]'::jsonb;
