"""Stream a recruiter-agent run as Server-Sent Events.

Uses ``agent.iter()`` to drive the agent graph node-by-node so we can surface
the full reasoning trace to the UI in real time:

* thinking deltas (when the model emits a reasoning channel),
* each tool call with its **name and arguments**,
* each tool **result** (summary + parsed body),
* the streamed final answer text,
* and rich domain events (search-result cards, push-to-main) derived from the
  tool return values.

Tools themselves stay pure (see ``pipeline/agent.py``); all UI eventing happens
here by observing the run.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, AsyncIterator

from pydantic_ai import (
    Agent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPartDelta,
    ThinkingPartDelta,
)
from pydantic_ai.messages import TextPart, ThinkingPart

from pipeline import agent_stream as S
from pipeline.agent_session import AgentSession
from pipeline.observability import (
    start_agent as _obs_start_agent,
    start_generation as _obs_start_generation,
    start_span as _obs_start_span,
    start_tool as _obs_start_tool,
    update_current_generation as _obs_update_current_generation,
)
from pipeline.search_telemetry import db_timing_payload


def _prompt_version(prompt: Any | None) -> str | None:
    if prompt is None:
        return None
    name = getattr(prompt, "name", None) or getattr(prompt, "prompt_name", None)
    version = getattr(prompt, "version", None)
    if name and version is not None:
        return f"{name}:v{version}"
    return name or (f"v{version}" if version is not None else None)


def _preview(value: Any, *, max_chars: int = 1000) -> Any:
    if isinstance(value, str):
        return value[:max_chars]
    if isinstance(value, list):
        return [_preview(v, max_chars=max_chars) for v in value[:20]]
    if isinstance(value, dict):
        return {k: _preview(v, max_chars=max_chars) for k, v in list(value.items())[:40]}
    return value


def _coerce_args(args: Any) -> Any:
    """Tool-call args may arrive as a JSON string or a dict; normalise to dict."""
    if args is None:
        return {}
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {"raw": args}
    return args


_TOOL_TRACE_NAMES = {
    "run_search": "tool.search_candidates",
    "modify_and_search": "tool.refine_search",
    "view_current_results": "tool.view_current_results",
    "get_candidate_detail": "tool.view_candidate",
    "get_candidate_details": "tool.view_candidates_batch",
    "explain_poor_results": "tool.diagnose_results",
    "compare_iterations": "tool.compare_searches",
    "keyword_search_batch": "tool.keyword_search_batch",
    "list_skills_batch": "tool.list_skills_batch",
    "list_recent_sessions": "tool.list_sessions",
    "query_candidates_db": "tool.query_database",
    "analyze_jd": "tool.analyze_job_description",
    "draft_outreach": "tool.draft_outreach",
    "generate_interview_questions": "tool.generate_interview_questions",
    "compare_candidates": "tool.compare_candidates",
}


def _trace_tool_name(tool_name: str) -> str:
    return _TOOL_TRACE_NAMES.get(tool_name, f"tool.{tool_name}")


def _tool_timing_payload(parsed: Any, latency_ms: float) -> dict[str, Any]:
    payload: dict[str, Any] = {"tool_wall_ms": latency_ms}
    if not isinstance(parsed, dict):
        return payload
    if isinstance(parsed.get("db_timing"), dict):
        payload["db_timing"] = db_timing_payload(parsed["db_timing"])
    retrieval_policy = parsed.get("retrieval_policy")
    if isinstance(retrieval_policy, dict):
        if isinstance(retrieval_policy.get("timing_summary"), dict):
            payload["retrieval_timing_summary"] = retrieval_policy["timing_summary"]
        if isinstance(retrieval_policy.get("timings_ms"), dict):
            payload["retrieval_timings_ms"] = retrieval_policy["timings_ms"]
    if isinstance(parsed.get("phase_timings"), dict):
        payload["phase_timings"] = parsed["phase_timings"]
    return payload


def _parse_content(content: Any) -> Any:
    """Tool results are JSON strings; parse them back to objects for the UI."""
    if isinstance(content, str):
        try:
            return json.loads(content)
        except (ValueError, TypeError):
            return content
    return content


def _summarize(tool_name: str, parsed: Any) -> str:
    """One-line, human-readable summary of a tool result for the activity log."""
    if not isinstance(parsed, dict):
        return "done"
    if "error" in parsed:
        return str(parsed["error"])
    if tool_name in ("run_search", "modify_and_search"):
        return f"{parsed.get('total', 0)} results"
    if tool_name == "view_current_results":
        return f"{len(parsed.get('results', []))} candidates"
    if tool_name == "keyword_search":
        return f"{parsed.get('total', 0)} matches"
    if tool_name == "keyword_search_batch":
        return f"{parsed.get('count', 0)} keyword matches"
    if tool_name == "get_candidate_detail":
        return parsed.get("full_name", "profile loaded")
    if tool_name == "get_candidate_details":
        return f"{parsed.get('count', 0)} profiles loaded"
    if tool_name == "list_skills_batch":
        return f"{parsed.get('count', 0)} skill matches"
    if tool_name == "save_hint":
        return "hint saved" if parsed.get("saved") else "not saved"
    if tool_name == "explain_poor_results":
        return "diagnosis ready"
    if tool_name == "compare_iterations":
        return "comparison ready"
    if tool_name == "push_to_main_panel":
        return "pushed to main panel"
    if tool_name == "query_candidates_db":
        return f"{parsed.get('count', '?')} rows"
    if tool_name in ("load_candidate_pool",):
        return f"pool of {parsed.get('count', 0)}"
    if tool_name in ("filter_from_pool", "aggregate_pool"):
        return f"{parsed.get('count', len(parsed))} items"
    if tool_name == "update_shortlist":
        return "shortlist updated"
    if tool_name == "update_working_spec":
        return "spec updated"
    if tool_name == "rerank_pool":
        return f"{parsed.get('reranked', '?')} reranked"
    if tool_name == "analyze_jd":
        return f"JD parsed: {parsed.get('role', 'role extracted')}"
    if tool_name == "draft_outreach":
        return f"draft ready for {parsed.get('candidate_name', 'candidate')}"
    if tool_name == "generate_interview_questions":
        return f"questions ready for {parsed.get('candidate_name', 'candidate')}"
    if tool_name == "compare_candidates":
        return "comparison ready"
    if tool_name == "save_search":
        return "search saved"
    if tool_name == "export_shortlist":
        n = len(parsed.get("shortlist_details", []))
        return f"{n} candidate{'s' if n != 1 else ''} exported"
    if tool_name == "confirm_observation":
        if "promoted_to_fact" in parsed:
            return "preference saved"
        if "dismissed" in parsed:
            return "observation dismissed"
        return "recorded"
    return "done"


def _domain_events(
    tool_name: str,
    parsed: Any,
    session: AgentSession,
    turn: dict[str, Any],
) -> list[str]:
    """Translate a tool result into rich SSE events the UI renders specially.

    ``turn`` carries per-turn state so we don't render the same candidate cards
    twice (e.g. a run_search immediately followed by view_current_results).
    """
    if not isinstance(parsed, dict):
        return []
    events: list[str] = []
    if tool_name in ("run_search", "modify_and_search"):
        results = parsed.get("results") or []
        if results:
            events.append(S.search_results_event(
                results, parsed.get("iteration_id", ""),
                parsed.get("query", ""), parsed.get("filters", {}),
                parsed.get("spec_summary", {}),
                parsed.get("latency_ms"),
                parsed.get("timings_ms", {}),
                parsed.get("phase_timings", {}),
                candidate_ids=parsed.get("candidate_ids") or [],
                deferred_candidate_ids=parsed.get("deferred_candidate_ids") or [],
                source="agent_search",
                result_kind="ranked",
                panel_title="Ranked results",
            ))
            turn["cards_shown"] = True
        events.append(S.stack_updated_event(len(session.search_stack)))
    elif tool_name == "view_current_results":
        results = parsed.get("results") or []
        if results and not turn.get("cards_shown"):
            events.append(S.search_results_event(
                results, "", parsed.get("query", ""), parsed.get("filters", {}),
                parsed.get("spec_summary", {}),
                parsed.get("latency_ms"),
                parsed.get("timings_ms", {}),
                parsed.get("phase_timings", {}),
                candidate_ids=parsed.get("candidate_ids") or [
                    str(r.get("id") or r.get("candidate_id"))
                    for r in results
                    if isinstance(r, dict) and (r.get("id") or r.get("candidate_id"))
                ],
                deferred_candidate_ids=parsed.get("deferred_candidate_ids") or [],
                source=parsed.get("source", "agent_search"),
                result_kind="ranked" if parsed.get("source") == "agent_search" else "selected",
                panel_title="Ranked results" if parsed.get("source") == "agent_search" else "Current candidates",
            ))
            turn["cards_shown"] = True
    elif tool_name == "keyword_search":
        results = parsed.get("results") or []
        if results:
            events.append(S.search_results_event(
                results, "", parsed.get("query", ""),
                source="keyword_search",
                result_kind="discovery",
                panel_title="Keyword matches",
            ))
            turn["cards_shown"] = True
    elif tool_name == "query_candidates_db":
        results = parsed.get("display_results") or []
        if results:
            display = parsed.get("display") if isinstance(parsed.get("display"), dict) else {}
            events.append(S.search_results_event(
                results, "", display.get("query", "Candidates from database query"), {},
                source=display.get("source", "query_candidates_db"),
                result_kind=display.get("result_kind", "selected"),
                panel_title="Agent-selected candidates",
            ))
            turn["cards_shown"] = True
    elif tool_name == "show_candidates":
        # Candidates the agent found by other means (DB lookup, detail) and chose
        # to surface — render them in the main results panel.
        results = parsed.get("results") or []
        if results:
            events.append(S.search_results_event(
                results, "", "Selected candidates", {},
                source="show_candidates",
                result_kind="selected",
                panel_title="Agent-selected candidates",
            ))
            turn["cards_shown"] = True
    elif tool_name == "push_to_main_panel":
        if parsed.get("pushed"):
            events.append(S.push_to_main_event(
                parsed.get("query", ""),
                parsed.get("filters", {}),
                parsed.get("result_ids", []),
            ))
    elif tool_name == "update_shortlist":
        if "shortlist" in parsed:
            events.append(S.shortlist_updated_event(parsed["shortlist"]))
    elif tool_name == "update_working_spec":
        if "working_spec" in parsed:
            events.append(S.spec_updated_event(parsed["working_spec"]))
    elif tool_name == "compare_candidates":
        if "comparison" in parsed:
            events.append(S.sse_event("COMPARE_CANDIDATES", parsed))
    return events


def _derive_suggestions(session: AgentSession) -> list[str]:
    """Context-aware next-action chips derived from the last search state."""
    if not session.search_stack:
        return []
    top = session.search_stack[-1]
    results = top.results_preview or []
    filters = top.filters or {}
    spec = top.spec_summary or {}

    if not results:
        return [
            "Try without location filter",
            "Remove experience minimum",
            "Search by keyword only",
        ]

    scores = [r.get("feature_score", 0) for r in results]
    avg = sum(scores) / len(scores) if scores else 0

    suggestions: list[str] = []

    # Location relaxation
    city = filters.get("city") or (spec.get("must_location") or {}).get("city")
    country = filters.get("country") or (spec.get("must_location") or {}).get("country")
    if city:
        suggestions.append(f"Relax city filter (remove {city})")
    elif country:
        suggestions.append(f"Remove country filter ({country})")

    # Experience relaxation
    min_exp = filters.get("min_years_exp") or (spec.get("experience_range") or {}).get("min_years")
    if min_exp:
        relaxed = max(0, int(min_exp) - 2)
        suggestions.append(f"Lower experience to {relaxed}+ yrs (was {min_exp}+)")

    # Failing required skill checks across top results
    failed: dict[str, int] = {}
    for r in results[:5]:
        for check in r.get("required_checks", []):
            if not check.get("matched"):
                skill = check.get("label", "").replace("Has ", "").strip()
                if skill:
                    failed[skill] = failed.get(skill, 0) + 1
    if failed:
        top_fail = max(failed, key=lambda k: failed[k])
        suggestions.append(f"Make {top_fail} optional (missing in {failed[top_fail]}/5)")

    # Dense-only retrieval + low quality
    dense_only = sum(1 for r in results if r.get("retrieval_paths") == ["dense"])
    if dense_only > len(results) * 0.5 and avg < 60:
        suggestions.append("Add explicit skill keywords to strengthen matches")

    # Planner quality
    if spec.get("used_fallback"):
        suggestions.append("Rephrase query — planner used fallback parsing")
    elif (spec.get("confidence") or 1.0) < 0.6:
        suggestions.append("Rephrase with role + key skill + location")

    # Salary ceiling
    if filters.get("max_salary") or filters.get("salary_max"):
        suggestions.append("Raise salary ceiling to see more candidates")

    # Widen geography when city + country both set
    if city and country and len(suggestions) < 4:
        suggestions.append(f"Widen to all of {country}")

    # Generic low-score fallback
    if avg < 40 and not suggestions:
        suggestions.append("Simplify to one core skill only")
        suggestions.append("Try a related role title")

    return suggestions[:4]


def _is_rate_limit(exc: Exception) -> bool:
    s = str(exc).lower()
    return "429" in s or "rate_limit" in s or "rate limit" in s


def _is_tool_use_failed(exc: Exception) -> bool:
    s = str(exc).lower()
    return (
        "tool_use_failed" in s
        or "failed_generation" in s
        or "failed to call a function" in s
    )


def _friendly_error(exc: Exception | None) -> str:
    """Turn a raw provider error into a short, recruiter-readable message."""
    if exc is None:
        return "Something went wrong. Please try again."
    s = str(exc)
    if _is_rate_limit(exc):
        m = re.search(r"try again in ([0-9hm.]+s)", s)
        when = f" Try again in {m.group(1)}." if m else " Try again shortly."
        return (
            "The agent model is rate-limited right now." + when
            + " You can also switch the agent model in Settings."
        )
    if _is_tool_use_failed(exc):
        return (
            "The model produced an invalid tool call and couldn't recover. "
            "Try rephrasing or shortening your message, or switch the agent "
            "model in Settings."
        )
    short = s.split("\n", 1)[0][:200]
    return f"Something went wrong: {short}"


def _fallback_model(model: str) -> str | None:
    """A different-provider model to fall back to when the primary keeps failing.

    Prefers the most reliable tool-caller available, skipping the current
    provider. DeepSeek > Gemini > Groq.
    """
    candidates: list[str] = []
    if os.environ.get("DEEPSEEK_API_KEY"):
        candidates.append("deepseek:deepseek-chat")
    if os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"):
        candidates.append("google:gemini-3.6-flash")
    if os.environ.get("GROQ_API_KEY"):
        candidates.append("groq:llama-3.3-70b-versatile")

    current_provider = model.split(":", 1)[0]
    for c in candidates:
        if c.split(":", 1)[0] != current_provider:
            return c
    return None


def _retry_plan(model: str) -> list[str]:
    """Models to try in order. One same-model retry (regeneration often fixes a
    transient tool_use_failed), then a cross-provider fallback if configured."""
    plan = [model, model]
    fb = _fallback_model(model)
    if fb:
        plan.append(fb)
    return plan


async def stream_agent_run(
    agent: Agent,
    *,
    message: str,
    model: str,
    deps: Any,
    session: AgentSession,
) -> AsyncIterator[str]:
    """Yield SSE strings for one agent turn, with graceful error handling.

    If the model fails before any output has streamed, retry (same model, then a
    configured fallback). If it fails mid-stream, surface a clean error rather
    than the raw provider dump. Persists message history only on success.
    """
    last_exc: Exception | None = None
    for attempt_idx, attempt_model in enumerate(_retry_plan(model)):
        emitted = {"any": False}
        attempt_started = time.perf_counter()
        with _obs_start_agent(
            "agent.respond",
            input={"message": message, "message_history_count": len(session.messages)},
            metadata={
                "attempt_index": attempt_idx,
                "model": attempt_model,
                "prompt_version": _prompt_version(getattr(deps, "lf_prompt", None)) or "inline",
            },
        ) as attempt_span:
            attempt_stats = {"tool_calls": 0, "model_requests": 0}
            emitted["_obs_attempt_stats"] = attempt_stats
            emitted["_obs_attempt_index"] = attempt_idx
            emitted["_obs_attempt_model"] = attempt_model
            emitted["_obs_prompt_version"] = _prompt_version(getattr(deps, "lf_prompt", None)) or "inline"
            emitted["_obs_system_prompt"] = getattr(deps, "system_prompt", "")
            try:
                async for ev in _run_once(agent, message, attempt_model, deps, session, emitted):
                    yield ev
                # After a successful search turn, emit context-aware next-action chips
                suggestions = _derive_suggestions(session)
                if suggestions:
                    with _obs_start_span("agent.suggest_next_actions", output={"suggestions": suggestions}):
                        yield S.suggested_actions_event(suggestions)
                attempt_span.update(output={
                    "status": "ok",
                    "tool_calls": attempt_stats["tool_calls"],
                    "model_requests": attempt_stats["model_requests"],
                    "suggestions_count": len(suggestions),
                    "latency_ms": round((time.perf_counter() - attempt_started) * 1000, 2),
                })
                return  # completed successfully
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                attempt_span.update(output={
                    "status": "error",
                    "tool_calls": attempt_stats["tool_calls"],
                    "model_requests": attempt_stats["model_requests"],
                    "error": _friendly_error(exc),
                    "latency_ms": round((time.perf_counter() - attempt_started) * 1000, 2),
                })
                if emitted["any"]:
                    # Output already streamed — a retry would duplicate it.
                    yield S.error_event(_friendly_error(exc))
                    return
                if _is_rate_limit(exc):
                    break  # retrying the same provider won't help within the window
                # otherwise fall through to the next model in the plan
    yield S.error_event(_friendly_error(last_exc))


async def _run_once(
    agent: Agent,
    message: str,
    model: str,
    deps: Any,
    session: AgentSession,
    emitted: dict[str, bool],
) -> AsyncIterator[str]:
    """One streaming attempt. Raises on failure (caller decides whether to retry).

    Sets ``emitted['any']`` once any visible event has been yielded so the caller
    knows a retry would produce duplicate output.
    """
    tool_names: dict[str, str] = {}      # tool_call_id -> tool name
    tool_args: dict[str, Any] = {}        # tool_call_id -> parsed args
    tool_spans: dict[str, tuple[Any, Any, float]] = {}
    turn: dict[str, Any] = {}            # per-turn UI state (e.g. cards_shown)
    open_text_id: str | None = None
    open_thinking_id: str | None = None
    attempt_stats = emitted.get("_obs_attempt_stats") or {}

    try:
        async with agent.iter(
            message,
            model=model,
            deps=deps,
            message_history=session.messages,
        ) as run:
            async for node in run:
                if Agent.is_model_request_node(node):
                    attempt_stats["model_requests"] = attempt_stats.get("model_requests", 0) + 1
                    text_chunks: list[str] = []
                    thinking_chunks: list[str] = []
                    gen_kwargs = {
                        "model": model,
                        "input": [
                            {"role": "system", "content": emitted.get("_obs_system_prompt", "")},
                            {
                                "role": "user",
                                "content": message,
                                "message_history_count": len(session.messages),
                            },
                        ],
                        "metadata": {
                            "attempt_index": emitted.get("_obs_attempt_index"),
                            "prompt_version": emitted.get("_obs_prompt_version", "inline"),
                        },
                    }
                    lf_prompt = getattr(deps, "lf_prompt", None)
                    if lf_prompt is not None:
                        gen_kwargs["prompt"] = lf_prompt
                    with _obs_start_generation("agent.generate_reply", **gen_kwargs):
                        async with node.stream(run.ctx) as request_stream:
                            async for event in request_stream:
                                if isinstance(event, PartStartEvent):
                                    part = event.part
                                    if isinstance(part, TextPart):
                                        if open_thinking_id:
                                            yield S.thinking_end(open_thinking_id)
                                            open_thinking_id = None
                                        if part.content:
                                            # non-streaming: full content in PartStart
                                            if open_text_id is None:
                                                open_text_id = str(uuid.uuid4())
                                                emitted["any"] = True
                                                yield S.text_start(open_text_id)
                                            text_chunks.append(part.content)
                                            yield S.text_delta(open_text_id, part.content)
                                        # empty TextPart: defer start to first delta —
                                        # avoids spurious empty bubbles when Gemini
                                        # emits a blank TextPart before tool calls
                                    elif isinstance(part, ThinkingPart):
                                        if open_text_id:
                                            yield S.text_end(open_text_id)
                                            open_text_id = None
                                        if open_thinking_id is None:
                                            open_thinking_id = str(uuid.uuid4())
                                            emitted["any"] = True
                                            yield S.thinking_start(open_thinking_id)
                                        if part.content:
                                            thinking_chunks.append(part.content)
                                            yield S.thinking_delta(open_thinking_id, part.content)
                                    else:
                                        # ToolCallPart or other — model switching to tools
                                        if open_text_id:
                                            yield S.text_end(open_text_id)
                                            open_text_id = None
                                        if open_thinking_id:
                                            yield S.thinking_end(open_thinking_id)
                                            open_thinking_id = None
                                elif isinstance(event, PartDeltaEvent):
                                    delta = event.delta
                                    if isinstance(delta, TextPartDelta) and delta.content_delta:
                                        if open_text_id is None:
                                            open_text_id = str(uuid.uuid4())
                                            emitted["any"] = True
                                            yield S.text_start(open_text_id)
                                        text_chunks.append(delta.content_delta)
                                        yield S.text_delta(open_text_id, delta.content_delta)
                                    elif isinstance(delta, ThinkingPartDelta) and delta.content_delta:
                                        if open_thinking_id is None:
                                            open_thinking_id = str(uuid.uuid4())
                                            emitted["any"] = True
                                            yield S.thinking_start(open_thinking_id)
                                        thinking_chunks.append(delta.content_delta)
                                        yield S.thinking_delta(open_thinking_id, delta.content_delta)
                        _obs_update_current_generation(output={
                            "text": "".join(text_chunks),
                            "thinking": "".join(thinking_chunks),
                        })
                    if open_thinking_id:
                        yield S.thinking_end(open_thinking_id)
                        open_thinking_id = None

                elif Agent.is_call_tools_node(node):
                    if open_text_id:
                        yield S.text_end(open_text_id)
                        open_text_id = None
                    async with node.stream(run.ctx) as handle_stream:
                        async for event in handle_stream:
                            if isinstance(event, FunctionToolCallEvent):
                                tcid = event.part.tool_call_id
                                name = event.part.tool_name
                                args = _coerce_args(event.part.args)
                                tool_names[tcid] = name
                                tool_args[tcid] = args
                                attempt_stats["tool_calls"] = attempt_stats.get("tool_calls", 0) + 1
                                cm = _obs_start_tool(
                                    _trace_tool_name(name),
                                    input=_preview(args),
                                    metadata={
                                        "tool_call_id": tcid,
                                        "attempt_index": emitted.get("_obs_attempt_index"),
                                        "model": emitted.get("_obs_attempt_model"),
                                    },
                                )
                                span = cm.__enter__()
                                tool_spans[tcid] = (cm, span, time.perf_counter())
                                emitted["any"] = True
                                yield S.tool_call_start(tcid, name, args)
                            elif isinstance(event, FunctionToolResultEvent):
                                tcid = event.tool_call_id
                                name = tool_names.get(tcid, "")
                                parsed = _parse_content(getattr(event.part, "content", None))
                                cm_span = tool_spans.pop(tcid, None)
                                if cm_span is not None:
                                    cm, span, started = cm_span
                                    latency_ms = round((time.perf_counter() - started) * 1000, 2)
                                    span.update(output={
                                        "summary": _summarize(name, parsed),
                                        "result": _preview(parsed),
                                        "latency_ms": latency_ms,
                                        "timing": _tool_timing_payload(parsed, latency_ms),
                                    })
                                    cm.__exit__(None, None, None)
                                yield S.tool_call_end(tcid, _summarize(name, parsed), parsed)
                                for ev in _domain_events(name, parsed, session, turn):
                                    yield ev

            if open_text_id:
                yield S.text_end(open_text_id)
                open_text_id = None
            if open_thinking_id:
                yield S.thinking_end(open_thinking_id)
                open_thinking_id = None
            if run.result is not None:
                session.messages = run.result.all_messages()
    except Exception:  # noqa: BLE001 — close open UI parts, then let caller decide
        for cm, span, started in list(tool_spans.values()):
            span.update(output={
                "status": "error",
                "latency_ms": round((time.perf_counter() - started) * 1000, 2),
            })
            cm.__exit__(None, None, None)
        if open_text_id:
            yield S.text_end(open_text_id)
        if open_thinking_id:
            yield S.thinking_end(open_thinking_id)
        raise
