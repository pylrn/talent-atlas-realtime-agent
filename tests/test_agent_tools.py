import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pipeline.agent_tools import (
    do_run_search,
    do_explain_poor_results,
    do_compare_iterations,
    do_get_candidate_detail,
    do_get_candidate_details,
    do_view_main_results,
    do_save_hint,
)
from pipeline.agent_session import AgentSession, StackEntry, RoleContext


def _make_session() -> AgentSession:
    return AgentSession(recruiter_id="r-1", session_id="s-1")


def _make_entry(query="python dev", n_results=3) -> StackEntry:
    return StackEntry(
        query=query,
        filters={},
        results_preview=[{"id": f"c-{i}", "name": f"Cand {i}", "score": 0.9 - i * 0.1}
                         for i in range(n_results)],
        agent_reasoning="initial search",
    )


@pytest.mark.asyncio
async def test_do_run_search_pushes_entry_to_stack(monkeypatch):
    session = _make_session()
    mock_pool = AsyncMock()

    mock_result = MagicMock()
    mock_result.candidate_id = "c-0"
    mock_result.feature_score = 0.9
    mock_result.full_name = "Cand 0"
    mock_result.city = "Bangalore"
    mock_result.years_exp = 5
    mock_result.skills = ["python"]

    mock_resp = MagicMock()
    mock_resp.results = [mock_result]
    mock_resp.spec = MagicMock(query_text="python dev")

    with patch("pipeline.agent_tools.SearchEngine") as MockEngine:
        instance = MockEngine.return_value
        instance.smart_search = AsyncMock(return_value=mock_resp)

        result = await do_run_search(
            pool=mock_pool,
            session=session,
            recruiter_id="r-1",
            query="python dev",
            filters={},
            weights={},
        )

    assert len(session.search_stack) == 1
    assert session.search_stack[0].query == "python dev"
    assert "results" in result
    assert len(result["results"]) <= 3


@pytest.mark.asyncio
async def test_do_run_search_uses_injected_search_engine():
    session = _make_session()
    mock_pool = AsyncMock()

    mock_result = MagicMock()
    mock_result.candidate_id = "c-0"
    mock_result.feature_score = 0.9
    mock_result.full_name = "Cand 0"
    mock_result.city = "Bangalore"
    mock_result.years_exp = 5
    mock_result.skills = ["python"]
    mock_result.explanation = {}

    mock_resp = MagicMock()
    mock_resp.results = [mock_result]
    mock_resp.spec = None
    mock_resp.phase_timings = {}
    mock_resp.total_candidates_scanned = 1
    mock_resp.clarify = None

    injected_engine = MagicMock()
    injected_engine.smart_search = AsyncMock(return_value=mock_resp)

    with patch("pipeline.agent_tools.SearchEngine") as MockEngine:
        result = await do_run_search(
            pool=mock_pool,
            session=session,
            recruiter_id="r-1",
            query="python dev",
            filters={},
            weights={},
            search_engine=injected_engine,
        )

    MockEngine.assert_not_called()
    injected_engine.smart_search.assert_awaited_once()
    assert injected_engine.smart_search.await_args.kwargs["mode"] == "no-llm"
    assert result["results"][0]["name"] == "Cand 0"
    assert result["mode"] == "no-llm"


@pytest.mark.asyncio
async def test_do_run_search_accepts_explicit_quality_mode():
    session = _make_session()
    mock_pool = AsyncMock()

    mock_result = MagicMock()
    mock_result.candidate_id = "c-0"
    mock_result.feature_score = 80
    mock_result.full_name = "Cand 0"
    mock_result.city = "Bangalore"
    mock_result.years_exp = 5
    mock_result.skills = ["python"]
    mock_result.explanation = {}

    mock_resp = MagicMock()
    mock_resp.results = [mock_result]
    mock_resp.spec = None
    mock_resp.phase_timings = {}
    mock_resp.total_candidates_scanned = 0
    mock_resp.clarify = None

    injected_engine = MagicMock()
    injected_engine.smart_search = AsyncMock(return_value=mock_resp)

    result = await do_run_search(
        pool=mock_pool,
        session=session,
        recruiter_id="r-1",
        query="messy ambiguous request",
        filters={},
        weights={},
        mode="quality",
        search_engine=injected_engine,
    )

    assert injected_engine.smart_search.await_args.kwargs["mode"] == "quality"
    assert session.search_stack[0].mode == "quality"
    assert result["mode"] == "quality"


