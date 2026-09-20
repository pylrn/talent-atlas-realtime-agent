from __future__ import annotations

import json
from typing import Any


def sse_event(event_type: str, data: dict[str, Any]) -> str:
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def text_start(message_id: str) -> str:
    return sse_event("TEXT_MESSAGE_START", {"messageId": message_id, "role": "assistant"})


def text_delta(message_id: str, delta: str) -> str:
    return sse_event("TEXT_MESSAGE_CONTENT", {"messageId": message_id, "delta": delta})


def text_end(message_id: str) -> str:
    return sse_event("TEXT_MESSAGE_END", {"messageId": message_id})


def thinking_start(message_id: str) -> str:
    return sse_event("THINKING_START", {"messageId": message_id})


def thinking_delta(message_id: str, delta: str) -> str:
    return sse_event("THINKING_CONTENT", {"messageId": message_id, "delta": delta})


def thinking_end(message_id: str) -> str:
    return sse_event("THINKING_END", {"messageId": message_id})


def tool_call_start(tool_call_id: str, tool_name: str, args: Any = None) -> str:
    return sse_event("TOOL_CALL_START", {
        "toolCallId": tool_call_id,
        "toolCallName": tool_name,
        "args": args,
    })


def tool_call_end(tool_call_id: str, result_summary: str = "", result: Any = None) -> str:
    return sse_event("TOOL_CALL_END", {
        "toolCallId": tool_call_id,
        "resultSummary": result_summary,
        "result": result,
    })


def search_results_event(
    results: list[dict],
    iteration_id: str,
    query: str = "",
    filters: dict | None = None,
    spec: dict | None = None,
    latency_ms: float | None = None,
    timings_ms: dict[str, float] | None = None,
    phase_timings: dict[str, float] | None = None,
    candidate_ids: list[str] | None = None,
    deferred_candidate_ids: list[str] | None = None,
    source: str = "agent_search",
    result_kind: str = "ranked",
    panel_title: str = "Ranked results",
) -> str:
    return sse_event("SEARCH_RESULTS", {
        "results": results,
        "iterationId": iteration_id,
        "query": query,
        "filters": filters or {},
        "specSummary": spec or {},
        "latency_ms": latency_ms,
        "timings_ms": timings_ms or {},
        "phase_timings": phase_timings or {},
        "candidate_ids": candidate_ids or [],
        "deferred_candidate_ids": deferred_candidate_ids or [],
        "source": source,
        "resultKind": result_kind,
        "panelTitle": panel_title,
    })


def push_to_main_event(query: str, filters: dict, result_ids: list[str]) -> str:
    return sse_event("PUSH_TO_MAIN", {
        "query": query,
        "filters": filters,
        "resultIds": result_ids,
    })


def stack_updated_event(depth: int) -> str:
    return sse_event("STACK_UPDATED", {"depth": depth})


def suggested_actions_event(suggestions: list[str]) -> str:
    return sse_event("SUGGESTED_ACTIONS", {"suggestions": suggestions})


def error_event(message: str) -> str:
    return sse_event("ERROR", {"message": message})


def spec_updated_event(spec: dict) -> str:
    return sse_event("SPEC_UPDATED", {"spec": spec})

def shortlist_updated_event(shortlist: dict) -> str:
    return sse_event("SHORTLIST_UPDATED", {"shortlist": shortlist})
