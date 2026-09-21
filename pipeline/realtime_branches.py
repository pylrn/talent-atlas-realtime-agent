"""Per-branch execution for an interruptible session.

Cancelling every in-flight query on each revision is what makes an
interruptible agent feel like it restarts: the work was already paid for and
the next revision often still needs most of it. Keeping everything is no better
— a stale branch finishes and is then discarded anyway.

The right answer is selective, and it follows from the branch fingerprints
already attached to each revision. A branch whose fingerprint is unchanged
between two revisions has identical inputs, so its in-flight work is still
valid and must be left alone. A branch whose fingerprint changed is invalid and
must be cancelled promptly so the new revision is not queued behind it.

This executor owns the per-branch tasks, so cancellation happens at branch
granularity rather than at search granularity. Results are handed back to the
canonical engine, which keeps fusion, ranking and enrichment in one place.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

BranchRunner = Callable[[], Awaitable[Any]]


class BranchExecutor:
    """Runs retrieval branches individually and cancels only invalidated ones."""

    def __init__(self) -> None:
        # branch fingerprint -> value. Keyed by fingerprint rather than branch,
        # so returning to an earlier revision is free.
        self._cache: dict[str, Any] = {}
        self._active: dict[str, asyncio.Task[Any]] = {}
        self._active_fingerprints: dict[str, str] = {}
        self.stats: dict[str, int] = {
            "branches_run": 0,
            "branches_reused": 0,
            "branches_preserved": 0,
            "branches_cancelled": 0,
        }
        # What happened to each branch most recently: reused, preserved,
        # cancelled or started. This is the audit trail for an interruption.
        self.decisions: dict[str, str] = {}

    async def acquire(
        self,
        branch: str,
        fingerprint: str,
        *,
        invalidated: bool,
        runner: BranchRunner,
    ) -> Any:
        """Resolve one branch, reusing, preserving or cancelling as appropriate.

        ``invalidated`` means this revision changed the branch's inputs. A
        branch that is not invalidated and whose work is already in flight is
        preserved and awaited rather than restarted, because the query has
        already been paid for and is still exactly what this revision needs.
        """
        self._harvest()
        self.decisions.pop(branch, None)
        active = self._active.get(branch)

        if not invalidated and fingerprint in self._cache:
            self.decisions[branch] = "reused"
            self.stats["branches_reused"] += 1
            return self._cache[fingerprint]

        if active is not None and not active.done() and self._active_fingerprints.get(branch) == fingerprint:
            self.decisions[branch] = "preserved"
            self.stats["branches_preserved"] += 1
            try:
                value = await active
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one branch failing must not fail the search
                # A failed branch is reported as absent; the engine fuses what
                # is left rather than failing the whole search.
                return None
            self._cache[fingerprint] = value
            self._forget(branch, active)
            return value

        if active is not None and not active.done():
            # Invalid for this revision, so it must stop rather than hold a
            # database slot the new revision is waiting on.
            active.cancel()
            self.decisions[branch] = "cancelled"
            self.stats["branches_cancelled"] += 1

        task = asyncio.create_task(runner())
        self._active[branch] = task
        self._active_fingerprints[branch] = fingerprint
        self.decisions.setdefault(branch, "started")
        self.stats["branches_run"] += 1
        try:
            value = await task
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - reported as an absent branch, not a crash
            self._forget(branch, task)
            return None
        self._cache[fingerprint] = value
        self._forget(branch, task)
        return value

    def cancel_all(self, *, reason: str = "cancelled") -> list[str]:
        """Cancel every in-flight branch. Never awaits them."""
        cancelled: list[str] = []
        for branch, task in list(self._active.items()):
            self._forget(branch, task)
            if task.done():
                continue
            task.cancel()
            cancelled.append(branch)
        self.stats["branches_cancelled"] += len(cancelled)
        for branch in cancelled:
            self.decisions[branch] = "cancelled"
        if cancelled:
            self.decisions["reason"] = reason
        return cancelled

    async def settle(self) -> None:
        """Await in-flight branches. Used by deterministic tests and shutdown."""
        tasks = [task for task in self._active.values() if not task.done()]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._harvest()

    def _forget(self, branch: str, task: asyncio.Task[Any]) -> None:
        if self._active.get(branch) is task:
            self._active.pop(branch, None)
            self._active_fingerprints.pop(branch, None)

    def _harvest(self) -> None:
        """Cache branches that finished while nobody was awaiting them.

        A preserved branch is awaited by the revision that preserved it, but a
        branch detached by an interruption finishes on its own. Harvesting keeps
        that work usable instead of letting it be discarded.
        """
        for branch, task in list(self._active.items()):
            if not task.done() or task.cancelled():
                continue
            fingerprint = self._active_fingerprints.get(branch)
            self._forget(branch, task)
            error = task.exception()
            if error is not None or fingerprint is None:
                continue
            self._cache[fingerprint] = task.result()
