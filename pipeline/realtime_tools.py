"""Bounded, typed tools exposed to the realtime conversation model.

The manifest is data, not a hardcoded table. Two things depend on that:

* **Classification travels with the tool.** ``effect`` says whether a call can
  change state, and ``blocking`` says whether the model may keep speaking across
  it. A hardcoded list of names cannot answer either question for a tool the
  session registers at runtime, and "is this call allowed to write?" is not a
  question worth guessing at.

* **A session can declare a new capability mid-conversation.** Registering one
  bumps the manifest revision, so a caller can tell that its declarations are
  stale and re-publish them.

State-modifying tools additionally get two guarantees the read-only ones do not
need:

* **Idempotency.** A repeated call with the same key performs one side effect
  and replays the first result, so a retried tool call cannot double-write.
* **Scope supersession.** Calls that touch the same collection share a scope. A
  newer call in a scope cancels an older in-flight one instead of racing it —
  "shortlist the top three", then "actually only the first one" must not leave
  both writes in flight.

The wire declaration stays exactly ``name``/``description``/``parameters``/
``behavior``. The provider validates that shape against its own schema, so the
classification is exposed through the registry rather than smuggled into the
declaration.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from pipeline.realtime_state import CONSTRAINT_SLOTS


class ToolRejected(ValueError):
    pass


#: The audit vocabulary for a state-changing call. ``applied`` and ``replayed``
#: are the two ways an effect is confirmed; ``cancelled`` and ``failed`` are the
#: two ways it is withdrawn. Keeping the set closed lets a caller count
#: confirmed effects without guessing at strings.
EffectOutcome = Literal["applied", "replayed", "cancelled", "failed"]
EFFECT_OUTCOMES: tuple[EffectOutcome, ...] = ("applied", "replayed", "cancelled", "failed")


ToolEffect = Literal["read_only", "state_modifying"]


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
    intent: Literal["refine", "replace"] = "refine"

    @model_validator(mode="after")
    def require_change(self) -> ReviseSearchArgs:
        # `intent` alone is not a change: it describes how to treat the other
        # fields, so a call that sets only intent has nothing to apply.
        if not (self.model_fields_set - {"intent"}):
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


class RequestClarificationArgs(_ToolArgs):
    """Ask the recruiter for a slot the plan genuinely needs.

    Declaring the missing slots rather than guessing them is what keeps an
    ambiguous request from being answered with a confident wrong search.
    """

    question: str = Field(min_length=3, max_length=300)
    slots_needed: list[str] = Field(default_factory=list, max_length=8)
    blocking: bool = True

    @model_validator(mode="after")
    def known_slots_only(self) -> RequestClarificationArgs:
        unknown = [slot for slot in self.slots_needed if slot not in CONSTRAINT_SLOTS]
        if unknown:
            raise ValueError(
                f"Unknown slot names: {', '.join(sorted(unknown))}. "
                f"Known slots: {', '.join(CONSTRAINT_SLOTS)}"
            )
        return self


class AddToShortlistArgs(_ToolArgs):
    """The one tool that changes state, and therefore the one that needs care."""

    candidate_ids: list[str] = Field(min_length=1, max_length=10)
    replace_existing: bool = False
    idempotency_key: str | None = Field(default=None, max_length=120)
    reason: str | None = Field(default=None, max_length=200)


class UseRoleImageArgs(_ToolArgs):
    """Adopt a role the recruiter shared as an image.

    ``ignore`` names requirement categories or phrases to leave out, so
    "use this role, but ignore the degree requirement" is expressible without
    re-describing the whole role.
    """

    image_id: str | None = Field(default=None, max_length=80)
    query: str | None = Field(default=None, min_length=2, max_length=1000)
    ignore: list[str] = Field(default_factory=list, max_length=12)
    top_k: int = Field(default=8, ge=1, le=20)


ToolCallback = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool's declaration plus what the harness is allowed to assume."""

    name: str
    description: str
    args_model: type[_ToolArgs]
    effect: ToolEffect = "read_only"
    blocking: bool = True
    # Calls that touch the same collection share a scope, so a newer call can
    # supersede an older one instead of racing it. None means "no side effects
    # to supersede".
    scope: str | None = None
    # A repeated call with the same key must not perform the side effect twice.
    idempotent: bool = False
    aliases: tuple[str, ...] = ()
    # Reachable by name, but not advertised to the model. Used for compatibility
    # aliases such as `revise_search`.
    hidden: bool = False


