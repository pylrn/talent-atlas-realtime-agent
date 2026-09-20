import pytest

# tests/test_memory.py
from pipeline.memory import (
    MemoryFact, MemoryObservation, build_agent_memory_block, load_memory,
)


def _fact(text):
    return MemoryFact(id="f1", content=text, category="other", source="manual")


def _obs(text, conf, id="o1"):
    return MemoryObservation(id=id, content=text, category="skill",
                             confidence=conf, evidence_count=5)


def test_agent_block_empty_when_no_memory():
    assert build_agent_memory_block([], []) == ""


def test_agent_block_lists_facts_and_rules():
    block = build_agent_memory_block([_fact("prefers startup experience")], [])
    assert "prefers startup experience" in block
    assert "NEVER apply them to a search" in block          # contract present
    assert "ONE observation question per session" in block


def test_agent_block_includes_only_confident_observations():
    obs = [_obs("often accepts React candidates", 0.85, "o1"),
           _obs("weak guess", 0.4, "o2")]
    block = build_agent_memory_block([], obs)
    assert "often accepts React candidates" in block
    assert "85%" in block
    assert "o1" in block                                  # id shown for the tool call
    assert "weak guess" not in block


def test_agent_block_caps_observations_at_three():
    obs = [_obs(f"observation number {i}", 0.9, f"o{i}") for i in range(5)]
    block = build_agent_memory_block([], obs)
    assert sum(f"observation number {i}" in block for i in range(5)) == 3


@pytest.mark.asyncio
async def test_load_memory_defaults_disabled_when_preferences_row_missing():
    from unittest.mock import AsyncMock

    pool = AsyncMock()
    pool.fetchrow.return_value = None

    bundle = await load_memory(pool, "11111111-1111-1111-1111-111111111111")

    assert bundle.enabled is False
    assert bundle.facts == []
    assert bundle.observations == []
    pool.fetch.assert_not_called()
