"""Opt-in Gemini Live smoke test through the public Talent Atlas WebSocket."""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import websockets


async def run(url: str, prompt: str, timeout: float) -> dict[str, object]:
    observed: list[str] = []
    started = time.perf_counter()
    async with websockets.connect(url, max_size=8 * 1024 * 1024) as socket:
        connected = json.loads(await asyncio.wait_for(socket.recv(), timeout=timeout))
        observed.append(str(connected.get("type")))
        await socket.send(json.dumps({"type": "session.start", "audio": False}))
        ready = False
        sent_prompt = False
        tool_completed = False
        completed_tools: list[str] = []
        started_tools: list[dict[str, object]] = []
        search_result: dict[str, object] = {}
        branch_counts: dict[str, int] = {}
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            remaining = deadline - asyncio.get_running_loop().time()
            message = await asyncio.wait_for(socket.recv(), timeout=remaining)
            if isinstance(message, bytes):
                observed.append("audio.output")
                continue
            event = json.loads(message)
            event_type = str(event.get("type"))
            observed.append(event_type)
            if event_type == "session.ready" and not sent_prompt:
                ready = True
                sent_prompt = True
                await socket.send(json.dumps({"type": "text.input", "text": prompt}))
            if event_type == "tool.started":
                payload = event.get("payload") or {}
                started_tools.append({
                    "name": payload.get("name"),
                    "arguments": payload.get("arguments") or {},
                })
            if event_type == "tool.completed":
                payload = event.get("payload") or {}
                tool_name = str(payload.get("name") or "")
                completed_tools.append(tool_name)
                if tool_name not in {"search_candidates", "revise_search"}:
                    continue
                tool_completed = True
                result = payload.get("result") or {}
                search_result = {
                    "count": result.get("count"),
                    "candidate_names": [
                        candidate.get("name")
                        for candidate in (result.get("candidates") or [])[:3]
                    ],
                    "candidate_locations": [
                        ", ".join(filter(None, [candidate.get("city"), candidate.get("country")]))
                        for candidate in (result.get("candidates") or [])[:3]
                    ],
                    "plan": result.get("plan") or {},
                    "relaxations_applied": result.get("relaxations_applied") or [],
                }
                await socket.send(json.dumps({"type": "trace.snapshot"}))
                continue
            if event_type == "trace.snapshot":
                nodes = (event.get("payload") or {}).get("nodes") or {}
                for node in nodes.values():
                    kind = str(node.get("kind") or "")
                    if kind in {"sql", "vector", "bm25", "skills", "fusion", "rerank"}:
                        details = node.get("details") or {}
                        branch_counts[kind] = int(details.get("candidate_count") or 0)
                break
            if event_type in {"error", "gemini.error"}:
                break
        await socket.send(json.dumps({"type": "session.stop"}))
    return {
        "ready": ready,
        "tool_completed": tool_completed,
        "completed_tools": completed_tools,
        "started_tools": started_tools,
        "search_result": search_result,
        "branch_candidate_counts": branch_counts,
        "event_types": observed,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="ws://127.0.0.1:8010/talent/realtime/ws")
    parser.add_argument(
        "--prompt",
        default="Find three Python engineers and use the candidate search tool before answering.",
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    result = asyncio.run(run(args.url, args.prompt, args.timeout))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ready"] and result["tool_completed"] else 1)


if __name__ == "__main__":
    main()