@pytest.mark.asyncio
async def test_do_run_search_passes_should_and_retrieval_controls():
    session = _make_session()
    mock_pool = AsyncMock()

    mock_resp = MagicMock()
    mock_resp.results = []
    mock_resp.spec = None
    mock_resp.phase_timings = {}
    mock_resp.total_candidates_scanned = 0
    mock_resp.clarify = None
    mock_resp.retrieval_policy = {"keyword": {"action": "skip"}}

    injected_engine = MagicMock()
    injected_engine.smart_search = AsyncMock(return_value=mock_resp)

    result = await do_run_search(
        pool=mock_pool,
        session=session,
        recruiter_id="r-1",
        query="frontend engineer react vue",
        filters={"city": "Bengaluru"},
        should={"skills": ["react", "vue"]},
        weights={},
        retrieval={"keyword_policy": "skip", "keyword_timeout_ms": 500},
        mode="agent-quality",
        search_engine=injected_engine,
    )

    kwargs = injected_engine.smart_search.await_args.kwargs
    assert kwargs["mode"] == "agent-quality"
    assert kwargs["explicit_filters"] == {
        "city": "Bengaluru",
        "should": {"skills": ["react", "vue"]},
        "skills": [],
    }
    assert kwargs["config_overrides"]["keyword_policy"] == "skip"
    assert kwargs["config_overrides"]["keyword_timeout_ms"] == 500
    assert result["retrieval_policy"]["keyword"]["action"] == "skip"
    assert session.search_stack[0].filters["should"]["skills"] == ["react", "vue"]


@pytest.mark.asyncio
async def test_do_run_search_exposes_pipeline_relaxations():
    session = _make_session()
    mock_resp = MagicMock()
    mock_resp.results = []
    mock_resp.spec = None
    mock_resp.phase_timings = {}
    mock_resp.total_candidates_scanned = 0
    mock_resp.clarify = None
    mock_resp.retrieval_policy = {}
    mock_resp.relaxations_applied = [{
        "field": "location",
        "from": {"city": "bangalore", "country": None},
        "to": "soft_preference",
        "reason": "no_results_with_strict_location_filter",
    }]
    engine = MagicMock()
    engine.smart_search = AsyncMock(return_value=mock_resp)

    result = await do_run_search(
        pool=AsyncMock(),
        session=session,
        recruiter_id="r-1",
        query="python engineer",
        filters={"city": "Bengaluru"},
        weights={},
        search_engine=engine,
    )

    assert result["relaxations_applied"][0]["field"] == "location"


@pytest.mark.asyncio
async def test_do_compare_iterations_shows_diff():
    entry_a = _make_entry("python dev", n_results=3)
    entry_b = _make_entry("senior python dev", n_results=3)
    entry_b.results_preview[0]["id"] = "c-new"

    result = await do_compare_iterations(entry_a, entry_b)

    assert "query" in result
    assert result["query"]["before"] == "python dev"
    assert result["query"]["after"] == "senior python dev"


