-- 013_outcome_reasons.sql
-- Optional recruiter explanation for accept/reject outcome feedback.

ALTER TABLE search_outcomes
  ADD COLUMN IF NOT EXISTS reason TEXT;

