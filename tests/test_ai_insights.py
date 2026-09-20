import pytest

from pipeline import settings
from pipeline.ai_insights import (
    AIInsightService,
    CandidateInsightInput,
    parse_ai_insight_response,
)


def _candidate(candidate_id: str, full_name: str) -> CandidateInsightInput:
    return CandidateInsightInput(
        candidate_id=candidate_id,
        full_name=full_name,
        city="Bengaluru",
        country="India",
        salary_min=120000,
        salary_max=160000,
        years_exp=6,
        skills=["python", "fastapi", "search"],
        best_chunk="Built Python search services with FastAPI and Postgres.",
        similarity_score=0.82,
        doc_type="resume",
        document_title="Resume",
        supporting_chunks=[
            {
                "doc_type": "transcript",
                "document_title": "Technical interview",
                "content": "Explained vector search tradeoffs and team leadership.",
            }
        ],
        rerank_score=0.91,
        rank_score=0.74,
    )


def test_parse_ai_insight_response_validates_ids_and_clamps_scores():
    candidates = [_candidate("candidate-1", "Ada"), _candidate("candidate-2", "Grace")]
    raw = """
    ```json
    {
      "best_candidate_id": "hallucinated",
      "summary": "Ada has the strongest evidence for the role.",
      "matrix": [
        {
          "candidate_id": "candidate-1",
          "fit_score": 124,
          "recommendation": "Strongest fit",
          "strengths": ["Python backend evidence"],
          "evidence": ["Built Python search services"],
          "gaps": [],
          "interview_probe": "Ask about scaling vector search."
        },
        {
          "candidate_id": "hallucinated",
          "fit_score": 80,
          "recommendation": "Ignore me",
          "strengths": ["Not in result set"],
          "evidence": ["Invented"],
          "gaps": [],
          "interview_probe": "No"
        }
      ]
    }
    ```
    """

    parsed = parse_ai_insight_response(raw, candidates, provider="gemini", model="gemini-test")

    assert parsed.status == "ok"
    assert parsed.best_candidate_id == "candidate-1"
    assert parsed.summary == "Ada has the strongest evidence for the role."
    assert len(parsed.matrix) == 1
    assert parsed.matrix[0].fit_score == 100
    assert parsed.matrix[0].full_name == "Ada"


def test_parse_ai_insight_response_returns_error_for_invalid_json():
    parsed = parse_ai_insight_response(
        "not json",
        [_candidate("candidate-1", "Ada")],
        provider="gemini",
        model="gemini-test",
    )

    assert parsed.status == "error"
    assert parsed.best_candidate_id is None
    assert parsed.matrix == []
    assert "Could not parse" in parsed.summary


def test_parse_ai_insight_response_scales_fractional_fit_scores():
    candidates = [_candidate("candidate-1", "Ada")]
    raw = """
    {
      "best_candidate_id": "candidate-1",
      "summary": "Ada is a strong fit.",
      "matrix": [
        {
          "candidate_id": "candidate-1",
          "fit_score": 0.87,
          "recommendation": "Strong fit",
          "strengths": [],
          "evidence": [],
          "gaps": [],
          "interview_probe": ""
        }
      ]
    }
    """

    parsed = parse_ai_insight_response(raw, candidates, provider="gemini", model="gemini-test")

    assert parsed.matrix[0].fit_score == 87


@pytest.mark.asyncio
async def test_ai_insight_service_skips_network_when_key_is_missing(monkeypatch):
    calls = []

    async def fake_call(self, system_prompt, user_prompt):
        calls.append((system_prompt, user_prompt))
        return "{}"

    monkeypatch.setattr(settings, "google_api_key", "", raising=False)
    monkeypatch.setattr(AIInsightService, "_call_llm", fake_call)

    service = AIInsightService(provider="gemini", model="gemini-test")
    insight = await service.generate(
        query="senior python backend engineer",
        filters_applied={"country": "India"},
        candidates=[_candidate("candidate-1", "Ada")],
    )

    assert insight.status == "unavailable"
    assert insight.matrix == []
    assert calls == []


@pytest.mark.asyncio
async def test_ai_insight_service_returns_search_based_matrix_when_llm_fails(monkeypatch):
    async def failing_call(self, system_prompt, user_prompt, *args, **kwargs):
        raise RuntimeError("503 model overloaded")

    monkeypatch.setattr(settings, "google_api_key", "AIzaSyRealEnoughForTest", raising=False)
    monkeypatch.setattr(AIInsightService, "_call_llm", failing_call)

    service = AIInsightService(provider="gemini", model="gemini-test")
    insight = await service.generate(
        query="senior python backend engineer",
        filters_applied={"country": "India"},
        candidates=[_candidate("candidate-1", "Ada"), _candidate("candidate-2", "Grace")],
    )

    assert insight.status == "error"
    assert insight.best_candidate_id == "candidate-1"
    assert insight.matrix[0].candidate_id == "candidate-1"
    assert insight.matrix[0].fit_score == 91
    assert "search ranking" in insight.summary


