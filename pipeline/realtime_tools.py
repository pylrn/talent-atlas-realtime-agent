"""Bounded, typed tools exposed to the realtime conversation model."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ToolRejected(ValueError):
    pass


class _ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SearchCandidatesArgs(_ToolArgs):
    query: str = Field(min_length=2, max_length=1000)
    city: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=120)
    min_years_exp: int | None = Field(default=None, ge=0, le=80)
    max_years_exp: int | None = Field(default=None, ge=0, le=80)
    must_skills: list[str] = Field(default_factory=list, max_length=20)
    should_skills: list[str] = Field(default_factory=list, max_length=20)
    should_themes: list[str] = Field(default_factory=list, max_length=20)
    should_roles: list[str] = Field(default_factory=list, max_length=12)
    should_locations: list[str] = Field(default_factory=list, max_length=12)
    excluded_skills: list[str] = Field(default_factory=list, max_length=20)
    keyword_policy: Literal["auto", "skip", "force"] = "auto"
    top_k: int = Field(default=8, ge=1, le=20)


class ReviseSearchArgs(_ToolArgs):
    query: str | None = Field(default=None, min_length=2, max_length=1000)
    city: str | None = Field(default=None, max_length=120)
    country: str | None = Field(default=None, max_length=120)
    min_years_exp: int | None = Field(default=None, ge=0, le=80)
    max_years_exp: int | None = Field(default=None, ge=0, le=80)
    must_skills: list[str] | None = Field(default=None, max_length=20)
    should_skills: list[str] | None = Field(default=None, max_length=20)
    should_themes: list[str] | None = Field(default=None, max_length=20)
    should_roles: list[str] | None = Field(default=None, max_length=12)
    should_locations: list[str] | None = Field(default=None, max_length=12)
    excluded_skills: list[str] | None = Field(default=None, max_length=20)
    keyword_policy: Literal["auto", "skip", "force"] | None = None
    top_k: int | None = Field(default=None, ge=1, le=20)

    @model_validator(mode="after")
    def require_change(self) -> "ReviseSearchArgs":
        if not self.model_fields_set:
            raise ValueError("At least one search field must change")
        return self


class InterruptSearchArgs(ReviseSearchArgs):
    """Interrupt stale retrieval and patch only the recruiter's changed fields."""


class InspectCandidateArgs(_ToolArgs):
    candidate_id: str = Field(min_length=8, max_length=80)
    include_documents: bool = True


class CompareCandidatesArgs(_ToolArgs):
    candidate_ids: list[str] = Field(min_length=2, max_length=6)
    focus: str | None = Field(default=None, max_length=300)


class FormatCurrentAnswerArgs(_ToolArgs):
    format: Literal["brief", "bullets", "table", "detailed"]


class CancelCurrentActionArgs(_ToolArgs):
    reason: str = Field(default="user_requested", max_length=200)


class ListSkillsArgs(_ToolArgs):
    queries: list[str] = Field(min_length=1, max_length=8)
    limit_per_query: int = Field(default=8, ge=1, le=20)


ToolCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


_TOOL_MODELS: dict[str, type[_ToolArgs]] = {
    "search_candidates": SearchCandidatesArgs,
    "interrupt_search": InterruptSearchArgs,
    "revise_search": ReviseSearchArgs,
    "inspect_candidate": InspectCandidateArgs,
    "compare_candidates": CompareCandidatesArgs,
    "format_current_answer": FormatCurrentAnswerArgs,
    "cancel_current_action": CancelCurrentActionArgs,
    "list_skills": ListSkillsArgs,
}

_TOOL_DESCRIPTIONS = {
    "search_candidates": "Start a new grounded candidate search from the recruiter's request.",
    "interrupt_search": "Interrupt stale retrieval and revise only explicit fields of the active search plan. Pass null for a field that should be removed.",
    "revise_search": "Compatibility alias for interrupt_search.",
    "inspect_candidate": "Inspect one candidate using approved profile and document evidence.",
    "compare_candidates": "Compare a bounded set of candidates using retrieved evidence.",
    "format_current_answer": "Reformat the current answer without retrieving new evidence.",
    "cancel_current_action": "Cancel currently cancellable read-only work at the user's request.",
    "list_skills": "Resolve recruiter wording to the exact canonical skills stored in the candidate database before using hard skill filters.",
}

# Tools that must not block the conversation.
#
# Retrieval against the corpus is the only operation slow enough that waiting on
# it produces dead air, and dead air is what makes an interruptible voice agent
# feel broken. These tools therefore run in the background: the model may speak
# one grounded sentence about the criteria it just sent, and the evidence is
# folded in whenever it arrives.
#
# Everything else stays blocking on purpose. list_skills gates plan
# construction, so the model genuinely needs its answer before continuing;
# inspect_candidate, compare_candidates and format_current_answer are bounded
# lookups whose latency is shorter than a filler sentence would be.
_NON_BLOCKING_TOOLS = frozenset({"search_candidates", "interrupt_search"})


def _gemini_schema(value: Any) -> Any:
    """Remove JSON Schema keywords unsupported by Gemini Live tools."""
    if isinstance(value, dict):
        return {
            key: _gemini_schema(item)
            for key, item in value.items()
            if key not in {"additionalProperties", "$schema"}
        }
    if isinstance(value, list):
        return [_gemini_schema(item) for item in value]
    return value


class RealtimeToolDispatcher:
    def __init__(self, callbacks: dict[str, ToolCallback]) -> None:
        self.callbacks = dict(callbacks)

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        canonical_name = "revise_search" if name == "interrupt_search" else name
        model = _TOOL_MODELS.get(canonical_name)
        if model is None:
            raise ToolRejected(f"Unknown realtime tool: {name}")
        try:
            validated = model.model_validate(arguments)
        except ValidationError as exc:
            raise ToolRejected(f"Invalid arguments for {name}: {exc}") from exc
        callback = self.callbacks.get(canonical_name)
        if callback is None:
            raise ToolRejected(f"Realtime tool is unavailable in this session: {name}")
        return await callback(validated.model_dump(exclude_unset=True, mode="json"))

    async def cancel_active(self, *, reason: str = "barge_in") -> dict[str, Any]:
        """Hard-cancel in-flight work. Reserved for explicit user cancellation."""
        callback = self.callbacks.get("cancel_current_action")
        if callback is None:
            return {"cancelled": False, "reason": "cancel_tool_unavailable"}
        return await callback({"reason": reason})

    async def note_barge_in(self, *, reason: str = "barge_in") -> dict[str, Any]:
        """Record an interruption without discarding in-flight retrieval.

        A barge-in is a conversational event. Treating it as a cancellation is
        what makes an interruptible agent restart from scratch, so the default
        behaviour here is to preserve the running search and let the next
        revision decide what actually changed.
        """
        callback = self.callbacks.get("note_barge_in")
        if callback is None:
            return {"barge_in_acknowledged": True, "reason": reason, "cancelled": False}
        return await callback({"reason": reason})

    @staticmethod
    def non_blocking_tools() -> frozenset[str]:
        """Tools the model may keep speaking across instead of waiting."""
        return _NON_BLOCKING_TOOLS

    @staticmethod
    def tool_declarations() -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "description": _TOOL_DESCRIPTIONS[name],
                "parameters": _gemini_schema(model.model_json_schema()),
                "behavior": (
                    "NON_BLOCKING" if name in _NON_BLOCKING_TOOLS else "BLOCKING"
                ),
            }
            for name, model in _TOOL_MODELS.items()
            if name != "revise_search"
        ]
