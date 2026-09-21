"""Session goal tracking above the search plan.

Theme 5 asks the agent to cater to different goal changes without losing
relevant session context. A search revision is the wrong level for that: it
describes *how* to search, not *what* the recruiter is trying to find. A goal is
the level above it.

The distinction that matters is between a refinement and a replacement.

* A refinement changes criteria inside one goal — "actually make it Bengaluru".
  The active plan is patched, so the parts the recruiter did not mention are
  deliberately kept.

* A replacement changes the goal itself — "now find me product designers". If
  that were patched the same way, every hard filter from the previous search
  would silently persist and the agent would answer a question nobody asked. So
  a replacement starts a clean plan.

What must not be lost on a replacement is the session context: which candidates
have already been surfaced. That is carried forward explicitly, so the agent can
say that some of these candidates also matched the earlier search instead of
presenting a repeat as new.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from pipeline.realtime_plan import SearchPlanRevision

# Hard filters that a replacement is allowed to drop. Preferences
# (should_skills and friends) are not listed: they shape ranking rather than
# eligibility, so losing them narrows the search instead of corrupting it.
DROPPABLE_CONSTRAINTS = (
    "city",
    "country",
    "min_years_exp",
    "max_years_exp",
    "must_skills",
    "excluded_skills",
)


class Goal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal_id: str = Field(default_factory=lambda: f"goal_{uuid.uuid4().hex}")
    statement: str = ""
    revision_id: str | None = None
    candidate_ids: list[str] = Field(default_factory=list)
    superseded: bool = False


class GoalLedger:
    """Append-only goal history for one session.

    Superseded goals are kept rather than dropped: they are what makes it
    possible to connect a new goal to what the session already showed.
    """

    def __init__(self) -> None:
        self.goals: list[Goal] = []

    @property
    def active(self) -> Goal | None:
        if not self.goals or self.goals[-1].superseded:
            return None
        return self.goals[-1]

    @property
    def superseded(self) -> list[Goal]:
        return [goal for goal in self.goals if goal.superseded]

    def start(self, *, statement: str, revision_id: str) -> tuple[Goal, Goal | None]:
        """Open a new goal, closing the active one. Returns (goal, replaced)."""
        replaced = self.active
        if replaced is not None:
            replaced.superseded = True
        goal = Goal(statement=" ".join(str(statement or "").split()), revision_id=revision_id)
        self.goals.append(goal)
        return goal, replaced

    def attach_revision(self, revision_id: str) -> Goal:
        """Point the active goal at the revision that now serves it."""
        if self.active is None:
            return self.start(statement="", revision_id=revision_id)[0]
        self.active.revision_id = revision_id
        return self.active

    def record_candidates(self, candidate_ids: list[str]) -> None:
        """Remember which candidates the active goal has shown."""
        if self.active is None:
            return
        known = set(self.active.candidate_ids)
        for candidate_id in candidate_ids:
            if candidate_id not in known:
                self.active.candidate_ids.append(candidate_id)
                known.add(candidate_id)

    def surfaced_candidate_ids(self) -> list[str]:
        """Every candidate this session has shown, in first-seen order."""
        seen: dict[str, None] = {}
        for goal in self.goals:
            for candidate_id in goal.candidate_ids:
                seen.setdefault(candidate_id, None)
        return list(seen)

    def context_for(self, candidate_ids: list[str]) -> dict[str, Any]:
        """Describe how a result relates to the rest of the session.

        Returned with every search so the model can connect a new goal to what
        it already showed. Computed before the candidates are recorded, so
        ``overlap_with_previous`` means "seen under an earlier goal", not
        "seen a moment ago in this one".
        """
        active = self.active
        earlier_ids = [
            candidate_id
            for goal in self.goals
            if goal is not active
            for candidate_id in goal.candidate_ids
        ]
        earlier = set(earlier_ids)
        overlap = [candidate_id for candidate_id in candidate_ids if candidate_id in earlier]
        replaced = self.goals[-2].statement if len(self.goals) > 1 else None
        return {
            "goal_id": active.goal_id if active else None,
            "goal_statement": active.statement if active else None,
            "replaces_goal": replaced,
            "previously_surfaced": earlier_ids,
            "overlap_with_previous": overlap,
            "note": _note(len(candidate_ids), overlap, replaced),
        }


def dropped_constraints(
    previous: SearchPlanRevision | None,
    revision: SearchPlanRevision,
) -> dict[str, Any]:
    """Hard filters the previous plan had and this one does not.

    A replacement is allowed to drop constraints, but it must not do so
    silently: this is what the trace reports so the change is visible.
    """
    if previous is None:
        return {}
    dropped: dict[str, Any] = {}
    for field in DROPPABLE_CONSTRAINTS:
        before = getattr(previous, field, None)
        after = getattr(revision, field, None)
        if before and not after:
            dropped[field] = before
    return dropped


def _note(total: int, overlap: list[str], replaced: str | None) -> str:
    if not replaced:
        return "This is the first goal in the session."
    if not overlap:
        return f"None of these {total} candidates appeared in an earlier search."
    return (
        f"{len(overlap)} of these {total} candidates also appeared in the earlier "
        f'search for "{replaced}".'
    )