def _default_tool_specs() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            name="search_candidates",
            description="Start a new grounded candidate search from the recruiter's request.",
            args_model=SearchCandidatesArgs,
            blocking=False,
        ),
        ToolSpec(
            name="interrupt_search",
            description=(
                "Interrupt stale retrieval and revise the active search plan. Set intent to "
                "'replace' when the recruiter has changed what they are looking for rather "
                "than a criterion within it; hard filters from the previous search are then "
                "dropped instead of silently carried over. Pass null for a field that should "
                "be removed."
            ),
            args_model=InterruptSearchArgs,
            blocking=False,
            aliases=("revise_search",),
        ),
        ToolSpec(
            name="inspect_candidate",
            description="Inspect one candidate using approved profile and document evidence.",
            args_model=InspectCandidateArgs,
        ),
        ToolSpec(
            name="compare_candidates",
            description="Compare a bounded set of candidates using retrieved evidence.",
            args_model=CompareCandidatesArgs,
        ),
        ToolSpec(
            name="format_current_answer",
            description="Reformat the current answer without retrieving new evidence.",
            args_model=FormatCurrentAnswerArgs,
        ),
        ToolSpec(
            name="cancel_current_action",
            description="Cancel currently cancellable read-only work at the user's request.",
            args_model=CancelCurrentActionArgs,
        ),
        ToolSpec(
            name="list_skills",
            description=(
                "Resolve recruiter wording to the exact canonical skills stored in the "
                "candidate database before using hard skill filters."
            ),
            args_model=ListSkillsArgs,
        ),
        ToolSpec(
            name="request_clarification",
            description=(
                "Ask the recruiter for a criterion the search genuinely needs, instead of "
                "guessing it. Name the missing slots so the question is answerable."
            ),
            args_model=RequestClarificationArgs,
        ),
        ToolSpec(
            name="use_role_image",
            description=(
                "Search for the role the recruiter shared as an image. Requirements the "
                "index cannot enforce are returned as unenforceable and must never be "
                "described as active filters. Pass ignore with a requirement category or "
                "phrase when the recruiter says to leave one out."
            ),
            args_model=UseRoleImageArgs,
            blocking=False,
        ),
        ToolSpec(
            name="add_to_shortlist",
            description=(
                "Add candidates to the recruiter's shortlist. This is the only tool that "
                "changes stored state. Pass replace_existing when the recruiter corrects an "
                "earlier selection, and reuse the same idempotency_key when retrying the same "
                "call so it cannot be applied twice."
            ),
            args_model=AddToShortlistArgs,
            effect="state_modifying",
            scope="shortlist",
            idempotent=True,
        ),
    )


DEFAULT_TOOL_SPECS: tuple[ToolSpec, ...] = _default_tool_specs()


class ToolRegistry:
    """A mutable, inspectable tool manifest.

    Tools are registered rather than hardcoded so a session can declare a new
    capability at runtime, and so every call site can ask what that capability
    is permitted to do.
    """

    def __init__(self, specs: Iterable[ToolSpec] = ()) -> None:
        self._specs: dict[str, ToolSpec] = {}
        self._aliases: dict[str, str] = {}
        self.revision = 0
        for spec in specs:
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        """Add or replace a tool, and bump the manifest revision."""
        self._specs[spec.name] = spec
        for alias in spec.aliases:
            self._aliases[alias] = spec.name
        self.revision += 1

    def unregister(self, name: str) -> bool:
        spec = self._specs.pop(name, None)
        if spec is None:
            return False
        for alias in spec.aliases:
            if self._aliases.get(alias) == name:
                self._aliases.pop(alias, None)
        self.revision += 1
        return True

    def resolve(self, name: str) -> str | None:
        """Map a requested name (possibly an alias) to its canonical name."""
        if name in self._specs:
            return name
        return self._aliases.get(name)

    def get(self, name: str) -> ToolSpec | None:
        canonical = self.resolve(name)
        return self._specs.get(canonical) if canonical else None

    def names(self) -> list[str]:
        return sorted(self._specs)

    def declarations(self) -> list[dict[str, Any]]:
        """The provider-facing manifest: name, description, schema, behavior."""
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "parameters": _gemini_schema(spec.args_model.model_json_schema()),
                "behavior": "BLOCKING" if spec.blocking else "NON_BLOCKING",
            }
            for spec in self._specs.values()
            if not spec.hidden
        ]

    def non_blocking(self) -> frozenset[str]:
        """Tools the model may keep speaking across instead of waiting."""
        return frozenset(spec.name for spec in self._specs.values() if not spec.blocking)

    def state_modifying(self) -> frozenset[str]:
        return frozenset(
            spec.name for spec in self._specs.values() if spec.effect == "state_modifying"
        )

    def scope_of(self, name: str) -> str | None:
        spec = self.get(name)
        return spec.scope if spec else None

    def clone(self) -> ToolRegistry:
        """A private copy, so a session's registrations never leak to another."""
        clone = ToolRegistry()
        clone._specs = dict(self._specs)
        clone._aliases = dict(self._aliases)
        clone.revision = self.revision
        return clone


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


