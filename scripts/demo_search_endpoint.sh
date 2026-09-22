#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${1:-http://127.0.0.1:8010}"
RESPONSE_FILE="${RESPONSE_FILE:-/tmp/talent-atlas-demo-search.json}"

read -r -d '' REQUEST <<'JSON' || true
{
  "query": "finance professional experienced in accounting, auditing and financial reporting",
  "mode": "no-llm",
  "skills": ["accounting", "auditing", "financial-reporting"],
  "skills_match": "and",
  "min_years_exp": 5,
  "should": {
    "themes": ["financial analysis", "forecasting"],
    "roles": ["finance manager", "accountant"]
  },
  "keyword_policy": "auto",
  "top_k": 8,
  "include_rank_explanation": true
}
JSON

printf '\nREQUEST: POST %s/search\n' "$BASE_URL"
printf '%s\n' "$REQUEST" | jq .

curl --fail --silent --show-error --max-time 90 \
  --request POST "$BASE_URL/search" \
  --header 'Content-Type: application/json' \
  --data "$REQUEST" > "$RESPONSE_FILE"

printf '\nRESPONSE SUMMARY\n'
jq '{
  query,
  mode,
  total_results,
  latency_ms,
  filters_applied,
  planner_spec: {
    intent: .planner_spec.intent,
    must: .planner_spec.must,
    should: .planner_spec.should,
    semantic_query: .planner_spec.semantic_query,
    lexical_terms: .planner_spec.lexical_terms
  },
  retrieval: {
    keyword_decision: .retrieval_policy.keyword,
    row_counts: .retrieval_policy.row_counts,
    ranking_timings_ms: .retrieval_policy.ranking_timings_ms
  },
  top_three: [
    .results[0:3][] | {
      candidate_id,
      full_name,
      city,
      country,
      years_exp,
      rank_score,
      skills: .skills[0:8],
      explanation: {
        match_tier: .ranking_explanation.match_tier,
        match_score: .ranking_explanation.match_score,
        summary: .ranking_explanation.summary_line,
        retrieval_paths: .ranking_explanation.retrieval_paths
      }
    }
  ]
}' "$RESPONSE_FILE"

printf '\nFull response saved to %s\n' "$RESPONSE_FILE"
printf 'Tip: run `jq . %s | less` to inspect every evidence field.\n' "$RESPONSE_FILE"
