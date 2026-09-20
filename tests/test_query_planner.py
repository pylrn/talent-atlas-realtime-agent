from pipeline.query_planner import parse_planner_response
from pipeline.search import SearchFilters


def test_parse_planner_response_extracts_query_filters_and_drops_doc_types():
    raw = """
    ```json
    {
      "query": "python backend api engineer",
      "filters": {
        "location": "India",
        "skills": ["Python", " FastAPI ", ""],
        "skills_match": "and",
        "min_years_exp": 4,
        "doc_types": ["resume"]
      },
      "confidence": 0.82,
      "reasoning": "The query names backend skills and India."
    }
    ```
    """

    planned = parse_planner_response(raw, "original query", SearchFilters(), min_confidence=0.4)

    assert planned.used_fallback is False
    assert planned.query == "python backend api engineer"
    assert planned.filters.location == "India"
    assert planned.filters.skills == ["python", "fastapi"]
    assert planned.filters.skills_match == "and"
    assert planned.filters.min_years_exp == 4
    assert planned.filters.doc_types is None
    assert planned.confidence == 0.82


def test_parse_planner_response_falls_back_for_invalid_or_low_confidence_output():
    base_filters = SearchFilters(location="Pune", min_salary=90000)

    invalid = parse_planner_response("{not json", "python dev", base_filters)
    low_confidence = parse_planner_response(
        '{"query":"java","filters":{"location":"USA"},"confidence":0.1}',
        "python dev",
        base_filters,
        min_confidence=0.4,
    )

    for planned in [invalid, low_confidence]:
        assert planned.used_fallback is True
        assert planned.query == "python dev"
        assert planned.filters.location == "Pune"
        assert planned.filters.min_salary == 90000


def test_parse_planner_response_preserves_explicit_user_filters_over_llm_filters():
    raw = """
    {
      "query": "senior java engineer",
      "filters": {
        "location": "USA",
        "country": "USA",
        "skills": ["java"],
        "skills_match": "or",
        "min_years_exp": 10,
        "max_salary": 200000
      },
      "confidence": 0.9
    }
    """
    base_filters = SearchFilters(
        location="India",
        country="India",
        skills=["python"],
        skills_match="and",
        min_salary=100000,
    )

    planned = parse_planner_response(raw, "python engineer", base_filters)

    assert planned.query == "senior java engineer"
    assert planned.filters.location == "India"
    assert planned.filters.country == "India"
    assert planned.filters.skills == ["python"]
    assert planned.filters.skills_match == "and"
    assert planned.filters.min_salary == 100000
    assert planned.filters.min_years_exp == 10
    assert planned.filters.max_salary == 200000
