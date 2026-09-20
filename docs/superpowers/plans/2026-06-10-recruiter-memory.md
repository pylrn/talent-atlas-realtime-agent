# Unified Recruiter Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Merge `personalization_hints` and behavioral signals (impressions/outcomes/search history) into one `recruiter_memory` table with a two-kind lifecycle — confirmed **facts** that bias searches, and profiler-derived **observations** that the agent surfaces for confirmation — governed by strict usage rules so memory aids instead of distracts.

**Architecture:** One Postgres table `recruiter_memory` holds both kinds. A deterministic (no-LLM) profiler in `pipeline/profiler.py` aggregates `search_history`, `search_impressions`, and `search_outcomes` into observations with computed confidence. `pipeline/memory.py` loads memory and renders prompt blocks with the usage contract. The search planner consumes facts only (same soft-bias rules as today); the agent consumes facts + top observations and gets a `confirm_observation` tool that promotes an observation to a fact (yes) or dismisses it forever (no). The legacy hints endpoints keep their response shapes but read/write the new table; existing hints are backfilled by migration.

**Tech Stack:** Postgres (asyncpg), FastAPI, PydanticAI, vanilla JS settings page, pytest.

---

## Memory model

```
behavior (accept/reject, filters, query terms)
        │  deterministic profiler (SQL aggregation, no LLM)
        ▼
observation  (kind='observation', confidence 0..0.95, evidence[])
        │  agent asks ONE question, user says yes/no
        ▼ yes                                    ▼ no
fact (kind='fact', confidence 1.0)         status='dismissed' (never recreated)
        │
        ├── planner: soft bias on should.*/semantic_query (existing rules)
        └── agent prompt: "saved preferences" block
```

Row shape (one table, both kinds):

| column | meaning |
|---|---|
| `kind` | `'fact'` (confirmed) or `'observation'` (hypothesis) |
| `category` | `skill, location, seniority, company_stage, work_style, salary, other` |
| `content` | one sentence ≤200 chars, e.g. "Tends to accept candidates with React" |
| `content_key` | normalized dedup slug, e.g. `skill:react`, `filter:city=bangalore` |
| `status` | `active / dismissed / expired / promoted` |
| `confidence` | facts: 1.0; observations: `min(0.95, consistency * n/(n+3))` |
| `evidence_count`, `evidence` | how many times seen + last 10 `{type, detail, at}` |
| `source` | `manual / suggested / agent / profiler` |
| `last_surfaced_at` | when the agent last asked about it (anti-nag: 7-day cooldown) |

## The usage contract (the anti-distraction rules)

These exact rules ship as constants in `pipeline/memory.py` and are injected into prompts:

