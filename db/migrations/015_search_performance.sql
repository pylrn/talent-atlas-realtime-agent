-- 015_search_performance.sql
-- Safe search indexes that work on regular Postgres/Supabase.

-- The runtime city filter normalizes hyphens/case:
--   LOWER(REPLACE(city, '-', ' ')) = LOWER(REPLACE($n, '-', ' '))
-- A plain btree on city cannot serve that expression, so keep a matching
-- functional index for narrow city searches.
CREATE INDEX IF NOT EXISTS idx_candidates_city_normalized
    ON candidates (LOWER(REPLACE(city, '-', ' ')));
