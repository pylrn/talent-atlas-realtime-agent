"""Selective branch cancellation.

The behaviour under test is the difference between an agent that restarts on
every interruption and one that only redoes what actually changed. A branch is
identified by its fingerprint, so "did this revision invalidate the branch" is a
fingerprint comparison rather than a guess.
"""

from __future__ import annotations

import asyncio

import pytest

from pipeline.realtime_branches import BranchExecutor


class _Runner:
    """A branch runner that can be held open so a test controls the timing."""

    def __init__(self, *, gate: asyncio.Event | None = None, fail: set[str] | None = None) -> None:
        self.calls: list[str] = []
        self.cancelled: list[str] = []
        self.gate = gate
        self.fail = fail or set()
        self.started: dict[str, asyncio.Event] = {}

    def mark(self, branch: str) -> asyncio.Event:
        return self.started.setdefault(branch, asyncio.Event())

    async def run(self, branch: str, value: str = "rows"):
        self.calls.append(branch)
        self.mark(branch).set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            if branch in self.fail:
                raise RuntimeError(f"{branch} failed")
        except asyncio.CancelledError:
            self.cancelled.append(branch)
            raise
        return value


@pytest.mark.asyncio
async def test_a_cold_revision_runs_every_branch():
    executor = BranchExecutor()
    runner = _Runner()

    values = {
        branch: await executor.acquire(
            branch, f"fp-{branch}", invalidated=True, runner=lambda b=branch: runner.run(b)
        )
        for branch in ("vector", "bm25", "skills", "sql")
    }

    assert sorted(runner.calls) == ["bm25", "skills", "sql", "vector"]
    assert set(values) == {"vector", "bm25", "skills", "sql"}
    assert executor.stats["branches_run"] == 4


@pytest.mark.asyncio
async def test_an_unchanged_branch_is_reused_without_running_again():
    executor = BranchExecutor()
    runner = _Runner()

    first = await executor.acquire("vector", "fp-1", invalidated=True, runner=lambda: runner.run("vector"))
    second = await executor.acquire("vector", "fp-1", invalidated=False, runner=lambda: runner.run("vector"))

    assert second == first
    assert runner.calls == ["vector"], "the branch was re-run despite an identical fingerprint"
    assert executor.decisions["vector"] == "reused"


@pytest.mark.asyncio
async def test_a_branch_still_in_flight_and_still_valid_is_preserved():
    """The core of selective cancellation.

    Two revisions disagree about which branches changed, so the second one must
    keep the first one's in-flight work instead of cancelling and redoing it.
    """
    executor = BranchExecutor()
    gate = asyncio.Event()
    runner = _Runner(gate=gate)

    first = asyncio.create_task(
        executor.acquire("vector", "fp-1", invalidated=True, runner=lambda: runner.run("vector"))
    )
    await runner.mark("vector").wait()

    second = asyncio.create_task(
        executor.acquire("vector", "fp-1", invalidated=False, runner=lambda: runner.run("vector"))
    )
    await asyncio.sleep(0)
    gate.set()

    assert await first == "rows"
    assert await second == "rows"
    assert runner.calls == ["vector"], "the preserved branch was started a second time"
    assert runner.cancelled == []
    assert executor.stats["branches_preserved"] == 1


@pytest.mark.asyncio
async def test_an_invalidated_branch_is_cancelled_promptly():
    """A branch whose inputs changed must not keep the new revision waiting."""
    executor = BranchExecutor()
    gate = asyncio.Event()
    runner = _Runner(gate=gate)

    stale = asyncio.create_task(
        executor.acquire("vector", "fp-old", invalidated=True, runner=lambda: runner.run("vector"))
    )
    await runner.mark("vector").wait()

    fresh = asyncio.create_task(
        executor.acquire("vector", "fp-new", invalidated=True, runner=lambda: runner.run("vector"))
    )
    await asyncio.sleep(0)
    gate.set()

    with pytest.raises(asyncio.CancelledError):
        await stale
    assert await fresh == "rows"
    assert runner.cancelled == ["vector"]
    assert executor.stats["branches_cancelled"] == 1