@pytest.mark.asyncio
async def test_ai_insight_service_times_out_to_search_based_matrix(monkeypatch):
    async def slow_call(self, system_prompt, user_prompt, *args, **kwargs):
        import asyncio

        await asyncio.sleep(0.05)
        return "{}"

    monkeypatch.setattr(settings, "google_api_key", "AIzaSyRealEnoughForTest", raising=False)
    monkeypatch.setattr(AIInsightService, "_call_llm", slow_call)

    service = AIInsightService(provider="gemini", model="gemini-test", timeout_seconds=0.01)
    insight = await service.generate(
        query="senior python backend engineer",
        filters_applied={"country": "India"},
        candidates=[_candidate("candidate-1", "Ada")],
    )

    assert insight.status == "error"
    assert insight.best_candidate_id == "candidate-1"
    assert len(insight.matrix) == 1
    assert "timed out" in insight.error.lower()


@pytest.mark.asyncio
async def test_ai_insight_service_falls_back_when_llm_returns_invalid_json(monkeypatch):
    async def invalid_json_call(self, system_prompt, user_prompt, *args, **kwargs):
        return "{not valid json"

    monkeypatch.setattr(settings, "google_api_key", "AIzaSyRealEnoughForTest", raising=False)
    monkeypatch.setattr(AIInsightService, "_call_llm", invalid_json_call)

    service = AIInsightService(provider="gemini", model="gemini-test")
    insight = await service.generate(
        query="senior python backend engineer",
        filters_applied={"country": "India"},
        candidates=[_candidate("candidate-1", "Ada")],
    )

    assert insight.status == "error"
    assert insight.best_candidate_id == "candidate-1"
    assert len(insight.matrix) == 1
    assert "Could not parse" in insight.error


@pytest.mark.asyncio
async def test_ai_insight_service_does_not_verify_every_candidate_by_default(monkeypatch):
    calls = 0

    async def successful_call(self, system_prompt, user_prompt, *args, **kwargs):
        nonlocal calls
        calls += 1
        return """
        {
          "best_candidate_id": "candidate-1",
          "summary": "Ada is the strongest fit.",
          "comparative_reasoning": "Ada has stronger Python search evidence than Grace.",
          "matrix": [
            {
              "candidate_id": "candidate-1",
              "fit_score": 92,
              "recommendation": "Strong fit",
              "strengths": ["Python search services"],
              "evidence": ["Built Python search services"],
              "gaps": [],
              "interview_probe": "Ask about scaling FastAPI search."
            },
            {
              "candidate_id": "candidate-2",
              "fit_score": 81,
              "recommendation": "Good fit",
              "strengths": ["Vector search"],
              "evidence": ["Explained vector search tradeoffs"],
              "gaps": [],
              "interview_probe": "Ask about production search latency."
            }
          ]
        }
        """

    monkeypatch.setattr(settings, "google_api_key", "AIzaSyRealEnoughForTest", raising=False)
    monkeypatch.setattr(AIInsightService, "_call_llm", successful_call)

    service = AIInsightService(provider="gemini", model="gemini-test")
    insight = await service.generate(
        query="senior python backend engineer",
        filters_applied={"country": "India"},
        candidates=[_candidate("candidate-1", "Ada"), _candidate("candidate-2", "Grace")],
    )

    assert insight.status == "ok"
    assert calls == 1


@pytest.mark.asyncio
async def test_verify_grounding_calculates_hallucination_rate(monkeypatch):
    service = AIInsightService(provider="gemini", model="gemini-test")
    
    # Mock _call_llm to return verifications
    async def mock_call_llm(system_prompt, user_prompt, **kwargs):
        # Ada has 1 supported and 1 unsupported claim
        return """
        {
          "verifications": [
            {"claim": "Python backend evidence", "supported": true},
            {"claim": "AWS certified", "supported": false}
          ]
        }
        """
    monkeypatch.setattr(service, "_call_llm", mock_call_llm)
    
    from pipeline.ai_insights import AIInsightMatrixEntry
    entry = AIInsightMatrixEntry(
        candidate_id="candidate-1",
        full_name="Ada",
        fit_score=90,
        recommendation="Good fit",
        strengths=["Python backend evidence"],
        evidence=["AWS certified"]
    )
    
    candidate = _candidate("candidate-1", "Ada")
    
    rate = await service._verify_grounding(entry, candidate)
    # 1 out of 2 unsupported -> 0.5 hallucination rate
    assert rate == 0.5
