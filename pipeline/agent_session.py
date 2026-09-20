from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

_sessions: dict[str, "AgentSession"] = {}


@dataclass
class StackEntry:
    query: str
    filters: dict[str, Any]
    results_preview: list[dict]   # enriched candidate dicts (scores + explanation)
    agent_reasoning: str
    iteration_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: float = field(default_factory=time.time)
    spec_summary: dict = field(default_factory=dict)  # key planner fields for diagnosis
    total_scanned: int = 0                            # total candidates considered
    latency_ms: float = 0.0
    timings_ms: dict[str, float] = field(default_factory=dict)
    phase_timings: dict[str, float] = field(default_factory=dict)
    mode: str = "no-llm"
    candidate_ids: list[str] = field(default_factory=list)
    deferred_candidate_ids: list[str] = field(default_factory=list)


@dataclass
class RoleContext:
    soft_preferences: list[str] = field(default_factory=list)
    negative_preferences: list[str] = field(default_factory=list)
    example_good_ids: list[str] = field(default_factory=list)


@dataclass
class AgentSession:
    recruiter_id: str
    session_id: str
    messages: list[Any] = field(default_factory=list)   # PydanticAI message history
    search_stack: list[StackEntry] = field(default_factory=list)
    role_context: RoleContext = field(default_factory=RoleContext)
    candidate_pool: list[dict] = field(default_factory=list)  # Tier 2 pool
    shortlist: dict[str, list[str]] = field(default_factory=lambda: {"accepted": [], "rejected": [], "held": []})
    working_spec: dict[str, Any] = field(default_factory=dict) # role, must_skills, location, exp_range, etc.
    pending_observation: dict[str, str] | None = None
    asked_observation_ids: set[str] = field(default_factory=set)
    created_at: float = field(default_factory=time.time)


def _key(recruiter_id: str, session_id: str) -> str:
    return f"{recruiter_id}:{session_id}"


def get_or_create(recruiter_id: str, session_id: str) -> AgentSession:
    k = _key(recruiter_id, session_id)
    if k not in _sessions:
        _sessions[k] = AgentSession(recruiter_id=recruiter_id, session_id=session_id)
    return _sessions[k]


def push_to_stack(session: AgentSession, entry: StackEntry) -> None:
    session.search_stack.append(entry)


def pop_from_stack(session: AgentSession) -> StackEntry | None:
    """Pop and return the top entry. Returns None if only one entry remains (can't go back)."""
    if len(session.search_stack) <= 1:
        return None
    return session.search_stack.pop()


def clear(recruiter_id: str, session_id: str) -> None:
    _sessions.pop(_key(recruiter_id, session_id), None)