DEFAULT_REGISTRY = ToolRegistry(DEFAULT_TOOL_SPECS)


class RealtimeToolDispatcher:
    """Validates, classifies and runs tool calls for one session.

    The registry is cloned per dispatcher by default, so a session that
    registers a tool at runtime does not change what any other session sees.
    """

    def __init__(
        self,
        callbacks: dict[str, ToolCallback],
        *,
        registry: ToolRegistry | None = None,
        state_hook: Callable[..., Any] | None = None,
    ) -> None:
        self.callbacks = dict(callbacks)
        self.registry = registry.clone() if registry is not None else DEFAULT_REGISTRY.clone()
        self.state_hook = state_hook
        # idempotency key -> the result the first execution produced.
        self._idempotency: dict[str, dict[str, Any]] = {}
        # scope -> the in-flight call that owns it.
        self._in_flight_by_scope: dict[str, asyncio.Task[dict[str, Any]]] = {}
        # Every state-changing call this session attempted, in order.
        self.effect_log: list[dict[str, Any]] = []

    # ── Manifest ─────────────────────────────────────────────────────────────

    def register_tool(self, spec: ToolSpec, callback: ToolCallback | None = None) -> int:
        """Declare a tool the session did not start with. Returns the revision."""
        self.registry.register(spec)
        if callback is not None:
            self.callbacks[spec.name] = callback
        return self.registry.revision

    def unregister_tool(self, name: str) -> bool:
        removed = self.registry.unregister(name)
        if removed:
            self.callbacks.pop(name, None)
        return removed

    @classmethod
    def tool_declarations(cls, registry: ToolRegistry | None = None) -> list[dict[str, Any]]:
        return (registry or DEFAULT_REGISTRY).declarations()

    @classmethod
    def non_blocking_tools(cls, registry: ToolRegistry | None = None) -> frozenset[str]:
        return (registry or DEFAULT_REGISTRY).non_blocking()

    # ── Dispatch ─────────────────────────────────────────────────────────────

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        arguments = dict(arguments or {})
        await self._notify("requested", name, arguments, None, None)

        canonical = self.registry.resolve(name)
        spec = self.registry.get(canonical) if canonical else None
        if spec is None:
            error = await self._reject(f"Unknown realtime tool: {name}", name, arguments)
            raise error

        # Arguments are validated before availability is checked: a malformed
        # request is malformed regardless of whether this session can serve it,
        # and saying so is more useful than reporting a missing tool.
        try:
            validated = spec.args_model.model_validate(arguments)
        except ValidationError as exc:
            error = await self._reject(f"Invalid arguments for {name}: {exc}", name, arguments)
            raise error from exc
        payload = validated.model_dump(exclude_unset=True, mode="json")

        callback = self._callback_for(spec, name)
        if callback is None:
            error = await self._reject(
                f"Realtime tool is unavailable in this session: {name}", name, arguments
            )
            raise error

        key = self._idempotency_key(spec, payload)
        if key is not None and key in self._idempotency:
            replayed = {**self._idempotency[key], "idempotent_replay": True}
            self._record_effect(spec, name, payload, outcome="replayed", key=key)
            return await self._finalize("completed", name, arguments, replayed, None)

        try:
            result = await self._execute(spec, name, payload, callback)
        except asyncio.CancelledError:
            # A newer call in the same scope replaced this one. The cancellation
            # is reported rather than swallowed, so the model never confirms an
            # effect that was withdrawn. Re-raised so the bridge reports the
            # call as cancelled rather than completed.
            self._record_effect(spec, name, payload, outcome="cancelled", key=key)
            await self._notify("failed", name, arguments, None, "superseded_by_newer_call")
            raise
        except Exception as exc:
            self._record_effect(spec, name, payload, outcome="failed", key=key)
            await self._notify("failed", name, arguments, None, exc)
            raise

        # Recorded after the effect landed, not when it was requested. A write
        # that is only logged on failure cannot be audited: the log would show
        # the retry but not the effect it retried, so "exactly one side effect"
        # would be unprovable from the very structure meant to prove it.
        self._record_effect(spec, name, payload, outcome="applied", key=key)
        if key is not None:
            self._idempotency[key] = dict(result)
        return await self._finalize("completed", name, arguments, result, None)

    async def _execute(
        self,
        spec: ToolSpec,
        name: str,
        payload: dict[str, Any],
        callback: ToolCallback,
    ) -> dict[str, Any]:
        if spec.scope is None:
            return await callback(payload)

        superseded = self._supersede_scope(spec.scope, by=name)
        task: asyncio.Task[dict[str, Any]] = asyncio.create_task(callback(payload))
        self._in_flight_by_scope[spec.scope] = task
        try:
            result = await task
        finally:
            if self._in_flight_by_scope.get(spec.scope) is task:
                self._in_flight_by_scope.pop(spec.scope, None)
        if superseded:
            # The model must be told an earlier call was replaced, otherwise it
            # will confirm both selections.
            result = {**result, "superseded_calls": superseded}
        return result

    def _callback_for(self, spec: ToolSpec, requested: str) -> ToolCallback | None:
        for candidate in (spec.name, requested, *spec.aliases):
            callback = self.callbacks.get(candidate)
            if callback is not None:
                return callback
        return None

    def _idempotency_key(self, spec: ToolSpec, payload: dict[str, Any]) -> str | None:
        if not spec.idempotent:
            return None
        raw = payload.get("idempotency_key")
        if raw:
            return f"{spec.name}:{raw}"
        # Without an explicit key the call is still deduplicated on its own
        # content, which is what makes a retried identical call safe.
        return f"{spec.name}:auto:{_stable_key(payload)}"

    def _supersede_scope(self, scope: str, *, by: str) -> list[str]:
        """Cancel an older in-flight call in the same scope. Never awaits it."""
        task = self._in_flight_by_scope.pop(scope, None)
        if task is None or task.done():
            return []
        task.cancel()
        return [scope]

    def _record_effect(
        self,
        spec: ToolSpec,
        name: str,
        payload: dict[str, Any],
        *,
        outcome: EffectOutcome,
        key: str | None,
    ) -> None:
        if spec.effect != "state_modifying":
            return
        self.effect_log.append({
            "tool": name,
            "scope": spec.scope,
            "outcome": outcome,
            "idempotency_key": key,
            "candidate_ids": list(payload.get("candidate_ids") or []),
            "replace_existing": bool(payload.get("replace_existing")),
        })
        del self.effect_log[:-100]

    async def _reject(self, message: str, name: str, arguments: dict[str, Any]) -> ToolRejected:
        error = ToolRejected(message)
        await self._notify("failed", name, arguments, None, error)
        return error

    async def _finalize(
        self,
        phase: str,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any],
        error: Any,
    ) -> dict[str, Any]:
        state = await self._notify(phase, name, arguments, result, error)
        if isinstance(state, dict) and isinstance(result, dict):
            return {**result, "state": state}
        return result

    async def _notify(
        self,
        phase: str,
        name: str,
        arguments: dict[str, Any],
        result: dict[str, Any] | None,
        error: Any,
    ) -> Any:
        if self.state_hook is None:
            return None
        outcome = self.state_hook(
            phase=phase, name=name, arguments=arguments, result=result, error=error
        )
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        return outcome

    # ── Cancellation ─────────────────────────────────────────────────────────

    async def cancel_active(self, *, reason: str = "barge_in") -> dict[str, Any]:
        """Hard-cancel in-flight work. Reserved for explicit user cancellation."""
        superseded = self._cancel_scopes()
        callback = self.callbacks.get("cancel_current_action")
        if callback is None:
            return {
                "cancelled": bool(superseded),
                "reason": "cancel_tool_unavailable",
                "superseded_effects": superseded,
            }
        result = await callback({"reason": reason})
        return {**result, "superseded_effects": superseded}

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

    def _cancel_scopes(self) -> list[str]:
        cancelled: list[str] = []
        for scope, task in list(self._in_flight_by_scope.items()):
            self._in_flight_by_scope.pop(scope, None)
            if task.done():
                continue
            task.cancel()
            cancelled.append(scope)
        return cancelled


def _stable_key(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