@pytest.mark.asyncio
async def test_two_revisions_changing_different_branches_keep_each_others_work():
    """A realistic interruption: the recruiter keeps adding detail.

    The first revision changed the query, so the vector branch is in flight. The
    second revision changes skills instead, which leaves the query untouched, so
    the in-flight vector work is still exactly right and must survive.
    """
    executor = BranchExecutor()
    gate = asyncio.Event()
    runner = _Runner(gate=gate)

    vector = asyncio.create_task(
        executor.acquire("vector", "fp-query", invalidated=True, runner=lambda: runner.run("vector"))
    )
    await runner.mark("vector").wait()

    # The new revision invalidates skills only.
    skills = asyncio.create_task(
        executor.acquire("skills", "fp-skills-2", invalidated=True, runner=lambda: runner.run("skills"))
    )
    preserved_vector = asyncio.create_task(
        executor.acquire("vector", "fp-query", invalidated=False, runner=lambda: runner.run("vector"))
    )
    await asyncio.sleep(0)
    gate.set()

    assert await vector == "rows"
    assert await preserved_vector == "rows"
    assert await skills == "rows"
    assert runner.cancelled == []
    assert sorted(runner.calls) == ["skills", "vector"]


@pytest.mark.asyncio
async def test_returning_to_an_earlier_fingerprint_is_free():
    executor = BranchExecutor()
    runner = _Runner()

    await executor.acquire("sql", "fp-a", invalidated=True, runner=lambda: runner.run("sql"))
    await executor.acquire("sql", "fp-b", invalidated=True, runner=lambda: runner.run("sql"))
    again = await executor.acquire("sql", "fp-a", invalidated=False, runner=lambda: runner.run("sql"))

    assert again == "rows"
    assert runner.calls == ["sql", "sql"], "returning to an earlier plan re-ran the branch"
    assert executor.stats["branches_reused"] == 1


@pytest.mark.asyncio
async def test_a_failing_branch_is_reported_as_absent_without_losing_the_others():
    executor = BranchExecutor()
    runner = _Runner(fail={"bm25"})

    good = await executor.acquire("vector", "fp-v", invalidated=True, runner=lambda: runner.run("vector"))
    bad = await executor.acquire("bm25", "fp-b", invalidated=True, runner=lambda: runner.run("bm25"))

    assert good == "rows"
    assert bad is None


@pytest.mark.asyncio
async def test_cancel_all_stops_every_in_flight_branch():
    executor = BranchExecutor()
    gate = asyncio.Event()
    runner = _Runner(gate=gate)

    tasks = [
        asyncio.create_task(
            executor.acquire(branch, f"fp-{branch}", invalidated=True, runner=lambda b=branch: runner.run(b))
        )
        for branch in ("vector", "skills")
    ]
    await runner.mark("vector").wait()
    await runner.mark("skills").wait()

    cancelled = executor.cancel_all(reason="session_closed")
    gate.set()
    outcomes = await asyncio.gather(*tasks, return_exceptions=True)

    assert sorted(cancelled) == ["skills", "vector"]
    assert sorted(runner.cancelled) == ["skills", "vector"]
    assert all(isinstance(outcome, asyncio.CancelledError) for outcome in outcomes)


@pytest.mark.asyncio
async def test_a_branch_that_finished_after_being_detached_is_still_harvested():
    """An interruption can detach a branch; its completed work must stay usable."""
    executor = BranchExecutor()
    runner = _Runner()

    stale = asyncio.create_task(
        executor.acquire("vector", "fp-1", invalidated=True, runner=lambda: runner.run("vector"))
    )
    await runner.mark("vector").wait()
    # The revision that owned this branch goes away, but the branch is valid.
    stale.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stale
    await asyncio.sleep(0)

    reused = await executor.acquire("vector", "fp-1", invalidated=False, runner=lambda: runner.run("vector"))

    assert reused == "rows"
    assert runner.calls == ["vector"]
