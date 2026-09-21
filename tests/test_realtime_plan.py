from pipeline.realtime_plan import BRANCHES, SearchPlanRevision, diff_revisions


def test_a_query_rewrite_leaves_the_skill_and_count_branches_reusable() -> None:
    """Selective cancellation is real, but only where the inputs really differ.

    Rewriting the question changes what the dense and lexical branches embed.
    The skill branch scores the same skill lists over the same eligible pool,
    and the count branch *is* the eligible pool, so both survive untouched.
    """
    old = SearchPlanRevision.create(
        query="python postgres engineer", must_skills=["python"], city="Pune"
    )
    new = old.patch(query="staff level python engineer")

    diff = diff_revisions(old, new)

    assert diff.replaced == {"vector", "bm25"}
    assert diff.reused == {"skills", "sql"}
    assert diff.changed_fields == {"query"}


def test_a_hard_filter_change_invalidates_every_branch() -> None:
    """The eligibility clause is inlined into all four branch queries.

    A city change is therefore not "an SQL change": every branch was filtered
    by the old city, and rows fetched under a different eligibility clause are
    not the same rows. Claiming otherwise would let the session answer with
    candidates who no longer qualify.
    """
    old = SearchPlanRevision.create(query="python postgres engineer", city="Pune")
    new = old.patch(city="Bengaluru")

    diff = diff_revisions(old, new)

    assert diff.replaced == set(BRANCHES)
    assert diff.reused == set()
    assert diff.changed_fields == {"city"}


def test_a_hard_skill_filter_change_invalidates_every_branch() -> None:
    """A must-have skill is part of the eligibility clause, not a soft hint."""
    old = SearchPlanRevision.create(query="backend engineer", must_skills=["mysql"])
    new = old.patch(must_skills=["mysql", "sql"])

    diff = diff_revisions(old, new)

    assert diff.replaced == set(BRANCHES)


def test_a_preferred_skill_change_replaces_the_skill_branch_only() -> None:
    old = SearchPlanRevision.create(query="backend engineer", should_skills=["kubernetes"])
    new = old.patch(should_skills=["kubernetes", "terraform"])

    diff = diff_revisions(old, new)

    assert diff.replaced == {"skills"}
    assert {"vector", "bm25", "sql"}.issubset(diff.reused)


def test_a_keyword_policy_change_replaces_only_the_lexical_branch() -> None:
    old = SearchPlanRevision.create(query="backend engineer", keyword_policy="auto")
    new = old.patch(keyword_policy="off")

    diff = diff_revisions(old, new)

    assert diff.replaced == {"bm25"}
    assert {"vector", "skills", "sql"}.issubset(diff.reused)


def test_a_soft_target_hint_keeps_every_branch_but_changes_the_plan() -> None:
    """A preference only steers fusion, so no branch is invalidated.

    The plan-level key still changes, because the fused ranking does. That
    combination is the useful one: the session re-fuses cached branch rows
    instead of re-querying the database.
    """
    old = SearchPlanRevision.create(query="backend engineer")
    new = old.patch(should_locations=["berlin"])

    diff = diff_revisions(old, new)

    assert diff.replaced == set()
    assert diff.reused == set(BRANCHES)
    assert new.plan_fingerprint != old.plan_fingerprint


def test_every_branch_fingerprint_carries_the_shared_eligibility_clause() -> None:
    """The shared component is visible, not implicit.

    If a branch fingerprint omitted the filter, the session could reuse rows
    that were fetched under a different eligibility clause. Each branch key
    must move when the filter moves.
    """
    base = SearchPlanRevision.create(query="backend engineer", city="Pune")
    narrowed = base.patch(min_years_exp=5)

    assert narrowed.filter_fingerprint != base.filter_fingerprint
    for branch in BRANCHES:
        assert (
            narrowed.branch_fingerprints[branch] != base.branch_fingerprints[branch]
        ), f"{branch} ignored a change to the shared eligibility clause"


def test_semantic_query_change_replaces_lexical_and_vector_branches() -> None:
    old = SearchPlanRevision.create(query="python backend engineer")
    new = old.patch(query="machine learning engineer")

    diff = diff_revisions(old, new)

    assert diff.replaced == {"vector", "bm25"}
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
    assert diff.reused == set(BRANCHES)
    assert diff.changed_fields == set()
    assert new.plan_fingerprint == old.plan_fingerprint


def test_initial_revision_marks_every_branch_new() -> None:
    revision = SearchPlanRevision.create(query="data engineer", min_years_exp=5)

    diff = diff_revisions(None, revision)

    assert diff.new == set(BRANCHES)
    assert diff.reused == set()
