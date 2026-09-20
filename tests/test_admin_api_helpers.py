from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi import HTTPException

from api import main as api_main
from api.main import (
    CandidateUpdate,
    SearchRequest,
    _build_candidate_update,
    _candidate_list_filters,
    _chunk_list_filters,
    _skill_option_values,
    _row_to_filter_candidate_result,
    _row_to_admin_candidate,
    _row_to_admin_chunk,
    _row_to_admin_document,
    _filters_dict,
)
from pipeline.search import SearchResult


def test_build_candidate_update_rejects_empty_payload():
    with pytest.raises(HTTPException) as exc:
        _build_candidate_update("candidate-1", CandidateUpdate())

    assert exc.value.status_code == 400
    assert exc.value.detail == "No candidate fields supplied"


def test_build_candidate_update_uses_only_supplied_fields():
    sql, params = _build_candidate_update(
        "candidate-1",
        CandidateUpdate(full_name="Ada Lovelace", skills=["python"], years_exp=3),
    )

    assert "full_name = $1" in sql
    assert "skills = $2" in sql
    assert "years_exp = $3" in sql
    assert "WHERE id = $4" in sql
    assert params == ["Ada Lovelace", ["python"], 3, "candidate-1"]


def test_build_candidate_update_allows_partial_updates_without_name():
    sql, params = _build_candidate_update(
        "candidate-1",
        CandidateUpdate(status="archived", skills=[]),
    )

    assert "skills = $1" in sql
    assert "status = $2" in sql
    assert "WHERE id = $3" in sql
    assert params == [[], "archived", "candidate-1"]


def test_admin_candidate_serializer_is_json_safe():
    row = {
        "id": UUID("00000000-0000-0000-0000-000000000001"),
        "full_name": "Ada Lovelace",
        "email": "ada@example.com",
        "age": 36,
        "location": "London, UK",
        "city": "London",
        "country": "UK",
        "interests": ["math"],
        "skills": ["python", "systems"],
        "years_exp": 5,
        "salary_min": 100000,
        "salary_max": 150000,
        "status": "active",
        "created_at": datetime(2026, 5, 14, 10, 30, tzinfo=UTC),
        "updated_at": datetime(2026, 5, 14, 11, 45, tzinfo=UTC),
        "document_count": 2,
        "chunk_count": 7,
    }

    candidate = _row_to_admin_candidate(row)

    assert candidate["id"] == "00000000-0000-0000-0000-000000000001"
    assert candidate["created_at"] == "2026-05-14T10:30:00+00:00"
    assert candidate["updated_at"] == "2026-05-14T11:45:00+00:00"
    assert candidate["document_count"] == 2
    assert candidate["chunk_count"] == 7


def test_admin_list_serializers_return_previews_without_vectors():
    document = _row_to_admin_document(
        {
            "id": "doc-1",
            "candidate_id": "candidate-1",
            "candidate_name": "Ada Lovelace",
            "doc_type": "resume",
            "title": "Resume",
            "raw_text_preview": "Line one\nLine two",
            "char_count": 17,
            "chunk_count": 2,
            "created_at": None,
        }
    )
    chunk = _row_to_admin_chunk(
        {
            "id": "chunk-1",
            "candidate_id": "candidate-1",
            "candidate_name": "Ada Lovelace",
            "document_id": "doc-1",
            "doc_type": "resume",
            "document_title": "Resume",
            "chunk_index": 0,
            "content_preview": "Built search systems",
            "token_count": 12,
            "created_at": None,
        }
    )

    assert document["preview"] == "Line one Line two"
    assert "raw_text" not in document
    assert chunk["preview"] == "Built search systems"
    assert "embedding" not in chunk


def test_chunk_list_filters_support_text_search():
    where_sql, params = _chunk_list_filters(
        candidate_id="candidate-1",
        document_id=None,
        q="kubernetes",
    )

    assert "dc.candidate_id = $1" in where_sql
    assert "dc.content ILIKE $2" in where_sql
    assert "cd.title ILIKE $2" in where_sql
    assert "c.full_name ILIKE $2" in where_sql
    assert params == ["candidate-1", "%kubernetes%"]


def test_candidate_list_filters_support_separate_candidate_fields():
    where_sql, params = _candidate_list_filters(
        q=None,
        status="active",
        name_email="ada",
        city="London",
        country="UK",
        skills=["python", "sql"],
    )

    assert "c.full_name ILIKE $1" in where_sql
    assert "c.email ILIKE $1" in where_sql
    assert "c.city ILIKE $2" in where_sql
    assert "c.country ILIKE $3" in where_sql
    assert "c.skills && $4::text[]" in where_sql
    assert "c.status = $5" in where_sql
    assert params == ["%ada%", "%London%", "%UK%", ["python", "sql"], "active"]