**Planner (facts only — unchanged from today's hint rules):** soft bias on `should.*`, `lexical_terms`, `semantic_query`, `hyde_profile` only; never `must.*`; current query always wins.

**Agent:**
1. Facts are defaults for soft preferences. Mention one only when it changes what you do.
2. Observations are NEVER applied to a search. The only allowed action is asking for confirmation.
3. At most ONE observation question per session, only when relevant to the current request, and only if `confidence ≥ 0.7`.
4. On "yes" → `confirm_observation(id, accept=true)`; it becomes a fact from the next search. On "no" → `accept=false`; never mention it again.
5. Never enumerate memory unprompted. If asked "what do you know about me", list facts, then observations with confidence.

**Caps:** ≤20 active facts (reject new with 400, same as today), ≤10 active observations (profiler evicts lowest-confidence). Observations with no new evidence for 30 days → `expired`.

---

### Task 1: Migration — `recruiter_memory` table + backfill

**Files:**
- Create: `db/migrations/011_recruiter_memory.sql`

- [ ] **Step 1: Write the migration**

```sql
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
```

- [ ] **Step 2: Apply against the running DB**

Run: `docker exec -i hybrid_search_db psql -U hybrid_user -d hiring_platform < "db/migrations/011_recruiter_memory.sql"`
Expected: `CREATE TABLE / CREATE INDEX / ALTER TABLE / INSERT 0 N` with no errors.

- [ ] **Step 3: Verify backfill**

Run: `docker exec hybrid_search_db psql -U hybrid_user -d hiring_platform -c "SELECT kind, status, source, count(*) FROM recruiter_memory GROUP BY 1,2,3;"`
Expected: one `fact | active` row per pre-existing hint (0 rows is fine if no hints existed).

- [ ] **Step 4: Commit**

```bash
git add db/migrations/011_recruiter_memory.sql
git commit -m "feat(db): recruiter_memory table unifying hints + behavioral observations"
```

---

### Task 2: `pipeline/memory.py` — load + prompt blocks + contract text

**Files:**
- Create: `pipeline/memory.py`
- Test: `tests/test_memory.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_memory.py
from pipeline.memory import (
    MemoryFact, MemoryObservation, build_agent_memory_block,
)


def _fact(text):
    return MemoryFact(id="f1", content=text, category="other", source="manual")


def _obs(text, conf, id="o1"):
    return MemoryObservation(id=id, content=text, category="skill",
                             confidence=conf, evidence_count=5)


def test_agent_block_empty_when_no_memory():
    assert build_agent_memory_block([], []) == ""


def test_agent_block_lists_facts_and_rules():
    block = build_agent_memory_block([_fact("prefers startup experience")], [])
    assert "prefers startup experience" in block
    assert "NEVER applied to a search" in block          # contract present
    assert "ONE observation question per session" in block


def test_agent_block_includes_only_confident_observations():
    obs = [_obs("often accepts React candidates", 0.85, "o1"),
           _obs("weak guess", 0.4, "o2")]
    block = build_agent_memory_block([], obs)
    assert "often accepts React candidates" in block
    assert "85%" in block
    assert "o1" in block                                  # id shown for the tool call
    assert "weak guess" not in block


def test_agent_block_caps_observations_at_three():
    obs = [_obs(f"observation number {i}", 0.9, f"o{i}") for i in range(5)]
    block = build_agent_memory_block([], obs)
    assert sum(f"observation number {i}" in block for i in range(5)) == 3
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_memory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pipeline.memory'`

- [ ] **Step 3: Implement `pipeline/memory.py`**

```python
"""Unified recruiter memory: load rows and render prompt blocks.

Memory has two kinds:
  - fact:        confirmed preference. Feeds the planner (soft bias) and agent.
  - observation: profiler hypothesis. Agent-only, confirmation-gated.

The usage contract lives here so every consumer injects the same rules.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import asyncpg

# Observations below this never reach the agent prompt.
SURFACE_CONFIDENCE = 0.7
MAX_OBSERVATIONS_IN_PROMPT = 3
SURFACE_COOLDOWN_DAYS = 7

AGENT_MEMORY_RULES = """\
RULES for using this memory:
1. Facts are defaults for soft preferences. Mention one only when it changes
   what you actually do in this turn.
2. Observations are unconfirmed guesses. They are NEVER applied to a search.
   The only allowed action is asking the recruiter to confirm one.
3. Ask at most ONE observation question per session, only when it is relevant
   to the current request. Phrase it naturally, e.g. "I've noticed you often
   accept candidates with side projects — want me to lean that way in future
   searches?" Then call confirm_observation with the observation id.
4. If the recruiter says yes → confirm_observation(id, accept=true). If no →
   accept=false, and never bring it up again.
5. Never enumerate this memory unprompted. If asked "what do you know about
   me", list facts first, then observations with their confidence."""


@dataclass
class MemoryFact:
    id: str
    content: str
    category: str = "other"
    source: str = "manual"


@dataclass
class MemoryObservation:
    id: str
    content: str
    category: str
    confidence: float
    evidence_count: int = 1


@dataclass
class MemoryBundle:
    enabled: bool = True
    facts: list[MemoryFact] = field(default_factory=list)
    observations: list[MemoryObservation] = field(default_factory=list)


async def load_memory(pool: asyncpg.Pool, recruiter_id: str) -> MemoryBundle:
    """Fetch active memory. Fail-open: any error returns an empty enabled bundle."""
    if not recruiter_id:
        return MemoryBundle()
    try:
        enabled_row = await pool.fetchrow(
            """SELECT personalization_enabled FROM recruiter_preferences
               WHERE recruiter_id = $1::uuid""", recruiter_id)
        enabled = bool(enabled_row["personalization_enabled"]) if enabled_row else True
        if not enabled:
            return MemoryBundle(enabled=False)
        rows = await pool.fetch(
            """SELECT id, kind, category, content, confidence, evidence_count,
                      last_surfaced_at
               FROM recruiter_memory
               WHERE recruiter_id = $1::uuid AND status = 'active'
               ORDER BY kind, confidence DESC, last_evidence_at DESC""",
            recruiter_id)
    except Exception:
        return MemoryBundle()
    bundle = MemoryBundle(enabled=True)
    for r in rows:
        if r["kind"] == "fact":
            bundle.facts.append(MemoryFact(
                id=str(r["id"]), content=r["content"], category=r["category"]))
        else:
            bundle.observations.append(MemoryObservation(
                id=str(r["id"]), content=r["content"], category=r["category"],
                confidence=float(r["confidence"]),
                evidence_count=int(r["evidence_count"])))
    return bundle


def build_agent_memory_block(
    facts: list[MemoryFact],
    observations: list[MemoryObservation],
) -> str:
    """Render the agent system-prompt memory section, or '' if nothing to say."""
    surfaced = [o for o in observations if o.confidence >= SURFACE_CONFIDENCE]
    surfaced = surfaced[:MAX_OBSERVATIONS_IN_PROMPT]
    if not facts and not surfaced:
        return ""
    lines: list[str] = ["## Recruiter Memory"]
    if facts:
        lines.append("\nConfirmed preferences (facts):")
        lines += [f"- {f.content}" for f in facts]
    if surfaced:
        lines.append("\nUnconfirmed observations (confirmation-gated):")
        lines += [
            f"- [{o.id}] {o.content} (confidence {o.confidence:.0%}, "
            f"seen {o.evidence_count}x)"
            for o in surfaced
        ]
    lines.append("\n" + AGENT_MEMORY_RULES)
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_memory.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add pipeline/memory.py tests/test_memory.py
git commit -m "feat(memory): MemoryBundle loader + agent prompt block with usage contract"
```

---

### Task 3: `pipeline/profiler.py` — deterministic behavior → observations

**Files:**
- Create: `pipeline/profiler.py`
- Test: `tests/test_profiler.py`

Detectors are pure functions over plain dict rows so they unit-test without a DB.
`recruiter_id` types differ across tables (`search_history.recruiter_id` is TEXT,
`search_impressions.recruiter_id` is UUID) — the SQL casts handle it.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_profiler.py
from pipeline.profiler import (
    detect_filter_repetition, detect_query_terms, detect_outcome_skew,
    observation_confidence,
)


def test_confidence_grows_with_evidence_and_consistency():
    low  = observation_confidence(consistency=0.6, evidence_count=3)
    high = observation_confidence(consistency=0.9, evidence_count=12)
    assert 0 < low < high <= 0.95


def test_filter_repetition_fires_at_60_pct_of_min_5():
    rows = [{"filters_json": {"city": "Bangalore"}} for _ in range(6)] + \
           [{"filters_json": {}} for _ in range(4)]
    obs = detect_filter_repetition(rows)
    assert any(o["content_key"] == "filter:city=bangalore" for o in obs)
    assert all(o["category"] == "location" for o in obs
               if o["content_key"].startswith("filter:city"))


def test_filter_repetition_quiet_below_threshold():
    rows = [{"filters_json": {"city": "Pune"}} for _ in range(2)] + \
           [{"filters_json": {}} for _ in range(8)]
    assert detect_filter_repetition(rows) == []


def test_query_terms_only_from_taste_lexicon():
    rows = [{"query": "passionate startup intern python"} for _ in range(5)] + \
           [{"query": "backend engineer"} for _ in range(5)]
    obs = detect_query_terms(rows)
    keys = {o["content_key"] for o in obs}
    assert "term:startup" in keys and "term:intern" in keys
    assert not any(k.startswith("term:python") for k in keys)  # not taste vocab


def test_outcome_skew_skill_lift():
    shown = [{"candidate_id": str(i), "skills": ["java"], "years_exp": 5}
             for i in range(20)]
    for i in range(4):
        shown[i]["skills"] = ["react", "java"]
    outcomes = [{"candidate_id": str(i), "action": "shortlisted"} for i in range(4)] + \
               [{"candidate_id": "10", "action": "shortlisted"},
                {"candidate_id": "11", "action": "shortlisted"}]
    obs = detect_outcome_skew(shown, outcomes)
    react = [o for o in obs if o["content_key"] == "skill:react"]
    assert react and react[0]["category"] == "skill"


def test_outcome_skew_needs_min_positive_outcomes():
    shown = [{"candidate_id": "1", "skills": ["react"], "years_exp": 3}]
    outcomes = [{"candidate_id": "1", "action": "shortlisted"}]
    assert detect_outcome_skew(shown, outcomes) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_profiler.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement `pipeline/profiler.py`**

```python
"""Deterministic behavioral profiler: search/outcome history → observations.

No LLM. Each detector returns candidate observations as plain dicts:
    {category, content, content_key, consistency, evidence_count, evidence}
profile_recruiter() aggregates, computes confidence, and upserts into
recruiter_memory — skipping any content_key the recruiter ever dismissed.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone

import asyncpg

logger = logging.getLogger(__name__)

LOOKBACK_SEARCHES = 20
MIN_SEARCHES = 5
FILTER_RATIO = 0.6
MIN_POSITIVE_OUTCOMES = 6
SKILL_LIFT = 1.8
MAX_ACTIVE_OBSERVATIONS = 10
EXPIRE_AFTER_DAYS = 30
POSITIVE_ACTIONS = ("saved", "contacted", "shortlisted")

# Only these words count as "taste" query terms — keeps junk out.
TASTE_LEXICON = {
    "startup", "scaleup", "scale-up", "enterprise", "agency", "fintech",
    "intern", "internship", "junior", "senior", "staff", "lead", "principal",
    "remote", "hybrid", "onsite", "contract", "freelance",
    "passionate", "hands-on", "research", "open-source", "side-project",
}
_TERM_CATEGORY = {
    "startup": "company_stage", "scaleup": "company_stage",
    "scale-up": "company_stage", "enterprise": "company_stage",
    "agency": "company_stage", "fintech": "company_stage",
    "intern": "seniority", "internship": "seniority", "junior": "seniority",
    "senior": "seniority", "staff": "seniority", "lead": "seniority",
    "principal": "seniority",
    "remote": "work_style", "hybrid": "work_style", "onsite": "work_style",
    "contract": "work_style", "freelance": "work_style",
}


def observation_confidence(consistency: float, evidence_count: int) -> float:
    """Monotone in both inputs, capped at 0.95 so observations never look certain."""
    return round(min(0.95, consistency * evidence_count / (evidence_count + 3)), 3)


def detect_filter_repetition(search_rows: list[dict]) -> list[dict]:
    """Same city/country/min_years_exp filter in >=60% of recent searches."""
    rows = search_rows[:LOOKBACK_SEARCHES]
    if len(rows) < MIN_SEARCHES:
        return []
    counts: Counter[tuple[str, str]] = Counter()
    for r in rows:
        f = r.get("filters_json") or {}
        if isinstance(f, str):
            try:
                f = json.loads(f)
            except json.JSONDecodeError:
                f = {}
        for key in ("city", "country", "min_years_exp"):
            v = f.get(key)
            if v not in (None, "", 0):
                counts[(key, str(v).lower())] += 1
    out = []
    for (key, val), n in counts.items():
        ratio = n / len(rows)
        if ratio >= FILTER_RATIO:
            label = {"city": "location", "country": "location",
                     "min_years_exp": "seniority"}[key]
            content = (f"Usually filters to {key.replace('_', ' ')} = {val} "
                       f"({n} of last {len(rows)} searches)")
            out.append({"category": label, "content": content,
                        "content_key": f"filter:{key}={val}",
                        "consistency": ratio, "evidence_count": n,
                        "evidence": [{"type": "filter_repetition",
                                      "detail": f"{key}={val} in {n}/{len(rows)}"}]})
    return out


def detect_query_terms(search_rows: list[dict]) -> list[dict]:
    """Taste-lexicon words recurring across recent queries (>=40%, min 4)."""
    rows = search_rows[:LOOKBACK_SEARCHES]
    if len(rows) < MIN_SEARCHES:
        return []
    hits: Counter[str] = Counter()
    for r in rows:
        words = {w.strip(".,()").lower() for w in str(r.get("query") or "").split()}
        for t in words & TASTE_LEXICON:
            hits[t] += 1
    out = []
    for term, n in hits.items():
        ratio = n / len(rows)
        if n >= 4 and ratio >= 0.4:
            out.append({"category": _TERM_CATEGORY.get(term, "work_style"),
                        "content": (f"Often searches for '{term}' "
                                    f"({n} of last {len(rows)} queries)"),
                        "content_key": f"term:{term}",
                        "consistency": ratio, "evidence_count": n,
                        "evidence": [{"type": "query_term",
                                      "detail": f"'{term}' in {n}/{len(rows)}"}]})
    return out


def detect_outcome_skew(shown: list[dict], outcomes: list[dict]) -> list[dict]:
    """Accepted-vs-shown skew on skills and years_exp."""
    accepted_ids = {o["candidate_id"] for o in outcomes
                    if o.get("action") in POSITIVE_ACTIONS}
    if len([o for o in outcomes if o.get("action") in POSITIVE_ACTIONS]) \
            < MIN_POSITIVE_OUTCOMES:
        return []
    by_id = {s["candidate_id"]: s for s in shown}
    accepted = [by_id[i] for i in accepted_ids if i in by_id]
    if not accepted or not shown:
        return []
    out = []

    # Skill lift: skill share among accepted vs among all shown.
    shown_skill = Counter(s.lower() for c in shown for s in (c.get("skills") or []))
    acc_skill = Counter(s.lower() for c in accepted for s in (c.get("skills") or []))
    for skill, k in acc_skill.items():
        share_acc = k / len(accepted)
        share_shown = shown_skill[skill] / len(shown)
        if k >= 3 and share_acc >= 0.6 and share_shown > 0 \
                and share_acc / share_shown >= SKILL_LIFT:
            out.append({"category": "skill",
                        "content": (f"Tends to accept candidates with {skill} "
                                    f"({k} of {len(accepted)} accepted)"),
                        "content_key": f"skill:{skill}",
                        "consistency": share_acc, "evidence_count": k,
                        "evidence": [{"type": "outcome_skew",
                                      "detail": f"{skill}: {share_acc:.0%} accepted "
                                                f"vs {share_shown:.0%} shown"}]})

    # Experience skew.
    yrs = [c.get("years_exp") for c in shown if c.get("years_exp") is not None]
    yrs_acc = [c.get("years_exp") for c in accepted if c.get("years_exp") is not None]
    if len(yrs) >= 10 and len(yrs_acc) >= 4:
        avg_shown, avg_acc = sum(yrs) / len(yrs), sum(yrs_acc) / len(yrs_acc)
        if abs(avg_acc - avg_shown) >= 2.5:
            direction = "junior" if avg_acc < avg_shown else "senior"
            out.append({"category": "seniority",
                        "content": (f"Tends to accept more {direction} candidates "
                                    f"(avg {avg_acc:.1f}y accepted vs "
                                    f"{avg_shown:.1f}y shown)"),
                        "content_key": f"seniority:prefers_{direction}",
                        "consistency": min(1.0, abs(avg_acc - avg_shown) / 5),
                        "evidence_count": len(yrs_acc),
                        "evidence": [{"type": "outcome_skew",
                                      "detail": f"avg exp {avg_acc:.1f} vs {avg_shown:.1f}"}]})
    return out


async def profile_recruiter(pool: asyncpg.Pool, recruiter_id: str) -> int:
    """Run all detectors and upsert observations. Returns count written."""
    search_rows = [dict(r) for r in await pool.fetch(
        """SELECT query, filters_json FROM search_history
           WHERE recruiter_id = $1 ORDER BY timestamp DESC LIMIT $2""",
        recruiter_id, LOOKBACK_SEARCHES)]
    shown = [dict(r) for r in await pool.fetch(
        """SELECT DISTINCT ON (c.id) c.id::text AS candidate_id,
                  c.skills, c.years_exp
           FROM search_impressions si JOIN candidates c ON c.id = si.candidate_id
           WHERE si.recruiter_id::text = $1
             AND si.shown_at > NOW() - INTERVAL '90 days'""",
        recruiter_id)]
    outcomes = [dict(r) for r in await pool.fetch(
        """SELECT si.candidate_id::text AS candidate_id, so.action
           FROM search_outcomes so
           JOIN search_impressions si ON si.id = so.impression_id
           WHERE si.recruiter_id::text = $1
             AND so.occurred_at > NOW() - INTERVAL '90 days'""",
        recruiter_id)]

    candidates = (detect_filter_repetition(search_rows)
                  + detect_query_terms(search_rows)
                  + detect_outcome_skew(shown, outcomes))

    dismissed = {r["content_key"] for r in await pool.fetch(
        """SELECT content_key FROM recruiter_memory
           WHERE recruiter_id = $1::uuid AND status = 'dismissed'""",
        recruiter_id)}

    written = 0
    for obs in candidates:
        if obs["content_key"] in dismissed:
            continue
        conf = observation_confidence(obs["consistency"], obs["evidence_count"])
        now = datetime.now(timezone.utc).isoformat()
        for e in obs["evidence"]:
            e["at"] = now
        await pool.execute(
            """INSERT INTO recruiter_memory
                 (recruiter_id, kind, category, content, content_key,
                  confidence, evidence_count, evidence, source)
               VALUES ($1::uuid, 'observation', $2, $3, $4, $5, $6, $7::jsonb,
                       'profiler')
               ON CONFLICT (recruiter_id, kind, content_key) WHERE status='active'
               DO UPDATE SET
                 content = EXCLUDED.content,
                 confidence = EXCLUDED.confidence,
                 evidence_count = EXCLUDED.evidence_count,
                 evidence = (
                   SELECT jsonb_agg(e) FROM (
                     SELECT e FROM jsonb_array_elements(
                       recruiter_memory.evidence || EXCLUDED.evidence) e
                     ORDER BY e->>'at' DESC LIMIT 10) s),
                 last_evidence_at = NOW()""",
            recruiter_id, obs["category"], obs["content"], obs["content_key"],
            conf, obs["evidence_count"], json.dumps(obs["evidence"]))
        written += 1

    # Expire stale observations; evict beyond the cap (lowest confidence first).
    await pool.execute(
        """UPDATE recruiter_memory SET status='expired'
           WHERE recruiter_id = $1::uuid AND kind='observation' AND status='active'
             AND last_evidence_at < NOW() - INTERVAL '%s days'"""
        % EXPIRE_AFTER_DAYS, recruiter_id)
    await pool.execute(
        """UPDATE recruiter_memory SET status='expired'
           WHERE id IN (
             SELECT id FROM recruiter_memory
             WHERE recruiter_id = $1::uuid AND kind='observation' AND status='active'
             ORDER BY confidence DESC, last_evidence_at DESC
             OFFSET $2)""",
        recruiter_id, MAX_ACTIVE_OBSERVATIONS)
    await pool.execute(
        """INSERT INTO recruiter_preferences (recruiter_id, last_profiled_at)
           VALUES ($1::uuid, NOW())
           ON CONFLICT (recruiter_id) DO UPDATE SET last_profiled_at = NOW()""",
        recruiter_id)
    return written


async def profile_recruiter_if_stale(pool: asyncpg.Pool, recruiter_id: str,
                                     max_age_hours: int = 1) -> None:
    """Background-safe wrapper: run only if last run is older than max_age_hours."""
    try:
        row = await pool.fetchrow(
            """SELECT last_profiled_at FROM recruiter_preferences
               WHERE recruiter_id = $1::uuid""", recruiter_id)
        if row and row["last_profiled_at"] is not None:
            age = datetime.now(timezone.utc) - row["last_profiled_at"]
            if age.total_seconds() < max_age_hours * 3600:
                return
        n = await profile_recruiter(pool, recruiter_id)
        logger.info("profiler: %s observations for %s", n, recruiter_id)
    except Exception as exc:
        logger.warning("profiler failed for %s: %s", recruiter_id, exc)
```

Note: the partial unique index `recruiter_memory_active_key` is declared `WHERE status='active'`; the `ON CONFLICT ... WHERE status='active'` clause must match it exactly or Postgres rejects the statement. If `asyncpg` raises `InvalidColumnReferenceError` here, the fallback is `ON CONFLICT (recruiter_id, kind, content_key) WHERE status = 'active'` written as an index predicate — verify against the live DB in the e2e task.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_profiler.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add pipeline/profiler.py tests/test_profiler.py
git commit -m "feat(profiler): deterministic behavior detectors + observation upsert"
```

---

### Task 4: Search pipeline reads facts from `recruiter_memory`

**Files:**
- Modify: `pipeline/search.py` (the `_load_recruiter_hints` method, ~line 508)

The planner keeps its existing hint-block builder, cache tag, and soft-bias rules
untouched — only the data source changes. Return shape stays `(enabled, [{"text": ...}])`.

- [ ] **Step 1: Replace the SQL in `_load_recruiter_hints`**

Replace the body of `_load_recruiter_hints` in `pipeline/search.py` with:

```python
    async def _load_recruiter_hints(self, recruiter_id: str | None) -> tuple[bool, list]:
        """Fetch (personalization_enabled, fact hints) from recruiter_memory.

        Returns (True, []) on miss or any error — personalization stays
        opt-out by data, never opt-out by failure mode.
        """
        if not recruiter_id:
            return True, []
        try:
            enabled_row = await self.pool.fetchrow(
                """SELECT personalization_enabled FROM recruiter_preferences
                   WHERE recruiter_id = $1::uuid""",
                recruiter_id,
            )
            enabled = bool(enabled_row["personalization_enabled"]) if enabled_row else True
            if not enabled:
                return False, []
            rows = await self.pool.fetch(
                """SELECT content FROM recruiter_memory
                   WHERE recruiter_id = $1::uuid AND kind = 'fact'
                     AND status = 'active'
                   ORDER BY created_at DESC LIMIT 20""",
                recruiter_id,
            )
        except Exception as exc:
            logger.debug("recruiter memory fetch failed: %s", exc)
            return True, []
        return enabled, [{"text": r["content"]} for r in rows]
```

- [ ] **Step 2: Run the existing search tests**

Run: `python -m pytest tests/ -k "search or planner or hints" -v`
Expected: PASS (the planner contract — hint dicts with `text` keys — is unchanged).

- [ ] **Step 3: Commit**

```bash
git add pipeline/search.py
git commit -m "refactor(search): planner hints now read from recruiter_memory facts"
```

---

### Task 5: Agent integration — memory in prompt + `confirm_observation` tool

**Files:**
- Modify: `pipeline/agent_tools.py` (rewrite `do_save_hint`, add `do_confirm_observation`)
- Modify: `pipeline/agent.py` (AgentDeps, system prompt, new tool registration)
- Modify: `pipeline/agent_run.py` (`_summarize` entries)
- Modify: `api/main.py` (`/agent/chat` loads memory server-side + fires profiler)

- [ ] **Step 1: Rewrite `do_save_hint` and add `do_confirm_observation` in `pipeline/agent_tools.py`**

Replace the existing `do_save_hint` (the gnarly jsonb-append SQL, ~line 394) with:

```python
async def do_save_hint(
    pool: asyncpg.Pool,
    recruiter_id: str,
    hint_text: str,
) -> dict[str, Any]:
    """Persist a confirmed recruiter preference as a memory fact."""
    hint_text = hint_text.strip()[:200]
    if not hint_text:
        return {"error": "Hint text is empty"}
    n = await pool.fetchval(
        """SELECT count(*) FROM recruiter_memory
           WHERE recruiter_id = $1::uuid AND kind='fact' AND status='active'""",
        recruiter_id)
    if n >= 20:
        return {"error": "Preference cap reached (20). Ask the recruiter to remove one first."}
    await pool.execute(
        """INSERT INTO recruiter_memory
             (recruiter_id, kind, category, content, content_key, source)
           VALUES ($1::uuid, 'fact', 'other', $2, 'agent:' || MD5(LOWER($2)), 'agent')
           ON CONFLICT (recruiter_id, kind, content_key) WHERE status='active'
           DO NOTHING""",
        recruiter_id, hint_text)
    return {"saved": hint_text}


async def do_confirm_observation(
    pool: asyncpg.Pool,
    recruiter_id: str,
    observation_id: str,
    accept: bool,
) -> dict[str, Any]:
    """Recruiter answered an observation question. yes → promote to fact;
    no → dismiss permanently (the profiler will never recreate that key)."""
    row = await pool.fetchrow(
        """SELECT id, category, content, content_key FROM recruiter_memory
           WHERE id = $1::uuid AND recruiter_id = $2::uuid
             AND kind = 'observation' AND status = 'active'""",
        observation_id, recruiter_id)
    if not row:
        return {"error": f"Observation {observation_id} not found or not active"}
    if not accept:
        await pool.execute(
            "UPDATE recruiter_memory SET status='dismissed' WHERE id = $1::uuid",
            observation_id)
        return {"dismissed": row["content"]}
    await pool.execute(
        "UPDATE recruiter_memory SET status='promoted' WHERE id = $1::uuid",
        observation_id)
    await pool.execute(
        """INSERT INTO recruiter_memory
             (recruiter_id, kind, category, content, content_key, source)
           VALUES ($1::uuid, 'fact', $2, $3, $4, 'profiler')
           ON CONFLICT (recruiter_id, kind, content_key) WHERE status='active'
           DO NOTHING""",
        recruiter_id, row["category"], row["content"], row["content_key"])
    return {"promoted_to_fact": row["content"]}
```

- [ ] **Step 2: Update `pipeline/agent.py`**

(a) Import the new helper and memory block builder:

```python
from pipeline.agent_tools import (
    # ... existing imports ...
    do_confirm_observation,
)
from pipeline.memory import build_agent_memory_block, MemoryFact, MemoryObservation
```

(b) In `AgentDeps`, add one field after `hints`:

```python
    observations: list[dict] = field(default_factory=list)  # [{id, content, confidence, evidence_count, category}]
```

(c) In `build_system_prompt`, change the signature to accept observations and
replace the hints block. Change:

```python
def build_system_prompt(
    query: str,
    filters: dict[str, Any],
    result_count: int,
    hints: list[str],
) -> str:
    hints_block = (
        "\n".join(f"- {h}" for h in hints)
        if hints else "No saved preferences yet."
    )
```

to:

```python
def build_system_prompt(
    query: str,
    filters: dict[str, Any],
    result_count: int,
    hints: list[str],
    observations: list[dict] | None = None,
) -> str:
    memory_block = build_agent_memory_block(
        [MemoryFact(id="", content=h) for h in hints],
        [MemoryObservation(id=o["id"], content=o["content"],
                           category=o.get("category", "other"),
                           confidence=o.get("confidence", 0.0),
                           evidence_count=o.get("evidence_count", 1))
         for o in (observations or [])],
    ) or "## Recruiter Memory\nNo saved preferences yet."
```

and in the f-string replace:

```
## This Recruiter's Saved Preferences
{hints_block}
```

with:

```
{memory_block}
```

(d) Update the `_system_prompt` decorator to pass observations:

```python
@recruiter_agent.system_prompt
async def _system_prompt(ctx: RunContext[AgentDeps]) -> str:
    return build_system_prompt(
        query=ctx.deps.query,
        filters=ctx.deps.filters,
        result_count=len(ctx.deps.result_ids),
        hints=ctx.deps.hints,
        observations=ctx.deps.observations,
    )
```

(e) Register the tool (next to `save_hint`):

```python
@recruiter_agent.tool
async def confirm_observation(
    ctx: RunContext[AgentDeps], observation_id: str, accept: bool
) -> str:
    """Record the recruiter's answer to an observation question.

    Args:
        observation_id: The [id] shown next to the observation in your memory block.
        accept: true if the recruiter confirmed the preference, false if rejected.
    """
    return json.dumps(await do_confirm_observation(
        ctx.deps.pool, ctx.deps.recruiter_id, observation_id, accept))
```

(f) In the "Memory & UI tools" section of the tool reference text, add:

```
- `confirm_observation(observation_id, accept)` — record the recruiter's yes/no to an observation question. Only call after they explicitly answer.
```

- [ ] **Step 3: `/agent/chat` loads memory server-side and fires the profiler**

In `api/main.py`'s `agent_chat`, after `session = _session_store.get_or_create(...)`:

```python
    from pipeline.memory import load_memory
    from pipeline.profiler import profile_recruiter_if_stale

    # First turn of a session → refresh the behavioral profile in the background.
    if not session.messages:
        asyncio.create_task(profile_recruiter_if_stale(pool, req.recruiter_id))

    memory = await load_memory(pool, req.recruiter_id)
```

and replace the `hints=` line in `AgentDeps(...)` with:

```python
        hints=[f.content for f in memory.facts],
        observations=[{"id": o.id, "content": o.content, "category": o.category,
                       "confidence": o.confidence, "evidence_count": o.evidence_count}
                      for o in memory.observations],
```

(`import asyncio` is already present in `api/main.py`; verify, add if missing.)

- [ ] **Step 4: `_summarize` entry in `pipeline/agent_run.py`**

Add to `_summarize` before `return "done"`:

```python
    if tool_name == "confirm_observation":
        if "promoted_to_fact" in parsed:
            return "preference saved"
        if "dismissed" in parsed:
            return "observation dismissed"
        return "recorded"
```

- [ ] **Step 5: Import check + tests**

Run: `python -c "from pipeline.agent import recruiter_agent; print('OK')" && python -m pytest tests/test_memory.py tests/test_profiler.py -v`
Expected: `OK`, all tests pass.

- [ ] **Step 6: Commit**

```bash
git add pipeline/agent.py pipeline/agent_tools.py pipeline/agent_run.py api/main.py
git commit -m "feat(agent): memory-aware prompt, confirm_observation tool, profiler trigger"
```

---

### Task 6: API endpoints on the new table

**Files:**
- Modify: `api/main.py` (rewrite hint endpoints' internals; add memory endpoints)

Response shapes of the legacy endpoints are preserved so the settings UI keeps
working; deletion switches from index-based to id-based (settings UI updated in Task 7).

- [ ] **Step 1: Rewrite `get_personalization` (GET `/api/recruiter/{id}/personalization`)**

```python
@app.get("/api/recruiter/{recruiter_id}/personalization")
async def get_personalization(recruiter_id: str):
    """Return (enabled, hints[]) — facts from recruiter_memory."""
    pool = app.state.pool
    row = await pool.fetchrow(
        """SELECT personalization_enabled FROM recruiter_preferences
           WHERE recruiter_id = $1::uuid""", recruiter_id)
    enabled = bool(row["personalization_enabled"]) if row else True
    facts = await pool.fetch(
        """SELECT id, content, source, created_at FROM recruiter_memory
           WHERE recruiter_id = $1::uuid AND kind='fact' AND status='active'
           ORDER BY created_at DESC LIMIT 20""", recruiter_id)
    return {"enabled": enabled,
            "hints": [{"id": str(r["id"]), "text": r["content"],
                       "source": r["source"],
                       "created_at": r["created_at"].isoformat()} for r in facts]}
```

- [ ] **Step 2: Rewrite `add_personalization_hint` (POST `/api/recruiter/{id}/hints`)**

Keep the sanitizer, length check, and Langfuse score block; replace the storage section:

```python
@app.post("/api/recruiter/{recruiter_id}/hints")
async def add_personalization_hint(recruiter_id: str, body: dict):
    """Append a fact. source defaults to 'manual'. 400 on empty/long/cap."""
    text   = _clean_hint_text(str(body.get("text", "")))
    source = body.get("source") if body.get("source") in {"manual", "suggested"} else "manual"
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 200:
        raise HTTPException(status_code=400, detail="text exceeds 200 chars")

    pool = app.state.pool
    n = await pool.fetchval(
        """SELECT count(*) FROM recruiter_memory
           WHERE recruiter_id = $1::uuid AND kind='fact' AND status='active'""",
        recruiter_id)
    if n >= 20:
        raise HTTPException(status_code=400, detail="hint cap reached (20)")
    await pool.execute(
        """INSERT INTO recruiter_memory
             (recruiter_id, kind, category, content, content_key, source)
           VALUES ($1::uuid, 'fact', 'other', $2, $3 || ':' || MD5(LOWER($2)), $3)
           ON CONFLICT (recruiter_id, kind, content_key) WHERE status='active'
           DO NOTHING""",
        recruiter_id, text, source)

    trace_id = body.get("langfuse_trace_id")
    if trace_id and source == "suggested":
        from pipeline.observability import record_score, ScoreName
        record_score(trace_id=trace_id, name=ScoreName.PERSONALIZATION_ACCEPTED,
                     value=1.0, comment=f"Accepted hint: {text}")
    return await get_personalization(recruiter_id)
```

- [ ] **Step 3: Replace index-delete with id-based memory endpoints**

Delete the old `delete_personalization_hint` (`DELETE /hints/{idx}`) and add:

```python
@app.get("/api/recruiter/{recruiter_id}/memory")
async def get_recruiter_memory(recruiter_id: str):
    """Full memory view: facts + active observations with confidence."""
    pool = app.state.pool
    rows = await pool.fetch(
        """SELECT id, kind, category, content, confidence, evidence_count,
                  source, created_at, last_evidence_at
           FROM recruiter_memory
           WHERE recruiter_id = $1::uuid AND status='active'
           ORDER BY kind, confidence DESC, created_at DESC""", recruiter_id)
    out = {"facts": [], "observations": []}
    for r in rows:
        item = {"id": str(r["id"]), "category": r["category"],
                "content": r["content"], "source": r["source"],
                "confidence": float(r["confidence"]),
                "evidence_count": r["evidence_count"],
                "created_at": r["created_at"].isoformat()}
        out["facts" if r["kind"] == "fact" else "observations"].append(item)
    return out


@app.delete("/api/recruiter/{recruiter_id}/memory/{memory_id}")
async def dismiss_memory_entry(recruiter_id: str, memory_id: str):
    """Dismiss any memory entry by id (facts removed, observations never recreated)."""
    pool = app.state.pool
    res = await pool.execute(
        """UPDATE recruiter_memory SET status='dismissed'
           WHERE id = $1::uuid AND recruiter_id = $2::uuid AND status='active'""",
        memory_id, recruiter_id)
    if res == "UPDATE 0":
        raise HTTPException(status_code=404, detail="memory entry not found")
    return await get_recruiter_memory(recruiter_id)


@app.post("/api/recruiter/{recruiter_id}/memory/profile")
async def trigger_profile(recruiter_id: str):
    """Manually run the behavioral profiler (testing / settings page)."""
    from pipeline.profiler import profile_recruiter
    n = await profile_recruiter(app.state.pool, recruiter_id)
    return {"observations_written": n}
```

- [ ] **Step 4: Run API tests**

Run: `python -m pytest tests/ -k "admin or api" -v`
Expected: PASS. If any test asserts on the old `DELETE /hints/{idx}` route or the
hint response shape, update that test to the id-based route — the new shape is a
superset (each hint now also has `id`).

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/
git commit -m "feat(api): memory endpoints; legacy hint routes now back onto recruiter_memory"
```

---

### Task 7: Settings UI — observations section + id-based delete

**Files:**
- Modify: `api/static/settings.html` (personalization card, ~line 457; JS ~lines 824-880)

- [ ] **Step 1: Update the delete handler to use ids**

In the hints list renderer (~line 835), the delete button currently calls
`/hints/${idx}`. Change the handler (~line 845) to:

```javascript
          await api(`/api/recruiter/${encodeURIComponent(pState().recId)}/memory/${h.id}`, { method: "DELETE" });
```

(where `h` is the hint object in the `.map((h, i) => ...)` — it now carries `id`).

- [ ] **Step 2: Add the observations section to the card**

After the `<div id="hintsList">` block (~line 471), add:

```html
      <div style="margin-top:18px">
        <strong style="font-size:13px">Learned observations</strong>
        <div class="muted" style="font-size:12px;margin:2px 0 8px">
          Patterns noticed from your search behavior. The agent may ask to
          confirm one — confirmed observations become saved preferences.
          They never affect searches until confirmed.
        </div>
        <div id="observationsList"></div>
      </div>
```

- [ ] **Step 3: Render observations in the JS**

Next to the existing hints loader (~line 854), extend it to also fetch memory:

```javascript
    async function loadObservations() {
      const s = pState();
      if (!s.recId) return;
      const data = await api(`/api/recruiter/${encodeURIComponent(s.recId)}/memory`);
      const list = document.getElementById("observationsList");
      const obs = data.observations || [];
      if (!obs.length) {
        list.innerHTML = '<div class="muted">Nothing learned yet.</div>';
        return;
      }
      list.innerHTML = obs.map(o => `
        <div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid var(--line,#eee)">
          <span style="flex:1;font-size:13px">${escapeHtml(o.content)}</span>
          <span class="muted" style="font-size:11px">${Math.round(o.confidence * 100)}%</span>
          <button type="button" data-mid="${o.id}" class="obs-dismiss" style="font-size:11px">Dismiss</button>
        </div>`).join("");
      list.querySelectorAll(".obs-dismiss").forEach(btn => {
        btn.addEventListener("click", async () => {
          await api(`/api/recruiter/${encodeURIComponent(s.recId)}/memory/${btn.dataset.mid}`, { method: "DELETE" });
          loadObservations();
        });
      });
    }
```

Call `loadObservations()` wherever the hints loader is called (same trigger,
~line 854 block), and verify an `escapeHtml` helper exists in settings.html —
if not, add the same 5-line helper used in agent-panel.js.

- [ ] **Step 4: Visual check with Playwright**

Run a quick headless check that the section renders:

```bash
python3 - <<'EOF'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(headless=True)
    page = b.new_page()
    page.goto("http://localhost:8000/static/settings.html")
    page.wait_for_load_state("networkidle")
    assert page.locator("#observationsList").count() == 1, "observations section missing"
    print("settings page OK")
    b.close()
EOF
```

Expected: `settings page OK`

- [ ] **Step 5: Commit**

```bash
git add api/static/settings.html
git commit -m "feat(settings): learned-observations section with dismiss; id-based hint delete"
```

---

### Task 8: End-to-end verification against the real system

**Files:** none (verification only)

- [ ] **Step 1: Restart the API server so all changes load**

Restart however the server is currently run (e.g. kill + `uvicorn api.main:app --reload --port 8000`), then:
Run: `curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/static/admin.html`
Expected: `200`

- [ ] **Step 2: Seed behavior and run the profiler for a test recruiter**

```bash
RID="00000000-0000-0000-0000-000000000042"
# 6 searches with a repeated city filter (search_history.recruiter_id is TEXT)
for i in 1 2 3 4 5 6; do
  docker exec hybrid_search_db psql -U hybrid_user -d hiring_platform -c \
    "INSERT INTO search_history (recruiter_id, query, filters_json) VALUES
     ('$RID', 'passionate startup intern engineer', '{\"city\": \"Bangalore\"}');"
done
curl -s -X POST "http://localhost:8000/api/recruiter/$RID/memory/profile"
```

Expected: `{"observations_written": N}` with N ≥ 2 (city filter + startup/intern terms).

- [ ] **Step 3: Verify the memory endpoint and dismissal**

```bash
curl -s "http://localhost:8000/api/recruiter/$RID/memory" | python3 -m json.tool
```

Expected: `observations` array containing the Bangalore-filter and taste-term entries
with confidence values. Then dismiss one and re-profile:

```bash
MID=$(curl -s "http://localhost:8000/api/recruiter/$RID/memory" | python3 -c "import sys,json; print(json.load(sys.stdin)['observations'][0]['id'])")
curl -s -X DELETE "http://localhost:8000/api/recruiter/$RID/memory/$MID" > /dev/null
curl -s -X POST "http://localhost:8000/api/recruiter/$RID/memory/profile"
curl -s "http://localhost:8000/api/recruiter/$RID/memory" | python3 -m json.tool
```

Expected: the dismissed observation does NOT reappear (dismissed keys are skipped).

- [ ] **Step 4: Verify the agent sees memory**

Send one agent message and confirm the run works and (in server logs) the
profiler fired on first turn:

```bash
curl -sN -X POST http://localhost:8000/agent/chat \
  -H 'Content-Type: application/json' \
  -d "{\"recruiter_id\": \"$RID\", \"session_id\": \"mem-test-1\",
       \"message\": \"what do you know about me?\"}" | head -50
```

Expected: streamed SSE with a text answer that lists the remaining observation(s)
and any facts — proving the memory block reached the prompt.

- [ ] **Step 5: Full test suite**

Run: `python -m pytest tests/ -v --timeout=120`
Expected: all pass (pre-existing failures unrelated to memory are acceptable if
they also fail on the base commit — verify with `git stash && pytest ... && git stash pop`
if anything is ambiguous).

- [ ] **Step 6: Cleanup seed data + commit any fixes**

```bash
docker exec hybrid_search_db psql -U hybrid_user -d hiring_platform -c \
  "DELETE FROM search_history WHERE recruiter_id = '$RID';
   DELETE FROM recruiter_memory WHERE recruiter_id = '$RID'::uuid;
   DELETE FROM recruiter_preferences WHERE recruiter_id = '$RID'::uuid;"
git add -A && git commit -m "test(memory): e2e verification fixes" --allow-empty
```

---

### Task 9: Documentation

**Files:**
- Create: `docs/memory.md`

- [ ] **Step 1: Write the doc**

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
git add docs/memory.md
git commit -m "docs: recruiter memory format, profiler signals, usage contract"
```
