from pipeline.ranking_explanation import build_ranking_explanation
from pipeline.search import SearchResult


def _result(**overrides):
    base = {
        "candidate_id": "candidate-1",
        "full_name": "Ada Lovelace",
        "email": "ada@example.com",
        "city": "London",
        "country": "UK",
        "salary_min": 100000,
        "salary_max": 150000,
        "years_exp": 7,
        "skills": ["python"],
        "best_chunk": "Built Python search systems.",
        "best_chunk_distance": 0.36,
        "similarity_score": 0.64,
        "doc_type": "resume",
        "document_title": "Resume",
        "rank_score": 0.6465,
        "rrf_score": 0.016,
        "ranking_signals": [
            {
                "name": "Embedding similarity",
                "value": 0.64,
                "weight": 0.5,
                "contribution": 0.32,
            },
            {
                "name": "Explicit skill coverage",
                "value": 1.0,
                "weight": 0.18,
                "contribution": 0.18,
            },
        ],
    }
    base.update(overrides)
    return SearchResult(**base)


def test_pipeline_confidence_uses_the_same_score_displayed_by_the_pipeline():
    explanation = build_ranking_explanation(_result(), rank_position=1)

    assert explanation["pipeline_confidence"] == 65
    assert explanation["confidence_label"] == "medium"
    assert explanation["rank_score"] == 0.6465
    assert explanation["sort_basis"] == "rank_score desc"
    assert explanation["score_basis"] == "rank_score"
    assert explanation["signals"][0]["name"] == "Embedding similarity"
    assert explanation["score_breakdown"][0]["name"] == "Embedding similarity"
    assert explanation["score_breakdown"][0]["contribution_percent"] == 32


def test_retrieval_breakdown_shows_weighted_confidence_components():
    explanation = build_ranking_explanation(_result(), rank_position=1)
    breakdown = {item["name"]: item for item in explanation["score_breakdown"]}

    assert breakdown["Embedding similarity"]["weight_percent"] == 50
    assert breakdown["Embedding similarity"]["contribution_percent"] == 32
    assert breakdown["Explicit skill coverage"]["weight_percent"] == 18
    assert breakdown["Explicit skill coverage"]["contribution_percent"] == 18
    assert breakdown["Reranking"]["applied"] is False
    assert breakdown["Reranking"]["contribution_percent"] == 0


def test_rerank_score_takes_precedence_for_pipeline_confidence():
    explanation = build_ranking_explanation(
        _result(rank_score=0.42, rerank_score=0.91),
        rank_position=1,
    )

    assert explanation["pipeline_confidence"] == 91
    assert explanation["confidence_label"] == "high"
    assert explanation["sort_basis"] == "rerank_score desc"
    assert explanation["score_basis"] == "rerank_score"
    assert explanation["final_score"] == 0.91
    assert explanation["retrieval_score"] == 0.42

    breakdown = {item["name"]: item for item in explanation["score_breakdown"]}
    assert breakdown["Reranking"]["applied"] is True
    assert breakdown["Reranking"]["weight_percent"] == 100
    assert breakdown["Reranking"]["contribution_percent"] == 91


def test_out_of_range_rerank_logits_are_calibrated_for_display_confidence():
    explanation = build_ranking_explanation(
        _result(rank_score=0.42, rerank_score=3.0),
        rank_position=1,
    )

    assert explanation["final_score"] == 3.0
    assert explanation["pipeline_confidence"] == 95
    assert explanation["score_breakdown"][0]["name"] == "Reranking"
    assert explanation["score_breakdown"][0]["contribution_percent"] == 95
    assert "raw score" in explanation["score_breakdown"][0]["formula"]


def test_pipeline_confidence_is_monotonic_with_existing_result_order():
    ordered_results = [
        _result(candidate_id="a", rank_score=0.9),
        _result(candidate_id="b", rank_score=0.6),
        _result(candidate_id="c", rank_score=0.2),
    ]

    confidences = [
        build_ranking_explanation(result, index + 1)["pipeline_confidence"]
        for index, result in enumerate(ordered_results)
    ]

    assert confidences == [90, 60, 20]
    assert confidences == sorted(confidences, reverse=True)
