from pipeline.realtime_plan import SearchPlanRevision, diff_revisions


def test_location_change_replaces_only_sql_branch() -> None:
    old = SearchPlanRevision.create(query="python postgres engineer", city="Pune")
    new = old.patch(city="Bengaluru")

    diff = diff_revisions(old, new)

    assert diff.reused == {"vector", "bm25", "skills"}
    assert diff.replaced == {"sql"}
    assert diff.changed_fields == {"city"}


def test_preferred_skill_change_replaces_skill_branch_only() -> None:
    old = SearchPlanRevision.create(query="backend engineer", should_skills=["kubernetes"])
    new = old.patch(should_skills=["kubernetes", "terraform"])

    diff = diff_revisions(old, new)

    assert "skills" in diff.replaced
    assert {"vector", "bm25", "sql"}.issubset(diff.reused)


def test_semantic_query_change_replaces_lexical_and_vector_branches() -> None:
    old = SearchPlanRevision.create(query="python backend engineer")
    new = old.patch(query="machine learning engineer")

    diff = diff_revisions(old, new)

    assert {"vector", "bm25"}.issubset(diff.replaced)
    assert new.parent_revision_id == old.revision_id


def test_revision_normalizes_equivalent_values_before_fingerprinting() -> None:
    old = SearchPlanRevision.create(
        query="  Senior   Python Engineer ",
        city="Bangalore",
        must_skills=["PostgreSQL", "python"],
    )
    new = old.patch(
        query="senior python engineer",
        city="bangalore",
        must_skills=["python", "postgresql"],
    )

    diff = diff_revisions(old, new)

    assert diff.replaced == set()
    assert diff.reused == {"vector", "bm25", "skills", "sql"}
    assert diff.changed_fields == set()


def test_initial_revision_marks_every_branch_new() -> None:
    revision = SearchPlanRevision.create(query="data engineer", min_years_exp=5)

    diff = diff_revisions(None, revision)

    assert diff.new == {"vector", "bm25", "skills", "sql"}
    assert diff.reused == set()