@pytest.mark.asyncio
async def test_do_get_candidate_detail_queries_db():
    candidate_id = "32d2c0db-8901-4118-8bca-b8d6bf92dc6f"
    mock_pool = AsyncMock()
    mock_pool.fetch = AsyncMock(return_value=[
        {
            "id": candidate_id,
            "full_name": "Ada Lovelace",
            "email": "ada@example.com",
            "city": "Bangalore",
            "candidate_id": candidate_id,
            "country": "India",
            "years_exp": 8,
            "salary_min": 100000,
            "salary_max": 140000,
            "skills": ["python"],
            "best_chunk": "Built analytical engines.",
            "doc_type": "resume",
            "document_title": "Ada Resume",
        },
    ])

    result = await do_get_candidate_detail(mock_pool, candidate_id)

    assert result["full_name"] == "Ada Lovelace"
    assert result["skills"] == ["python"]
    mock_pool.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_do_get_candidate_details_batches_profiles():
    ids = [
        "32d2c0db-8901-4118-8bca-b8d6bf92dc6f",
        "44b96d29-c531-4544-bdca-4c3bc9641dde",
    ]
    mock_pool = AsyncMock()
    mock_pool.fetch = AsyncMock(return_value=[
        {
            "id": ids[0],
            "full_name": "Ada Lovelace",
            "email": "ada@example.com",
            "city": "Bangalore",
            "country": "India",
            "years_exp": 8,
            "salary_min": 100000,
            "salary_max": 140000,
            "skills": ["python"],
            "best_chunk": "Built analytical engines.",
            "doc_type": "resume",
            "document_title": "Ada Resume",
        },
        {
            "id": ids[1],
            "full_name": "Grace Hopper",
            "email": "grace@example.com",
            "city": "New York",
            "country": "USA",
            "years_exp": 10,
            "salary_min": 130000,
            "salary_max": 180000,
            "skills": ["compilers"],
            "best_chunk": "Led compiler work.",
            "doc_type": "resume",
            "document_title": "Grace Resume",
        },
    ])

    result = await do_get_candidate_details(mock_pool, ids)

    assert result["count"] == 2
    assert [c["id"] for c in result["candidates"]] == ids
    mock_pool.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_do_view_main_results_preserves_ranked_search_scores_and_explanations():
    session = _make_session()
    ranked = {
        "id": "c-1",
        "name": "Ada Lovelace",
        "feature_score": 49,
        "match_tier": "Partial",
        "best_evidence": "Matched CRM and lead generation evidence.",
        "ranking_explanation": {
            "match_score": 49,
            "score_basis": "feature_score",
            "summary_line": "Low cross-encoder but strong skill match.",
            "checks": {"required": [{"label": "Has CRM", "matched": True}], "preferred": []},
            "score_breakdown": [
                {"name": "Text match quality", "score": 49, "weight_pct": 42, "contribution_percent": 20}
            ],
        },
    }
    session.search_stack.append(StackEntry(
        query="SDR profiles",
        filters={},
        results_preview=[ranked],
        agent_reasoning="",
    ))
    mock_pool = AsyncMock()
    mock_pool.fetch = AsyncMock(return_value=[])

    result = await do_view_main_results(mock_pool, ["c-1"], session=session)

    assert result[0]["feature_score"] == 49
    assert result[0]["ranking_explanation"]["match_score"] == 49
    assert result[0]["ranking_explanation"]["summary_line"] == "Low cross-encoder but strong skill match."
    mock_pool.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_do_view_main_results_db_only_candidates_explain_unscored_selection():
    mock_pool = AsyncMock()
    mock_pool.fetch = AsyncMock(return_value=[
        {
            "id": "c-1",
            "full_name": "Ada Lovelace",
            "email": "ada@example.com",
            "city": "London",
            "country": "UK",
            "years_exp": 4,
            "salary_min": None,
            "salary_max": None,
            "skills": ["crm", "lead-generation"],
            "best_chunk": "Resume text mentions CRM and lead generation.",
            "doc_type": "resume",
            "document_title": "Ada Resume",
        },
    ])

    result = await do_view_main_results(mock_pool, ["c-1"])

    assert result[0]["score_available"] is False
    assert result[0]["feature_score"] is None
    assert result[0]["ranking_explanation"]["score_basis"] == "agent_selected"
    assert "not from a scored hybrid ranking" in result[0]["ranking_explanation"]["summary_line"]
    assert result[0]["ranking_explanation"]["best_evidence"] == "Resume text mentions CRM and lead generation."


