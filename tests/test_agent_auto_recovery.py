"""Auto-recovery: weak search results must carry a `recovery` diagnostic inline.

When `do_run_search` comes back weak (empty, low average score, planner asked to
clarify, or low planner confidence) it attaches a `recovery` block so the agent
reasons from real ranking signals instead of guessing. Strong results must NOT
carry it. `used_fallback` alone must not trigger recovery (regex often parses
simple queries fine).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pipeline.agent_tools as at
from pipeline.agent_session import AgentSession


def _result(score, paths=("dense", "bm25", "skill")):
    return SimpleNamespace(
        candidate_id="c" + str(score), feature_score=score,
        retrieval_paths=list(paths),
        rerank_score=0.5, fused_rrf_score=0.1, similarity_score=0.5,
        sort_basis="rerank_score", full_name="X", city="", country="",
        years_exp=5, skills=["python"], salary_min=None, salary_max=None,
        best_chunk="",
        explanation={
            "checks": {"required": [], "preferred": []},
            "score_breakdown": [],
            "match_tier": "Good",
            "match_score": score,
            "summary_line": "",
            "best_evidence": "",
        },
    )


class _FakeEngine:
    def __init__(self, results, conf=0.9, used_fallback=False, clarify=None):
        self._r, self._conf, self._fb, self._clar = results, conf, used_fallback, clarify

    async def smart_search(self, **kw):
        spec = SimpleNamespace(
            semantic_query="q", input_type="query", confidence=self._conf,
            used_fallback=self._fb, dropped_items=[], clarify=self._clar,
            must=SimpleNamespace(skills=["python"], city=None, country=None,
                                 min_years_exp=None, max_years_exp=None),
            should=SimpleNamespace(skills=[]),
        )
        return SimpleNamespace(results=self._r, spec=spec,
                               total_candidates_scanned=100, clarify=self._clar)


def _run(engine):
    sess = AgentSession(session_id="s", recruiter_id="r")
    with patch.object(at, "SearchEngine", lambda pool: engine):
        return asyncio.run(at.do_run_search(
            pool=None, session=sess, recruiter_id="r",
            query="python dev", filters={}, weights={},
        ))


def test_strong_results_have_no_recovery():
    out = _run(_FakeEngine([_result(80), _result(75), _result(72)]))
    assert "recovery" not in out


def test_low_score_triggers_recovery():
    out = _run(_FakeEngine([_result(30), _result(25)]))
    assert out["recovery"]["triggered"] is True
    assert any("low average score" in w for w in out["recovery"]["why"])
    assert "diagnostic" in out["recovery"]


def test_empty_results_trigger_recovery():
    out = _run(_FakeEngine([]))
    assert "zero results" in out["recovery"]["why"]


def test_low_confidence_triggers_recovery_even_with_ok_scores():
    out = _run(_FakeEngine([_result(80)], conf=0.4))
    assert any("low planner confidence" in w for w in out["recovery"]["why"])


def test_used_fallback_alone_does_not_trigger_recovery():
    # Strong scores + high confidence but the planner used regex fallback.
    out = _run(_FakeEngine([_result(80), _result(76)], used_fallback=True))
    assert "recovery" not in out
