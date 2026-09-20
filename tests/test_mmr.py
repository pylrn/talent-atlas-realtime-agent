"""Tests for MMR diversity selection."""

from pipeline.diversity import mmr_select, _skill_similarity
from pipeline.search_result import SearchResult


def _r(cid, score, skills):
    return SearchResult(candidate_id=cid, feature_score=score, skills=skills)


def test_returns_at_most_top_k():
    pool = [_r(f"c{i}", 90 - i, ["python"]) for i in range(10)]
    out = mmr_select(pool, top_k=3)
    assert len(out) == 3


def test_picks_highest_score_first():
    pool = [
        _r("a", 80, ["python"]),
        _r("b", 95, ["java"]),
        _r("c", 70, ["go"]),
    ]
    out = mmr_select(pool, top_k=1, lam=1.0)  # full relevance
    assert out[0].candidate_id == "b"


def test_diversity_breaks_duplicates():
    """With three near-duplicate skill sets and one different one,
    the diverse candidate should still surface in top 2 when lam < 1."""
    pool = [
        _r("dup1", 95, ["python", "django"]),
        _r("dup2", 94, ["python", "django"]),
        _r("dup3", 93, ["python", "django"]),
        _r("diff", 80, ["rust", "wasm"]),  # different stack
    ]
    out = mmr_select(pool, top_k=2, lam=0.5)
    ids = [r.candidate_id for r in out]
    assert "dup1" in ids
    assert "diff" in ids   # diversity should pull this into top 2


def test_skill_similarity_jaccard():
    a = _r("a", 0, ["python", "django", "postgres"])
    b = _r("b", 0, ["python", "django", "redis"])
    # intersection = {python, django} = 2, union = 4 → 0.5
    assert _skill_similarity(a, b) == 0.5


def test_skill_similarity_empty():
    a = _r("a", 0, [])
    b = _r("b", 0, [])
    assert _skill_similarity(a, b) == 0.0
