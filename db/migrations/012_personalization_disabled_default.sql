-- 012_personalization_disabled_default.sql
-- New recruiters should explicitly opt in before confirmed memory facts
-- influence planning or agent behavior.

ALTER TABLE recruiter_preferences
  ALTER COLUMN personalization_enabled SET DEFAULT FALSE;
