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
