import pytest
from unittest.mock import AsyncMock
from pipeline.personalization import load_recruiter_profile, personalization_score, RecruiterProfile
from pipeline.search_result import SearchResult

@pytest.fixture
def mock_pool():
    return AsyncMock()

@pytest.mark.asyncio
async def test_empty_history_returns_none(mock_pool):
    mock_pool.fetch.return_value = []
    profile = await load_recruiter_profile(mock_pool, "rec-123")
    assert profile is None

def test_cold_start_neutral_score():
    cand = SearchResult(candidate_id="c1", skills=["Python"])
    
    # <5 outcomes -> cold start
    profile = RecruiterProfile(
        skill_affinities={"python": 1.0},
        location_affinities={},
        preferred_exp_range=(0, 10),
        total_outcomes=4
    )
    score = personalization_score(cand, profile)
    assert score == 0.5
    
    # None profile -> neutral
    assert personalization_score(cand, None) == 0.5

@pytest.mark.asyncio
async def test_skill_affinity_positive(mock_pool):
    mock_pool.fetch.return_value = [
        {"action": "shortlisted", "skills": ["Python", "Django"], "city": "NYC", "country": "US", "years_exp": 5},
        {"action": "saved", "skills": ["Python", "FastAPI"], "city": "NYC", "country": "US", "years_exp": 6},
    ] * 3 # 6 outcomes to pass cold-start
    
    profile = await load_recruiter_profile(mock_pool, "rec-123")
    assert profile is not None
    assert profile.total_outcomes == 6
    assert profile.skill_affinities["python"] == 1.0  # Normalized to max
    assert profile.skill_affinities["django"] > 0
    assert profile.skill_affinities["fastapi"] > 0
    
    # Score a candidate with Python
    cand1 = SearchResult(candidate_id="c1", skills=["Python", "Go"], city="NYC", country="US", years_exp=5)
    score1 = personalization_score(cand1, profile)
    
    # Score a candidate without Python
    cand2 = SearchResult(candidate_id="c2", skills=["Ruby"], city="NYC", country="US", years_exp=5)
    score2 = personalization_score(cand2, profile)
    
    assert score1 > score2

@pytest.mark.asyncio
async def test_negative_outcomes_penalize(mock_pool):
    mock_pool.fetch.return_value = [
        {"action": "rejected", "skills": ["Java", "Spring"], "city": "SF", "country": "US", "years_exp": 2},
    ] * 5
    
    profile = await load_recruiter_profile(mock_pool, "rec-123")
    assert profile.skill_affinities["java"] == -1.0 # Max penalty
    
    cand = SearchResult(candidate_id="c1", skills=["Java"], city="LA", country="US", years_exp=5)
    score = personalization_score(cand, profile)
    
    # Should be pulled down by negative skill affinity
    assert score < 0.5

@pytest.mark.asyncio
async def test_location_affinity(mock_pool):
    mock_pool.fetch.return_value = [
        {"action": "contacted", "skills": [], "city": "Bangalore", "country": "IN", "years_exp": 5},
    ] * 5
    
    profile = await load_recruiter_profile(mock_pool, "rec-123")
    assert profile.location_affinities["bangalore|in"] == 1.0
    
    cand1 = SearchResult(candidate_id="c1", city="Bangalore", country="IN", years_exp=5)
    cand2 = SearchResult(candidate_id="c2", city="Mumbai", country="IN", years_exp=5)
    
    assert personalization_score(cand1, profile) > personalization_score(cand2, profile)

def test_score_integration():
    from pipeline.feature_ranker import _compute_signals
    from pipeline.spec import CanonicalSearchSpec, MustFilters, ShouldFilters
    
    r = SearchResult(
        candidate_id="c1",
        skills=["Python"],
        city="NYC",
        country="US",
        years_exp=5,
        fused_rrf_score=0.035 # max retrieval
    )
    
    spec = CanonicalSearchSpec(
        input_type="query",
        intent="candidate_search",
        must=MustFilters(skills=["Python"]),
        should=ShouldFilters(skills=[])
    )
    
    # Without personalization
    signals_none = _compute_signals(r, spec, {"Python"}, 1, 1, recruiter_profile=None)
    assert signals_none["personalization"] == 0.5
    
    # With positive personalization
    profile_good = RecruiterProfile(
        skill_affinities={"python": 1.0},
        location_affinities={"nyc|us": 1.0},
        preferred_exp_range=(3, 7),
        total_outcomes=5
    )
    signals_good = _compute_signals(r, spec, {"Python"}, 1, 1, recruiter_profile=profile_good)
    assert signals_good["personalization"] > 0.8
    
    # With negative personalization
    profile_bad = RecruiterProfile(
        skill_affinities={"python": -1.0},
        location_affinities={"la|us": 1.0},
        preferred_exp_range=(10, 15),
        total_outcomes=5
    )
    signals_bad = _compute_signals(r, spec, {"Python"}, 1, 1, recruiter_profile=profile_bad)
    assert signals_bad["personalization"] < 0.3

def test_feature_weight_normalization():
    from pipeline.feature_ranker import _WEIGHTS_WITH_CE, _WEIGHTS_WITHOUT_CE
    
    # Ensure weights still sum to 1.0
    assert abs(sum(_WEIGHTS_WITH_CE.values()) - 1.0) < 1e-5
    assert abs(sum(_WEIGHTS_WITHOUT_CE.values()) - 1.0) < 1e-5
