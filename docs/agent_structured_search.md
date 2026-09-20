# Agent Structured Search Changes

## Why This Changed

The agent now does most query understanding before it calls search. That means
the backend should not run the same request through another LLM planner unless
the agent deliberately asks for it.

The old contract only had:

```text
run_search(query, filters, weights, mode)
```

That made the agent put too much meaning into `query`. Optional skills, role
themes, and hard requirements could blur together. In `no-llm` mode the fallback
planner may parse known skills from the query and promote them into hard
`must.skills`, which is wrong for searches like "React or Vue".

The other issue was latency. In the default `BM25_BACKEND=fts` mode, the branch
named "BM25" is actually PostgreSQL full-text search with `ts_rank_cd`. It can
be very useful for exact terms, but for broad/common terms it may match and rank
a large chunk set before `LIMIT` can return the top rows. That branch was the
main source of multi-second tails.

There is also an optional `BM25_BACKEND=paradedb` mode. That uses ParadeDB
`pg_search` for real BM25 ranking, so the keyword branch can ask the index for
score-ordered top-k rows instead of scanning and sorting a broad match set.

## New Mental Model

The agent is the planner. The backend is the executor with guardrails.

```text
recruiter message
  -> agent extracts hard requirements and soft preferences
  -> agent calls run_search with structured fields
  -> backend executes deterministic retrieval
  -> keyword branch runs through FTS guardrails or ParadeDB BM25
  -> ranking/fusion/enrichment returns candidates plus diagnostics
```

## New Search Mode

`agent-quality` is a new mode for agent-planned searches.

It uses:

```text
use_llm_planner = false
use_cross_encoder = true
keyword_policy = auto
keyword_timeout_ms = 800
```

Use it when the agent has already structured the search but wants stronger final
ranking. This is different from `quality`, which still uses backend LLM planning.

Mode guidance:

| Mode | Use When |
| --- | --- |
| `no-llm` | Agent has structured the request and wants faster results. |
| `agent-quality` | Agent has structured the request and wants stronger ranking. |
| `quality` | Raw ambiguous text needs backend LLM planning. |
| `fast` | Rarely; legacy fast preset. |

## New Agent Tool Contract

`run_search` now accepts:

```text
run_search(query, filters?, should?, weights?, retrieval?, mode?)
```

Hard requirements go in `filters`.

```json
{
  "skills": ["python"],
  "city": "Bengaluru",
  "min_years_exp": 5
}
```

Soft preferences go in `should`.

```json
{
  "skills": ["aws", "kubernetes"],
  "themes": ["api design", "cloud infrastructure"],
  "roles": ["backend engineer"],
  "locations": ["remote"]
}
```

Retrieval controls go in `retrieval`.

```json
{
  "keyword_policy": "auto",
  "keyword_timeout_ms": 800
}
```

If `should` is provided without hard `filters.skills`, the agent/API explicitly
sends `skills: []`. This prevents the no-LLM fallback parser from promoting
optional query terms into hard skill filters.

## Keyword Policy

The keyword branch still lives in `retrieve_bm25.py` for compatibility. It now
has two backends:

| Backend | Meaning |
| --- | --- |
| `fts` | Current Postgres `tsvector` + `ts_rank_cd` fallback. |
| `paradedb` | Real BM25 via ParadeDB/`pg_search` and `pdb.score`. |

Policies:

| Policy | Meaning |
| --- | --- |
| `auto` | Backend decides from filters, terms, and candidate count. |
| `skip` | Do not run keyword/FTS. Use dense + skill retrieval. |
| `force` | Run keyword/FTS, but still apply timeout. |

In `auto` with `BM25_BACKEND=fts`, keyword runs when:

- hard filters are narrow,
- the filtered candidate pool is small,
- or the query contains specific terms like `kubernetes`, `terraform`, `sql`,
  `salesforce`, `postgresql`, `llm`.

With `BM25_BACKEND=fts`, it skips when:

- the pool is broad,
- and terms are common, such as `manager`, `developer`, `finance`,
  `marketing`, `leader`, `experience`, `systems`.

The response includes:

```json
{
  "retrieval_policy": {
    "keyword": {
      "action": "skip",
      "reason": "broad_pool_common_terms",
      "candidate_count": 9555,
      "timeout_ms": 800,
      "backend": "fts"
    }
  }
}
```

With `BM25_BACKEND=paradedb`, `auto` runs the keyword branch for any usable
keyword query because the BM25 index makes broad top-k ranking bounded. Use
`retrieval={"keyword_policy":"skip"}` only when the query is intentionally vague
and exact keywords would add noise.

## Examples

Required Python, preferred AWS:

```text
run_search(
  query="senior backend engineer cloud APIs",
  filters={"skills":["python"], "city":"Berlin", "min_years_exp":5},
  should={"skills":["aws"], "roles":["backend engineer"]},
  mode="agent-quality"
)
```

React or Vue, neither required:

```text
run_search(
  query="frontend engineer responsive ui",
  filters={"skills":[], "city":"Bengaluru"},
  should={"skills":["react","vue"], "roles":["frontend engineer"]},
  mode="agent-quality"
)
```

Broad semantic query where keyword is likely wasteful:

```text
run_search(
  query="finance operations manager",
  filters={"skills":[]},
  should={"roles":["operations manager"], "themes":["finance operations"]},
  retrieval={"keyword_policy":"skip"},
  mode="no-llm"
)
```

Exact tool/acronym query:

```text
run_search(
  query="kubernetes terraform sre platform",
  filters={"skills":[]},
  should={"skills":["kubernetes","terraform"], "roles":["sre"]},
  retrieval={"keyword_policy":"auto"},
  mode="agent-quality"
)
```

## Expected Latency Impact

Before this change, broad no-LLM searches could be gated by the keyword branch.
In the earlier benchmark, current no-LLM p90 was about 4 seconds and the worst
case was about 5 seconds.

The skip-broad-keyword simulation on the same 20 no-cache queries produced:

```text
avg: 661 ms
p50: 599 ms
p90: 1495 ms
max: 1724 ms
```

With `BM25_BACKEND=fts`, the real implementation behaves similarly for broad
searches because the expensive keyword branch now either skips or times out.
Specific searches still get keyword recall when it is likely to help.

With `BM25_BACKEND=paradedb`, the expected shape changes: broad keyword queries
should keep keyword recall without the old multi-second `ts_rank_cd` tail. See
`docs/paradedb_pg_search_integration.md` for rollout details.

## Files Changed

- `pipeline/modes.py` adds `agent-quality`.
- `pipeline/keyword_policy.py` classifies keyword eligibility.
- `pipeline/search.py` applies keyword policy, timeout, and diagnostics.
- `pipeline/retrieve_bm25.py` applies DB-side `statement_timeout` and optional
  ParadeDB `pg_search` SQL.
- `pipeline/database.py` supports a separate search/read pool.
- `pipeline/validator.py` merges explicit `should` fields.
- `pipeline/agent.py` updates the agent prompt and tool signature.
- `pipeline/agent_tools.py` passes `should` and retrieval controls through.
- `api/main.py` exposes the same contract on `/search`.
