#!/usr/bin/env python3
"""Ramp-test the Talent UI agent endpoint.

This hits the same POST /agent/chat streaming endpoint used by api/static/talent.js.
Each request uses a unique session_id so it behaves like separate browser sessions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid
from dataclasses import dataclass
from typing import Any

import aiohttp


QUERIES = [
    "Find senior python backend engineers in Bengaluru with FastAPI and AWS experience.",
    "Show me data engineers in Pune who mention Spark Airflow ETL and warehouses.",
    "Find React frontend developers in San Francisco with TypeScript and Docker.",
    "Find Java Spring Boot developers in Bangalore with REST API experience.",
    "Find Linux infrastructure engineers who have hosted LLM services.",
    "Show Salesforce admins in Pune with Apex Visualforce and workflow experience.",
    "Find SQL developers in Hyderabad with SSIS SSRS and performance tuning.",
    "Find finance managers in New York with budgeting forecasting and reporting.",
    "Find DevOps engineers in India with Kubernetes Terraform Docker and AWS.",
    "Find hotel operations managers with front desk guest handling and booking systems.",
]


@dataclass
class Result:
    index: int
    status: int | None
    ok: bool
    latency_ms: float
    events: int
    tool_calls: int
    searches: int
    errors: list[str]
    body_bytes: int


def _parse_event(block: str) -> tuple[str, dict[str, Any]]:
    event = ""
    data = ""
    for line in block.splitlines():
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: "):
            data += line[6:]
    if not event:
        return "", {}
    try:
        return event, json.loads(data) if data else {}
    except json.JSONDecodeError:
        return "ERROR", {"message": "Malformed SSE payload"}


async def one_request(
    client: aiohttp.ClientSession,
    *,
    base_url: str,
    run_id: str,
    index: int,
    message: str,
    recruiter_id: str,
    model: str,
    timeout_s: float,
) -> Result:
    started = time.perf_counter()
    session_id = f"load-{run_id}-{index}-{uuid.uuid4().hex[:8]}"
    payload = {
        "recruiter_id": recruiter_id,
        "session_id": session_id,
        "message": message,
        "context": {
            "query": "",
            "filters": {},
            "result_ids": [],
            "candidate_ids": [],
            "candidate_summaries": [],
            "session_messages": [],
            "session_summary": "",
            "hints": [],
        },
        "model": model,
    }
    events = 0
    tool_calls = 0
    searches = 0
    errors: list[str] = []
    body_bytes = 0
    status: int | None = None
    buf = ""

    try:
        async with client.post(
            f"{base_url.rstrip('/')}/agent/chat",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout_s),
        ) as resp:
            status = resp.status
            async for chunk in resp.content.iter_chunked(4096):
                text = chunk.decode("utf-8", errors="replace")
                body_bytes += len(chunk)
                buf += text
                blocks = buf.split("\n\n")
                buf = blocks.pop()
                for block in blocks:
                    if not block.strip():
                        continue
                    event, data = _parse_event(block)
                    if not event:
                        continue
                    events += 1
                    if event == "TOOL_CALL_START":
                        tool_calls += 1
                    elif event == "SEARCH_RESULTS":
                        searches += 1
                    elif event == "ERROR":
                        errors.append(str(data.get("message") or "unknown error"))
            ok = status == 200 and not errors
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic tool
        errors.append(f"{type(exc).__name__}: {exc}")
        ok = False

    return Result(
        index=index,
        status=status,
        ok=ok,
        latency_ms=(time.perf_counter() - started) * 1000,
        events=events,
        tool_calls=tool_calls,
        searches=searches,
        errors=errors,
        body_bytes=body_bytes,
    )


def summarize(results: list[Result]) -> dict[str, Any]:
    latencies = [r.latency_ms for r in results]
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    return {
        "total": len(results),
        "ok": len(ok),
        "failed": len(failed),
        "p50_ms": round(statistics.median(latencies), 1) if latencies else 0,
        "p95_ms": round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 1) if latencies else 0,
        "max_ms": round(max(latencies), 1) if latencies else 0,
        "avg_tool_calls": round(sum(r.tool_calls for r in results) / max(1, len(results)), 2),
        "avg_search_events": round(sum(r.searches for r in results) / max(1, len(results)), 2),
        "errors": [err for r in failed for err in r.errors[:2]][:10],
    }


async def run_level(args: argparse.Namespace, concurrency: int, run_id: str) -> list[Result]:
    connector = aiohttp.TCPConnector(limit=max(concurrency, 1) * 2)
    async with aiohttp.ClientSession(connector=connector) as client:
        tasks = []
        for i in range(concurrency):
            message = QUERIES[i % len(QUERIES)]
            tasks.append(asyncio.create_task(one_request(
                client,
                base_url=args.base_url,
                run_id=run_id,
                index=i,
                message=message,
                recruiter_id=args.recruiter_id,
                model=args.model,
                timeout_s=args.timeout,
            )))
            if args.stagger_ms:
                await asyncio.sleep(args.stagger_ms / 1000)
        return await asyncio.gather(*tasks)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--recruiter-id", default="7f23e9d8-d2e5-45a4-8973-54c31f21a4dd")
    parser.add_argument("--model", default="deepseek:deepseek-chat")
    parser.add_argument("--levels", default="1,2,3,5,8,10")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--stagger-ms", type=int, default=0)
    parser.add_argument("--stop-on-failure", action="store_true")
    args = parser.parse_args()

    run_id = uuid.uuid4().hex[:8]
    levels = [int(x.strip()) for x in args.levels.split(",") if x.strip()]
    all_results: dict[int, list[Result]] = {}
    for level in levels:
        print(f"\n== concurrency {level} ==")
        results = await run_level(args, level, run_id)
        all_results[level] = results
        summary = summarize(results)
        print(json.dumps(summary, indent=2))
        for result in results:
            state = "ok" if result.ok else "FAIL"
            first_error = result.errors[0] if result.errors else ""
            print(
                f"  #{result.index:02d} {state} status={result.status} "
                f"latency={result.latency_ms:.1f}ms events={result.events} "
                f"tools={result.tool_calls} searches={result.searches} {first_error}"
            )
        if args.stop_on_failure and summary["failed"]:
            break

    print("\n== final ==")
    print(json.dumps({str(k): summarize(v) for k, v in all_results.items()}, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