@pytest.mark.asyncio
async def test_do_save_hint_writes_to_recruiter_memory():
    mock_pool = AsyncMock()
    mock_pool.execute = AsyncMock()
    mock_pool.fetchval = AsyncMock(return_value=0)

    result = await do_save_hint(mock_pool, "r-1", "prefers startup builders")

    mock_pool.execute.assert_called_once()
    call_args = mock_pool.execute.call_args[0]
    assert "recruiter_memory" in call_args[0]
    assert result["saved"] == "prefers startup builders"


from pipeline.agent_tools import (
    do_keyword_search,
    do_keyword_search_batch,
    do_load_candidate_pool,
    do_filter_from_pool,
    do_aggregate_pool,
    do_list_recent_sessions,
    do_list_skills_batch,
)


@pytest.mark.asyncio
async def test_do_keyword_search_returns_candidates():
    mock_pool = AsyncMock()
    mock_pool.fetch = AsyncMock(return_value=[
        {"id": "c-1", "full_name": "Ada", "city": "Bangalore", "country": "India", "years_exp": 6, "score": 0.8},
    ])
    results = await do_keyword_search(mock_pool, "machine learning")
    assert len(results) == 1
    assert results[0]["full_name"] == "Ada"


@pytest.mark.asyncio
async def test_do_keyword_search_batch_groups_matches_by_query():
    mock_pool = AsyncMock()
    mock_pool.fetch = AsyncMock(return_value=[
        {"term": "airflow", "id": "c-1", "full_name": "Ada", "city": "Pune", "country": "India", "years_exp": 5, "score": 0.8},
        {"term": "spark", "id": "c-2", "full_name": "Grace", "city": "Mumbai", "country": "India", "years_exp": 7, "score": 0.7},
    ])

    result = await do_keyword_search_batch(mock_pool, ["airflow", "spark", "airflow"])

    assert result["queries"] == ["airflow", "spark"]
    assert result["results"]["airflow"][0]["full_name"] == "Ada"
    assert result["count"] == 2
    mock_pool.fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_do_load_candidate_pool_populates_session():
    session = _make_session()
    mock_pool = AsyncMock()
    mock_pool.fetch = AsyncMock(return_value=[
        {"id": "c-1", "full_name": "Ada", "city": "Bangalore", "country": "India",
         "years_exp": 6, "salary_min": 100000, "salary_max": 140000},
        {"id": "c-2", "full_name": "Grace", "city": "Mumbai", "country": "India",
         "years_exp": 4, "salary_min": 80000, "salary_max": 110000},
    ])
    result = await do_load_candidate_pool(mock_pool, session, criteria={}, limit=50)
    assert session.candidate_pool == result["pool"]
    assert len(result["pool"]) == 2


def test_do_filter_from_pool_by_city():
    session = _make_session()
    session.candidate_pool = [
        {"id": "c-1", "city": "Bangalore", "years_exp": 6},
        {"id": "c-2", "city": "Mumbai", "years_exp": 4},
    ]
    result = do_filter_from_pool(session, {"city": "Bangalore"})
    assert len(result) == 1
    assert result[0]["id"] == "c-1"


def test_do_aggregate_pool_counts_by_city():
    session = _make_session()
    session.candidate_pool = [
        {"id": "c-1", "city": "Bangalore"},
        {"id": "c-2", "city": "Bangalore"},
        {"id": "c-3", "city": "Mumbai"},
    ]
    result = do_aggregate_pool(session, "city")
    assert result["Bangalore"] == 2
    assert result["Mumbai"] == 1


