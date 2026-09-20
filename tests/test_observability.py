"""Tests for the Langfuse facade. Search must work identically whether
Langfuse is enabled, disabled, or failing."""

from unittest.mock import MagicMock, patch

from pipeline import observability as obs


def test_score_name_enum_has_expected_members():
    # Closed set of score names. Adding a new score requires adding to enum.
    expected = {
        "REQUEST_LATENCY_MS",
        "SEARCH_CLICKED", "SEARCH_ZERO_RESULTS", "SEARCH_LATENCY_MS",
        "SEARCH_QUERY_UNDERSTANDING_LATENCY_MS", "SEARCH_RETRIEVAL_LATENCY_MS",
        "SEARCH_RANKING_LATENCY_MS", "SEARCH_DENSE_LATENCY_MS",
        "SEARCH_KEYWORD_LATENCY_MS", "SEARCH_SKILL_LATENCY_MS",
        "SEARCH_COUNT_LATENCY_MS", "SEARCH_ENRICHMENT_LATENCY_MS",
        "SEARCH_DB_POOL_WAIT_MAX_MS", "SEARCH_DB_ROUNDTRIP_MAX_MS",
        "SEARCH_DB_ROUNDTRIP_TOTAL_MS", "SEARCH_DB_APP_OVERHEAD_TOTAL_MS",
        "CACHE_HIT_RATE_REQUEST", "CACHE_L1_HITS_REQUEST",
        "CACHE_L2_HITS_REQUEST", "CACHE_MISSES_REQUEST",
        "SEARCH_RESULT_COUNT", "SEARCH_TOP_SCORE",
        "RETRIEVAL_RELEVANCE",
        "PERSONALIZATION_ACCEPTED",
        "INSIGHTS_LATENCY_MS", "LLM_LATENCY_MS", "LLM_FALLBACK_USED",
        "RECRUITER_ACTION", "POSITIVE_OUTCOME",
        "PLANNER_FILTER_ACCURACY", "TOP1", "MRR10", "NDCG10",
        "HALLUCINATION_RATE",
    }
    assert {m.name for m in obs.ScoreName} == expected


