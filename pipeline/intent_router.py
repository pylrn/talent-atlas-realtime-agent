"""Intent router — decides which execution path to take after planning.

  candidate_lookup     → direct DB fetch by name, skip retrieval
  candidate_filter     → SQL + sort by recency, skip embedding/BM25
  candidate_search     → full pipeline (default)
  candidate_comparison → fetch both candidates, return comparison dict
  candidate_explanation→ fetch single candidate, return explanation block
  analytics_query      → aggregate SQL
"""

from __future__ import annotations

from pipeline.spec import CanonicalSearchSpec
from pipeline.constants import CONFIDENCE_THRESHOLD


def should_short_circuit(spec: CanonicalSearchSpec, cfg: dict) -> str | None:
    """Return a short-circuit action name, or None to run the full pipeline.

    Short-circuit actions handled OUTSIDE the retrieval pipeline:
      "clarify"      — return clarify question, no search
      "lookup"       — direct candidate fetch by name
      "comparison"   — fetch both candidates, no retrieval
      "explanation"  — fetch + explain single candidate, no retrieval
      "analytics"    — aggregate SQL query
      "filter_only"  — SQL + recency sort, no embedding

    None → run full retrieval pipeline.
    """
    use_confidence_gate = cfg.get("use_confidence_gate", True)
    use_intent_router   = cfg.get("use_intent_router",   True)

    # Confidence gate (step 6)
    if use_confidence_gate and spec.clarify and spec.confidence < CONFIDENCE_THRESHOLD:
        return "clarify"

    if not use_intent_router:
        return None

    intent = spec.intent

    if intent == "candidate_lookup":
        return "lookup"
    if intent == "candidate_comparison":
        return "comparison"
    if intent == "candidate_explanation":
        return "explanation"
    if intent == "analytics_query":
        return "analytics"
    if intent == "candidate_filter" or spec.is_filter_only():
        return "filter_only"

    return None  # candidate_search — full pipeline