def test_skill_option_values_drop_resume_fragments_and_normalize():
    rows = [
        {"value": "Python"},
        {"value": "ReactJS"},
        {"value": "012015-present-nyc-teaching-classes-25-biology"},
        {"value": "quick-learner-eagerness-to-learn-new-things-competitive-attitude"},
        {"value": "3d modeling"},
        {"value": "project-management"},
        {"value": "python"},
    ]

    assert _skill_option_values(rows) == [
        "3d-modeling",
        "project-management",
        "python",
        "react",
    ]


def test_search_request_allows_filter_only_searches():
    req = SearchRequest(location="Pune", skills=["python"], min_salary=90000, top_k=5)

    assert req.query == ""
    assert req.location == "Pune"
    assert req.skills == ["python"]
    assert req.min_salary == 90000
    assert req.top_k == 5
    assert req.include_rank_explanation is True
    assert req.include_fit_analysis is None


def test_search_request_can_disable_rank_explanation_and_keeps_it_out_of_filters():
    req = SearchRequest(query="python", include_rank_explanation=False)

    assert req.include_rank_explanation is False
    assert "include_rank_explanation" not in _filters_dict(req)


def test_search_request_allows_larger_result_sets():
    req = SearchRequest(query="python", top_k=104)

    assert req.top_k == 104


def test_search_request_can_enable_ai_insights_without_filter_leakage():
    req = SearchRequest(query="python", include_ai_insights=True)

    assert req.include_ai_insights is True
    assert "include_ai_insights" not in _filters_dict(req)


def test_search_request_can_enable_reranking_without_filter_leakage():
    req = SearchRequest(query="python", enable_reranking=True, reranker="local-fast")

    assert req.enable_reranking is True
    assert req.reranker == "local-fast"
    assert "enable_reranking" not in _filters_dict(req)
    assert "reranker" not in _filters_dict(req)


def test_deprecated_fit_analysis_flag_can_disable_rank_explanation():
    req = SearchRequest(query="python", include_fit_analysis=False)

    assert req.include_fit_analysis is False
    assert "include_fit_analysis" not in _filters_dict(req)


@pytest.mark.asyncio
async def test_search_skips_rank_explanation_when_request_disables_it(monkeypatch):
    class FakeEngine:
        async def search(self, **kwargs):
            return []

    def fail_if_called(*args, **kwargs):
        raise AssertionError("ranking explanation should be skipped")

    monkeypatch.setattr(api_main.app.state, "search_engine", FakeEngine(), raising=False)
    monkeypatch.setattr(api_main, "_attach_ranking_explanations", fail_if_called)

    response = await api_main.search(SearchRequest(query="python", include_rank_explanation=False))

    assert response.results == []


@pytest.mark.asyncio
async def test_search_returns_all_available_results_when_top_k_is_larger(monkeypatch):
    available_results = [
        _search_result("first", rank_score=0.8),
        _search_result("second", rank_score=0.6),
    ]

    class FakeEngine:
        async def search(self, **kwargs):
            assert kwargs["top_k"] == 500
            return available_results

    monkeypatch.setattr(api_main.app.state, "search_engine", FakeEngine(), raising=False)

    response = await api_main.search(SearchRequest(query="python", top_k=500))

    assert response.total_results == 2
    assert [item.candidate_id for item in response.results] == ["first", "second"]


@pytest.mark.asyncio
async def test_search_response_includes_latency_breakdown(monkeypatch):
    class FakeEngine:
        async def search(self, **kwargs):
            return [_search_result("first", rank_score=0.8)]

    monkeypatch.setattr(api_main.app.state, "search_engine", FakeEngine(), raising=False)

    response = await api_main.search(SearchRequest(query="python"))

    assert response.timings_ms["total"] == response.latency_ms
    assert "search_pipeline" in response.timings_ms
    assert "result_formatting" in response.timings_ms
    assert "rerank" not in response.timings_ms


@pytest.mark.asyncio
async def test_inline_ai_insights_are_included_in_total_latency(monkeypatch):
    from pipeline import cache as _cache

    _cache.clear_all()

    class FakeEngine:
        async def search(self, **kwargs):
            return [_search_result("first", rank_score=0.8)]

    class FakeInsightService:
        async def generate(self, query, filters_applied, candidates):
            return api_main.PipelineAIInsightResult(
                status="ok",
                provider="gemini",
                model="gemini-test",
                best_candidate_id=candidates[0].candidate_id,
                summary="first is strongest.",
                comparative_reasoning="first has the best evidence.",
                matrix=[],
                timings_ms={"llm": 1200.0, "total": 1500.0},
            )

    monkeypatch.setattr(api_main.app.state, "search_engine", FakeEngine(), raising=False)
    monkeypatch.setattr(api_main, "AIInsightService", lambda **kwargs: FakeInsightService())

    response = await api_main.search(
        SearchRequest(query="python", include_ai_insights=True),
    )

    assert response.ai_insights is not None
    assert response.ai_insights.timings_ms == {"llm": 1200.0, "total": 1500.0}
    assert response.timings_ms["insights_llm"] == 1200.0
    assert response.timings_ms["insights_total"] == 1500.0
    assert response.timings_ms["total"] == response.latency_ms
    assert response.timings_ms["total"] >= response.timings_ms["search_total"]
    _cache.clear_all()


