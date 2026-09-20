import sys
from pathlib import Path

from pipeline.search import SearchFilters

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.benchmark_search_strategies import (
    compare_planned_filters,
    evaluate_rank,
    percentile,
    summarize_strategy_runs,
)


def test_evaluate_rank_reports_top_hit_mrr_and_ndcg():
    ranked = [
        {"candidate_id": "first"},
        {"candidate_id": "second"},
        {"candidate_id": "target"},
    ]

    metrics = evaluate_rank(ranked, "target")

    assert metrics["rank"] == 3
    assert metrics["top1"] == 0.0
    assert metrics["hit3"] == 1.0
    assert metrics["hit10"] == 1.0
    assert metrics["mrr10"] == 1 / 3
    assert round(metrics["ndcg10"], 4) == 0.5


def test_percentile_uses_nearest_rank_with_empty_default():
    assert percentile([], 95) == 0.0
    assert percentile([10, 20, 30, 40], 50) == 30
    assert percentile([10, 20, 30, 40], 95) == 40


def test_summarize_strategy_runs_aggregates_quality_latency_and_cost():
    rows = [
        {
            "strategies": {
                "rrf": {
                    "rank": 1,
                    "top1": 1.0,
                    "hit3": 1.0,
                    "hit10": 1.0,
                    "mrr10": 1.0,
                    "ndcg10": 1.0,
                    "total_ms": 100.0,
                    "planning_ms": 0.0,
                    "embedding_ms": 20.0,
                    "retrieval_ms": 70.0,
                    "rerank_ms": 0.0,
                    "llm_calls": 0,
                    "estimated_cost_usd": 0.0,
                }
            }
        },
        {
            "strategies": {
                "rrf": {
                    "rank": None,
                    "top1": 0.0,
                    "hit3": 0.0,
                    "hit10": 0.0,
                    "mrr10": 0.0,
                    "ndcg10": 0.0,
                    "total_ms": 300.0,
                    "planning_ms": 0.0,
                    "embedding_ms": 60.0,
                    "retrieval_ms": 200.0,
                    "rerank_ms": 0.0,
                    "llm_calls": 0,
                    "estimated_cost_usd": 0.0,
                }
            }
        },
    ]

    summary = summarize_strategy_runs(rows, ["rrf"])

    assert summary[0]["strategy_id"] == "rrf"
    assert summary[0]["top1"] == 0.5
    assert summary[0]["hit10"] == 0.5
    assert summary[0]["p50_total_ms"] == 300.0
    assert summary[0]["p95_total_ms"] == 300.0
    assert summary[0]["estimated_cost_per_1k"] == 0.0


def test_compare_planned_filters_scores_only_labeled_fields():
    expected = SearchFilters(location="India", skills=["python", "sql"], min_years_exp=3)
    planned = SearchFilters(location="India", skills=["python"], min_years_exp=3)

    comparison = compare_planned_filters(expected, planned)

    assert comparison["fields"] == 3
    assert comparison["correct"] == 2
    assert comparison["accuracy"] == 2 / 3
