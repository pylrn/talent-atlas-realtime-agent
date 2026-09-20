# Recruiter Memory

One table — `recruiter_memory` — holds everything the system remembers about a
recruiter. Two kinds of entries share one lifecycle:

| kind | what it is | who creates it | where it's used |
|---|---|---|---|
| `fact` | confirmed preference | manual (settings), planner offer accept, agent `save_hint`, promoted observation | planner soft bias + agent prompt |
| `observation` | behavioral hypothesis with confidence | profiler (`pipeline/profiler.py`) | agent prompt only, confirmation-gated |

## Lifecycle

behavior → profiler → observation (active) → agent asks once → fact (yes) or
dismissed-forever (no). Observations expire after 30 days without new evidence.
Caps: 20 active facts, 10 active observations.

## Profiler signals (deterministic, no LLM)

- Filter repetition: same city/country/min_years in ≥60% of last 20 searches (min 5).
- Taste-term repetition: lexicon words (startup, intern, remote, …) in ≥40% of queries (min 4).
- Outcome skew: skill lift ≥1.8× among accepted vs shown (min 6 positive outcomes);
  accepted-vs-shown experience gap ≥2.5 years.

Confidence = `min(0.95, consistency × n/(n+3))` — never displayed as certain.

Runs in the background on the first agent turn of a session (if >1h stale) or via
`POST /api/recruiter/{id}/memory/profile`.

## Usage contract

Facts: soft bias only (`should.*`, `semantic_query`); the current query always wins;
never hard filters. Observations: never applied to searches; the agent may ask about
at most ONE per session (confidence ≥0.7, 7-day re-surface cooldown); "no" is final.

## Endpoints

- `GET  /api/recruiter/{id}/memory` — facts + observations
- `DELETE /api/recruiter/{id}/memory/{mid}` — dismiss an entry
- `POST /api/recruiter/{id}/memory/profile` — run profiler now
- Legacy `GET /personalization`, `POST /hints` keep their shapes, backed by this table.