def test_is_enabled_returns_false_when_setting_off(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", False)
    assert obs.is_enabled() is False


def test_record_score_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", False)
    # Must not raise even though no client is initialised.
    obs.record_score(trace_id="t1", name=obs.ScoreName.SEARCH_RESULT_COUNT, value=5)


def test_record_score_calls_client_when_enabled(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    fake = MagicMock()
    monkeypatch.setattr(obs, "_get_client", lambda: fake)
    obs.record_score(trace_id="t1", name=obs.ScoreName.SEARCH_RESULT_COUNT, value=5, comment="ok")
    fake.create_score.assert_called_once_with(
        trace_id="t1", name="search.result_count", value=5, comment="ok")


def test_record_score_swallows_client_exceptions(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    fake = MagicMock()
    fake.create_score.side_effect = RuntimeError("network down")
    monkeypatch.setattr(obs, "_get_client", lambda: fake)
    # Must not raise — Langfuse failures must never fail search.
    obs.record_score(trace_id="t1", name=obs.ScoreName.SEARCH_RESULT_COUNT, value=5)


def test_should_sample_always_true_for_errors(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_sample_rate", 0.0)
    assert obs.should_sample(latency_ms=50, status_code=500) is True


def test_should_sample_always_true_for_slow(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_sample_rate", 0.0)
    monkeypatch.setattr(obs._settings, "langfuse_slow_threshold_ms", 1000)
    assert obs.should_sample(latency_ms=1500, status_code=200) is True


def test_should_sample_always_true_for_outcomes_route(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_sample_rate", 0.0)
    assert obs.should_sample(latency_ms=100, status_code=200, route="/outcomes") is True


def test_should_sample_respects_sample_rate_for_happy_path(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_sample_rate", 0.0)
    monkeypatch.setattr(obs._settings, "langfuse_slow_threshold_ms", 1000)
    # Rate 0 → never sample fast successes.
    assert obs.should_sample(latency_ms=100, status_code=200) is False
    monkeypatch.setattr(obs._settings, "langfuse_sample_rate", 1.0)
    assert obs.should_sample(latency_ms=100, status_code=200) is True


def test_redact_strips_emails_and_long_text_in_prod_mode(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_trace_content_mode", "prod_redacted")
    raw = {"query": "alice@example.com python developer",
           "resume_text": "x" * 5000}
    cleaned = obs.redact(raw)
    assert "alice@example.com" not in str(cleaned)
    assert len(str(cleaned["resume_text"])) <= 200


def test_redact_passthrough_in_dev_mode(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_trace_content_mode", "dev_full")
    raw = {"query": "alice@example.com"}
    assert obs.redact(raw) == raw

def test_init_langfuse_idempotent(monkeypatch):
    # Calling init twice should not create two clients.
    monkeypatch.setattr(obs, "_client", None)
    monkeypatch.setattr(obs, "_initialized", False)
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    monkeypatch.setattr(obs._settings, "langfuse_public_key", "pk")
    monkeypatch.setattr(obs._settings, "langfuse_secret_key", "sk")
    with patch("langfuse.Langfuse") as FakeLF:
        obs.init_langfuse()
        obs.init_langfuse()
        assert FakeLF.call_count == 1


def test_init_langfuse_swallows_init_errors(monkeypatch):
    monkeypatch.setattr(obs, "_client", None)
    monkeypatch.setattr(obs, "_initialized", False)
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    with patch("langfuse.Langfuse", side_effect=RuntimeError("bad key")):
        # Must not raise.
        obs.init_langfuse()
    assert obs._client is None


def test_get_planner_system_prompt_returns_local_when_disabled(monkeypatch):
    """When Langfuse is off, get_planner_system_prompt must return the local
    PLANNER_SYSTEM_PROMPT constant with version=None."""
    monkeypatch.setattr(obs._settings, "langfuse_enabled", False)
    from pipeline.prompts import get_planner_system_prompt, PLANNER_SYSTEM_PROMPT
    prompt, version, lf_prompt = get_planner_system_prompt()
    assert prompt is PLANNER_SYSTEM_PROMPT
    assert version is None
    assert lf_prompt is None


def test_get_planner_system_prompt_returns_local_on_langfuse_error(monkeypatch):
    """When Langfuse is enabled but get_prompt raises, we fall back to local."""
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    fake_client = MagicMock()
    fake_client.get_prompt.side_effect = RuntimeError("network down")
    monkeypatch.setattr(obs, "_get_client", lambda: fake_client)
    from pipeline.prompts import get_planner_system_prompt, PLANNER_SYSTEM_PROMPT
    prompt, version, lf_prompt = get_planner_system_prompt()
    assert prompt is PLANNER_SYSTEM_PROMPT
    assert version is None
    assert lf_prompt is None


def test_get_prompt_returns_none_when_disabled(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", False)
    assert obs.get_prompt("recruiter-agent") is None


def test_get_prompt_fetches_labelled_prompt_when_enabled(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    fake_client = MagicMock()
    fake_prompt = object()
    fake_client.get_prompt.return_value = fake_prompt
    monkeypatch.setattr(obs, "_get_client", lambda: fake_client)

    assert obs.get_prompt("recruiter-agent", label="staging") is fake_prompt
    fake_client.get_prompt.assert_called_once_with(
        "recruiter-agent",
        label="staging",
        cache_ttl_seconds=300,
    )


def test_get_prompt_negative_caches_missing_prompt(monkeypatch):
    monkeypatch.setattr(obs._settings, "langfuse_enabled", True)
    fake_client = MagicMock()
    fake_client.get_prompt.side_effect = RuntimeError("prompt not found")
    monkeypatch.setattr(obs, "_get_client", lambda: fake_client)
    monkeypatch.setattr(obs, "_missing_prompt_cache", {})

    assert obs.get_prompt("recruiter-agent", label="production") is None
    assert obs.get_prompt("recruiter-agent", label="production") is None

    fake_client.get_prompt.assert_called_once_with(
        "recruiter-agent",
        label="production",
        cache_ttl_seconds=300,
    )
