from pipeline.planner_fallback import fallback_plan
import pytest

from pipeline.retrieve_bm25 import (
    _build_paradedb_match_query,
    _build_tsquery,
    _semantic_fallback_terms,
    _should_use_fts_first,
    retrieve_bm25,
)
from pipeline.spec import CanonicalSearchSpec


def test_semantic_fallback_bm25_anchors_role_phrase():
    terms = _semantic_fallback_terms(
        "find hotel manager with front-desk operations guest handling "
        "staff supervision and booking systems experience"
    )

    tsquery = _build_tsquery(terms, derived_from_semantic_query=True)

    assert tsquery.startswith("hotel & (")
    assert "manager | front-desk" in tsquery
    assert "find" not in tsquery
    assert "experience" not in tsquery


def test_explicit_lexical_terms_keep_or_recall_query():
    tsquery = _build_tsquery(["python", "backend"], derived_from_semantic_query=False)

    assert tsquery == "python | backend"


def test_paradedb_match_query_uses_plain_match_text():
    query = _build_paradedb_match_query(
        ["python", "backend"],
        derived_from_semantic_query=False,
    )

    assert query == "python backend"


def test_paradedb_semantic_fallback_puts_specific_anchor_first():
    terms = _semantic_fallback_terms(
        "find hotel manager with front-desk operations guest handling"
    )

    query = _build_paradedb_match_query(terms, derived_from_semantic_query=True)

    assert query.startswith("hotel ")
    assert "front-desk" in query
    assert "experience" not in query


def test_country_only_bm25_uses_fts_first_plan():
    spec = fallback_plan(
        "developer with information technology services background",
        {"country": "USA"},
    )

    assert _should_use_fts_first(spec) is True


def test_skill_filtered_bm25_keeps_candidate_first_plan():
    spec = fallback_plan(
        "full-stack React TypeScript developer",
        {"country": "USA", "skills": ["react"]},
    )

    assert _should_use_fts_first(spec) is False


class _FakePool:
    def __init__(self):
        self.sql = ""
        self.params = ()

    async def fetch(self, sql, *params):
        self.sql = sql
        self.params = params
        return []


@pytest.mark.asyncio
async def test_paradedb_backend_uses_pg_search_score_and_overfetch():
    pool = _FakePool()
    spec = CanonicalSearchSpec(
        input_type="query",
        intent="candidate_search",
        semantic_query="python backend engineer",
        lexical_terms=["python", "backend"],
    )

    rows = await retrieve_bm25(
        spec,
        "status = ANY($1::text[])",
        [["active"]],
        pool,
        top_k=10,
        backend="paradedb",
        overfetch_factor=4,
        overfetch_min=60,
    )

    assert rows == []
    assert "dc.content ||| $2" in pool.sql
    assert "pdb.score(dc.id)" in pool.sql
    assert "LIMIT 60" in pool.sql
    assert pool.params[1] == "python backend"
