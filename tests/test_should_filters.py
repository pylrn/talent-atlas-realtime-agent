from pipeline.planner_fallback import fallback_plan
from pipeline.spec import CanonicalSearchSpec
from pipeline.validator import _merge_explicit


def _empty_spec():
    return CanonicalSearchSpec(
        input_type="query",
        intent="candidate_search",
        semantic_query="frontend engineer react vue",
        search_targets=["candidate_profile", "resume_chunks"],
    )


def test_merge_explicit_accepts_nested_should_fields():
    spec = _empty_spec()

    out = _merge_explicit(
        spec,
        {
            "skills": ["python"],
            "should": {
                "skills": ["aws", "kubernetes"],
                "themes": ["api design"],
                "roles": ["backend engineer"],
                "locations": ["remote"],
            },
        },
    )

    assert out.must.skills == ["python"]
    assert out.should.skills == ["aws", "kubernetes"]
    assert out.should.themes == ["api design"]
    assert out.should.roles == ["backend engineer"]
    assert out.should.locations == ["remote"]


def test_merge_explicit_empty_skills_clears_fallback_must_skills():
    spec = fallback_plan(
        "frontend engineer react vue",
        explicit_filters={
            "skills": [],
            "should": {"skills": ["react", "vue"]},
        },
    )

    assert spec.must.skills == []
    assert spec.should.skills == ["react", "vue"]
    assert spec.lexical_terms == ["react", "vue"]
