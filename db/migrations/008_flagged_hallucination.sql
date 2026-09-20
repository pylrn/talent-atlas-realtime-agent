-- 008_flagged_hallucination.sql
-- Drop the old CHECK constraint on search_outcomes.action and add a new one supporting 'flagged_hallucination'.

ALTER TABLE search_outcomes DROP CONSTRAINT IF EXISTS search_outcomes_action_check;

ALTER TABLE search_outcomes ADD CONSTRAINT search_outcomes_action_check CHECK (
    action IN ('viewed', 'saved', 'contacted', 'shortlisted', 'archived', 'rejected', 'flagged_hallucination')
);