@pytest.mark.asyncio
async def test_search_applies_optional_reranker_before_explanation(monkeypatch):
    first = _search_result("first", rank_score=0.8)
    second = _search_result("second", rank_score=0.6)

    class FakeEngine:
        async def search(self, **kwargs):
            return [first, second]

    class FakeReranker:
        async def rerank(self, query, results):
            results[0].rerank_score = 0.2
            results[1].rerank_score = 0.9
            return [results[1], results[0]]

    monkeypatch.setattr(api_main.app.state, "search_engine", FakeEngine(), raising=False)
    monkeypatch.setattr(api_main, "_get_cached_reranker", lambda app, choice: FakeReranker())

    response = await api_main.search(
        SearchRequest(query="python", enable_reranking=True, include_rank_explanation=True),
    )

    assert [item.candidate_id for item in response.results] == ["second", "first"]
    assert response.results[0].rerank_score == 0.9
    assert response.results[0].ranking_explanation["sort_basis"] == "rerank_score desc"
    assert response.results[0].ranking_explanation["pipeline_confidence"] == 90


@pytest.mark.asyncio
async def test_search_insights_compares_at_most_top_ten(monkeypatch):
    from pipeline import cache as _cache

    _cache.clear_all()
    captured = {}

    class FakeEngine:
        async def search(self, **kwargs):
            captured["top_k"] = kwargs["top_k"]
            return [
                _search_result(f"candidate-{index}", rank_score=1 - (index / 100))
                for index in range(12)
            ]

    class FakeInsightService:
        async def generate(self, query, filters_applied, candidates):
            captured["candidate_count"] = len(candidates)
            return api_main.AIInsightResponse(
                status="ok",
                provider="gemini",
                model="gemini-test",
                best_candidate_id=candidates[0].candidate_id,
                summary="candidate-0 is strongest.",
                matrix=[],
                timings_ms={"llm": 1.0},
            )

    monkeypatch.setattr(api_main.app.state, "search_engine", FakeEngine(), raising=False)
    monkeypatch.setattr(api_main, "AIInsightService", lambda **kwargs: FakeInsightService())

    response = await api_main.search_insights(SearchRequest(query="python", top_k=50))

    assert response.status == "ok"
    assert captured["top_k"] == 10
    assert captured["candidate_count"] == 10
    assert response.best_candidate_id == "candidate-0"


def test_search_request_does_not_expose_doc_type_filtering():
    req = SearchRequest(query="python", doc_types=["resume"])

    assert "doc_types" not in req.model_dump()


def _search_result(candidate_id: str, rank_score: float) -> SearchResult:
    return SearchResult(
        candidate_id=candidate_id,
        full_name=f"{candidate_id} Candidate",
        email=None,
        city="Bengaluru",
        country="India",
        salary_min=None,
        salary_max=None,
        years_exp=5,
        skills=["python"],
        best_chunk="Python backend search systems.",
        best_chunk_distance=0.3,
        similarity_score=0.7,
        doc_type="resume",
        document_title="Resume",
        rrf_score=0.016,
        rank_score=rank_score,
        ranking_signals=[
            {
                "name": "Embedding similarity",
                "value": 0.7,
                "weight": 0.5,
                "contribution": 0.35,
            }
        ],
    )


def test_filter_only_rows_map_to_candidate_results():
    result = _row_to_filter_candidate_result(
        {
            "candidate_id": UUID("00000000-0000-0000-0000-000000000002"),
            "full_name": "Katherine Johnson",
            "email": "katherine@example.com",
            "city": "Bengaluru",
            "country": "India",
            "skills": ["python", "postgres"],
            "salary_min": 120000,
            "salary_max": 160000,
            "best_chunk": "Built analytics pipelines in Postgres.",
            "doc_type": "resume",
            "document_title": "Resume",
        }
    )

    assert result.candidate_id == "00000000-0000-0000-0000-000000000002"
    assert result.similarity_score == 0
    assert result.doc_type == "resume"
    assert result.best_chunk == "Built analytics pipelines in Postgres."
    assert result.salary_min == 120000
    assert result.salary_max == 160000