@pytest.mark.asyncio
async def test_do_list_recent_sessions_returns_compact_summaries():
    mock_pool = AsyncMock()
    mock_pool.fetch = AsyncMock(return_value=[
        {
            "session_id": "s-1",
            "title": "Python search",
            "summary": "Looked for senior Python engineers.",
            "updated_at": "2026-06-15T10:00:00+00:00",
        }
    ])

    result = await do_list_recent_sessions(mock_pool, "r-1", limit=3)

    assert result["count"] == 1
    assert result["sessions"][0]["session_id"] == "s-1"
    assert result["sessions"][0]["summary"] == "Looked for senior Python engineers."
    assert "agent_chat_sessions" in mock_pool.fetch.call_args[0][0]


@pytest.mark.asyncio
async def test_do_list_skills_batch_groups_matches_by_query():
    mock_pool = AsyncMock()
    mock_pool.fetch = AsyncMock(return_value=[
        {"term": "spark", "skill": "spark", "n": 12},
        {"term": "spark", "skill": "spark-sql", "n": 5},
        {"term": "airflow", "skill": "airflow", "n": 7},
    ])

    result = await do_list_skills_batch(mock_pool, ["spark", "airflow", "spark"])

    assert result["queries"] == ["spark", "airflow"]
    assert result["results"]["spark"][0]["skill"] == "spark"
    assert result["count"] == 3
    mock_pool.fetch.assert_awaited_once()


from pipeline.agent_tools import do_query_candidates_db


@pytest.mark.asyncio
async def test_do_query_candidates_db_blocks_dml():
    mock_pool = AsyncMock()
    for bad_sql in [
        "DELETE FROM candidates",
        "UPDATE candidates SET status='inactive'",
        "DROP TABLE candidates",
        "INSERT INTO candidates VALUES (1)",
        "TRUNCATE candidates",
        "ALTER TABLE candidates ADD COLUMN x int",
    ]:
        result = await do_query_candidates_db(mock_pool, bad_sql)
        assert "error" in result, f"Expected error for: {bad_sql}"
        assert "not allowed" in result["error"].lower()
    mock_pool.fetch.assert_not_called()


@pytest.mark.asyncio
async def test_do_query_candidates_db_injects_limit():
    mock_pool = AsyncMock()
    mock_pool.fetch = AsyncMock(return_value=[{"id": "c-1", "full_name": "Ada"}])
    result = await do_query_candidates_db(mock_pool, "SELECT id, full_name FROM candidates")
    assert result["rows"][0]["full_name"] == "Ada"
    call_sql = mock_pool.fetch.call_args[0][0]
    assert "LIMIT 50" in call_sql.upper()


@pytest.mark.asyncio
async def test_do_query_candidates_db_hydrates_candidate_rows_for_display():
    candidate_id = "32d2c0db-8901-4118-8bca-b8d6bf92dc6f"
    mock_pool = AsyncMock()
    mock_pool.fetch = AsyncMock(side_effect=[
        [{"id": candidate_id, "full_name": "Ada", "city": "Bangalore"}],
        [{
            "id": candidate_id,
            "full_name": "Ada",
            "email": "ada@example.com",
            "city": "Bangalore",
            "country": "India",
            "years_exp": 8,
            "salary_min": None,
            "salary_max": None,
            "skills": ["python"],
            "best_chunk": "Resume mentions Python.",
            "doc_type": "resume",
            "document_title": "Ada Resume",
        }],
    ])

    result = await do_query_candidates_db(mock_pool, "SELECT id, full_name, city FROM candidates")

    assert result["candidate_ids"] == [candidate_id]
    assert result["display_results"][0]["id"] == candidate_id
    assert result["display"]["result_kind"] == "selected"
    assert mock_pool.fetch.await_count == 2


@pytest.mark.asyncio
async def test_do_query_candidates_db_blocks_disallowed_tables():
    mock_pool = AsyncMock()
    result = await do_query_candidates_db(mock_pool, "SELECT * FROM recruiter_preferences")
    assert "error" in result
    assert "not allowed" in result["error"].lower()
