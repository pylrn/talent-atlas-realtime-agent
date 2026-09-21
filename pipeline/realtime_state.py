"""Explicit session state, published on every transition.

An agent that only emits events leaves its listener to reconstruct what it
currently believes. That reconstruction is exactly where interruptions go
wrong: after a barge-in nobody can say whether the city filter was dropped,
kept, or never applied in the first place. Every transition therefore publishes
one self-contained snapshot instead.

The snapshot answers five questions in a single object:

``intent``
    what the model just asked for, derived from the tool it called.
``slots``
    the criteria the session currently holds — *including* the ones it does not
    hold. An explicit ``None`` is a fact ("no city constraint"), and it is the
    fact a clarification decision depends on.
``revision``
    which immutable plan those slots belong to, and the fingerprints that make
    its reuse auditable.
``changed_fields``
    what this transition changed relative to the previous plan.
``status``
    where the turn is: planning, retrieving, ready, clarifying, answered,
    cancelled or failed.

``phase`` additionally says whether the snapshot describes a request or its
outcome, so one tool call produces a matched pair instead of a single ambiguous
event.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from pipeline.realtime_plan import BRANCHES, SearchPlanRevision

# Where the turn is. `clarifying` and `cancelled` are first-class states rather
# than error paths: an agent that cannot say "I am blocked on a missing slot"
# has to guess, and guessing is what a clarification exists to avoid.
SessionStatus = Literal[
    "idle",
    "planning",
    "retrieving",
    "ready",
    "clarifying",
    "answered",
    "cancelled",
    "failed",
]

# What the model asked for. Derived from the tool name so a snapshot is
# self-describing without joining it to a separate tool event.
TurnIntent = Literal[
    "search",
    "refine",
    "replace",
    "interrupt",
    "inspect",
    "compare",
    "reformat",
    "list_skills",
    "cancel",
    "clarify",
    "answer",
]

# Which lifecycle moment the snapshot describes.
SnapshotPhase = Literal[
    "requested",
    "completed",
    "failed",
    "interrupted",
    "clarification",
    "final",
]

INTENT_BY_TOOL: dict[str, TurnIntent] = {
    "search_candidates": "search",
    "revise_search": "refine",
    "interrupt_search": "refine",
    "inspect_candidate": "inspect",
    "compare_candidates": "compare",
    "format_current_answer": "reformat",
    "list_skills": "list_skills",
    "cancel_current_action": "cancel",
    "request_clarification": "clarify",
}

# Slot keys that describe *eligibility* or *ranking*. `status` and
# `keyword_policy` are excluded from the unset report because they always carry
# a default and are never "missing" in a way the recruiter could fill.
CONSTRAINT_SLOTS = (
    "city",
    "country",
    "min_years_exp",
    "max_years_exp",
    "must_skills",
    "should_skills",
    "should_themes",
    "should_roles",
    "should_locations",
    "excluded_skills",
)


class RevisionRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision_id: str | None = None
    parent_revision_id: str | None = None
    plan_fingerprint: str | None = None
    filter_fingerprint: str | None = None

    @classmethod
    def of(cls, revision: SearchPlanRevision | None) -> RevisionRef:
        if revision is None:
            return cls()
        return cls(
            revision_id=revision.revision_id,
            parent_revision_id=revision.parent_revision_id,
            plan_fingerprint=revision.plan_fingerprint,
            filter_fingerprint=revision.filter_fingerprint,
        )


class BranchActivity(BaseModel):
    """What happened to each retrieval branch during this transition.

    ``preserved`` is the interesting one: it means the branch was already
    running, its inputs had not changed, and the session deliberately let it
    finish rather than restarting it.
    """

    model_config = ConfigDict(extra="forbid")

    reused: list[str] = Field(default_factory=list)
    executed: list[str] = Field(default_factory=list)
    preserved: list[str] = Field(default_factory=list)
    cancelled: list[str] = Field(default_factory=list)

    @classmethod
    def from_decisions(cls, decisions: dict[str, str] | None) -> BranchActivity:
        """Translate a BranchExecutor decision map into a snapshot view."""
        buckets: dict[str, list[str]] = {
            "reused": [],
            "executed": [],
            "preserved": [],
            "cancelled": [],
        }
        for branch in BRANCHES:
            decision = (decisions or {}).get(branch)
            if decision == "reused":
                buckets["reused"].append(branch)
            elif decision == "preserved":
                buckets["preserved"].append(branch)
            elif decision == "cancelled":
                buckets["cancelled"].append(branch)
            elif decision == "started":
                buckets["executed"].append(branch)
        return cls(**buckets)


class StateSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_version: int = 1
    session_id: str
    sequence: int = 0
    phase: SnapshotPhase
    intent: TurnIntent
    status: SessionStatus
    # False for a snapshot produced by speculative pre-retrieval. The listener
    # must be able to tell a provisional state from the authoritative one, or a
    # guess would be indistinguishable from a decision.
    authoritative: bool = True
    tool: str | None = None
    revision: RevisionRef = Field(default_factory=RevisionRef)
    slots: dict[str, Any] = Field(default_factory=dict)
    unset_slots: list[str] = Field(default_factory=list)
    changed_fields: list[str] = Field(default_factory=list)
    branches: BranchActivity = Field(default_factory=BranchActivity)
    # Where the current state came from — the transcript, a role image, or both.
    # Without this, "the role came from an image the recruiter showed" is
    # information the harness holds and the client cannot see.
    evidence_sources: list[str] = Field(default_factory=list)
    candidate_count: int | None = None
    note: str | None = None


def slots_from_revision(revision: SearchPlanRevision | None) -> dict[str, Any]:
    """Flatten a revision into the recruiter-facing slot state.

    Every constraint key is present, including the empty ones. Dropping them
    would make "the city filter was never set" indistinguishable from "the city
    filter is not part of this payload", and only one of those is a reason to
    ask the recruiter a question.
    """
    if revision is None:
        return {"query": ""}
    return {
        "query": revision.query,
        "city": revision.city,
        "country": revision.country,
        "min_years_exp": revision.min_years_exp,
        "max_years_exp": revision.max_years_exp,
        "status": revision.status,
        "must_skills": list(revision.must_skills),
        "should_skills": list(revision.should_skills),
        "should_themes": list(revision.should_themes),
        "should_roles": list(revision.should_roles),
        "should_locations": list(revision.should_locations),
        "excluded_skills": list(revision.excluded_skills),
        "keyword_policy": revision.keyword_policy,
    }


def unset_slots(slots: dict[str, Any]) -> list[str]:
    """Constraint slots that currently carry no value."""
    missing: list[str] = []
    for key in CONSTRAINT_SLOTS:
        value = slots.get(key)
        if value is None or value == "" or value == []:
            missing.append(key)
    return missing


def build_snapshot(
    *,
    session_id: str,
    sequence: int,
    phase: SnapshotPhase,
    intent: TurnIntent,
    status: SessionStatus,
    tool: str | None = None,
    revision: SearchPlanRevision | None = None,
    changed_fields: list[str] | tuple[str, ...] = (),
    branches: BranchActivity | None = None,
    evidence_sources: list[str] | tuple[str, ...] = (),
    candidate_count: int | None = None,
    note: str | None = None,
    authoritative: bool = True,
) -> StateSnapshot:
    slots = slots_from_revision(revision)
    return StateSnapshot(
        session_id=session_id,
        sequence=sequence,
        phase=phase,
        intent=intent,
        status=status,
        authoritative=authoritative,
        tool=tool,
        revision=RevisionRef.of(revision),
        slots=slots,
        unset_slots=unset_slots(slots),
        changed_fields=sorted(str(field) for field in changed_fields),
        branches=branches or BranchActivity(),
        evidence_sources=[str(source) for source in evidence_sources],
        candidate_count=candidate_count,
        note=note,
    )


class SnapshotJournal:
    """Bounded, append-only history of published snapshots.

    Kept in memory rather than only on the event stream because a deterministic
    replay needs the state a turn *started* from, and an event consumer that
    joined late would have missed it.
    """

    def __init__(self, *, limit: int = 200) -> None:
        self.limit = max(1, int(limit))
        self.snapshots: list[StateSnapshot] = []
        self._sequence = 0

    @property
    def latest(self) -> StateSnapshot | None:
        return self.snapshots[-1] if self.snapshots else None

    def next_sequence(self) -> int:
        self._sequence += 1
        return self._sequence

    def append(self, snapshot: StateSnapshot) -> StateSnapshot:
        self.snapshots.append(snapshot)
        if len(self.snapshots) > self.limit:
            del self.snapshots[: len(self.snapshots) - self.limit]
        return snapshot

    def to_list(self) -> list[dict[str, Any]]:
        return [snapshot.model_dump(mode="json") for snapshot in self.snapshots]

    def reset(self) -> None:
        self.snapshots.clear()
        self._sequence = 0
