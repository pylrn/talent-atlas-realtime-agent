#!/usr/bin/env python3
"""Ramp-test direct POST /search requests.

This isolates raw search/retrieval capacity from the Talent agent loop. Each
request is a normal no-LLM search payload like the UI sends after the agent has
formed a structured query.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import aiohttp


QUERIES: list[dict[str, Any]] = [
    {
        "name": "python_bengaluru",
        "query": "senior python engineer backend development",
        "city": "Bengaluru",
        "skills": ["python"],
        "min_years_exp": 5,
    },
    {
        "name": "data_pune",
        "query": "data engineer spark airflow etl data warehouse pipelines",
        "city": "Pune",
        "skills": ["spark", "sql"],
    },
    {
        "name": "linux_bangalore",
        "query": "linux infrastructure engineer hosting llm services docker kubernetes",
        "city": "Bangalore",
        "skills": ["linux"],
    },
    {
        "name": "java_bangalore",
        "query": "java spring boot backend developer REST APIs microservices",
        "city": "Bangalore",
        "skills": ["java"],
    },
    {
        "name": "react_sf",
        "query": "react frontend engineer typescript accessible responsive UI",
        "city": "San Francisco",
        "skills": ["react"],
    },
    {
        "name": "devops_india",
        "query": "devops engineer kubernetes terraform docker aws ci cd",
        "country": "India",
        "skills": ["kubernetes"],
    },
    {
        "name": "sql_hyderabad",
        "query": "sql developer database administrator performance tuning SSIS SSRS",
        "city": "Hyderabad",
        "skills": ["sql"],
    },
    {
        "name": "finance_ny",
        "query": "finance manager budgeting forecasting reporting accounting",
        "city": "New York",
    },
    {
        "name": "salesforce_pune",
        "query": "salesforce administrator apex visualforce workflow automation",
        "city": "Pune",
        "skills": ["salesforce"],
    },
    {
        "name": "hotel_ops",
        "query": "hotel operations manager front desk guest handling booking systems",
        "status": ["active"],
    },
    {
        "name": "cloud_security",
        "query": "cloud security engineer aws azure incident response compliance",
        "skills": ["aws"],
    },
    {
        "name": "mobile_flutter",
        "query": "mobile app developer flutter react native android ios",
        "skills": ["flutter"],
    },
    {
        "name": "ml_python",
        "query": "machine learning engineer python deep learning model deployment",
        "skills": ["python"],
    },
    {
        "name": "product_manager",
        "query": "product manager analytics roadmap stakeholder communication",
        "status": ["active"],
    },
    {
        "name": "marketing_vague",
        "query": "marketing growth campaigns content analytics",
    },
    {
        "name": "usa_leader_broad",
        "query": "experienced leader operations strategy team management",
        "country": "USA",
    },
    {
        "name": "remote_backend",
        "query": "backend engineer distributed systems postgres redis APIs",
        "skills": ["postgresql"],
    },
    {
        "name": "hr_manager",
        "query": "human resources manager recruiting employee relations payroll",
        "status": ["active"],
    },
    {
        "name": "designer",
        "query": "product designer user research design systems figma",
    },
    {
        "name": "qa_automation",
        "query": "qa automation engineer selenium test frameworks ci",
        "skills": ["selenium"],
    },
]


@dataclass
class Result:
    index: int
    name: str
    status: int | None
    ok: bool
    wall_ms: float
    service_wall_ms: float | None = None
    queue_wait_ms: float = 0.0
    api_latency_ms: float | None = None
    total_results: int | None = None
    search_pipeline_ms: float | None = None
    search_total_ms: float | None = None
    trace_id: str | None = None
    trace_url: str | None = None
    phase_timings: dict[str, float] = field(default_factory=dict)
    retrieval_policy: dict[str, Any] = field(default_factory=dict)
    error: str = ""


def _payload(case: dict[str, Any], args: argparse.Namespace, index: int, run_id: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "query": case["query"],
        "mode": args.mode,
        "top_k": args.top_k,
        "session_id": f"search-load-{run_id}-{index}-{uuid.uuid4().hex[:8]}",
        "recruiter_id": args.recruiter_id,
        "enable_reranking": args.enable_reranking,
        "include_rank_explanation": args.include_rank_explanation,
        "include_ai_insights": False,
        "keyword_policy": args.keyword_policy,
    }
    if args.keyword_timeout_ms:
        payload["keyword_timeout_ms"] = args.keyword_timeout_ms
    if args.config_overrides:
        payload["config_overrides"] = args.config_overrides
    for key in (
        "city",
        "country",
        "skills",
        "status",
        "min_years_exp",
        "max_years_exp",
        "min_salary",
        "max_salary",
    ):
        if key in case:
            payload[key] = case[key]
    return payload


async def one_search(
    client: aiohttp.ClientSession,
    *,
    base_url: str,
    case: dict[str, Any],
    args: argparse.Namespace,
    index: int,
    run_id: str,
) -> Result:
    started = time.perf_counter()
    status: int | None = None
    try:
        async with client.post(
            f"{base_url.rstrip('/')}/search",
            json=_payload(case, args, index, run_id),
            timeout=aiohttp.ClientTimeout(total=args.timeout),
        ) as resp:
            status = resp.status
            text = await resp.text()
            wall_ms = (time.perf_counter() - started) * 1000
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return Result(index, case["name"], status, False, wall_ms, error=text[:240])
            if status != 200:
                return Result(index, case["name"], status, False, wall_ms, error=json.dumps(data)[:240])
            timings = data.get("timings_ms") or {}
            return Result(
                index=index,
                name=case["name"],
                status=status,
                ok=True,
                wall_ms=wall_ms,
                api_latency_ms=data.get("latency_ms"),
                total_results=data.get("total_results"),
                search_pipeline_ms=timings.get("search_pipeline"),
                search_total_ms=timings.get("search_total"),
                trace_id=data.get("langfuse_trace_id"),
                trace_url=data.get("langfuse_trace_url"),
                phase_timings=data.get("phase_timings") or {},
                retrieval_policy=data.get("retrieval_policy") or {},
            )
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        return Result(
            index=index,
            name=case["name"],
            status=status,
            ok=False,
            wall_ms=(time.perf_counter() - started) * 1000,
            error=f"{type(exc).__name__}: {exc}",
        )


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(len(values) - 1, max(0, round((len(values) - 1) * pct)))
    return values[idx]


def _avg(values: list[float | None]) -> float | None:
    clean = [float(v) for v in values if v is not None]
    if not clean:
        return None
    return round(sum(clean) / len(clean), 1)


def summarize(results: list[Result]) -> dict[str, Any]:
    walls = [r.wall_ms for r in results]
    service_walls = [r.service_wall_ms if r.service_wall_ms is not None else r.wall_ms for r in results]
    queue_waits = [r.queue_wait_ms for r in results]
    ok = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    phase_keys = sorted({key for r in ok for key in r.phase_timings})
    phases = {
        key: _avg([r.phase_timings.get(key) for r in ok])
        for key in phase_keys
    }
    keyword_statuses: dict[str, int] = {}
    for result in ok:
        keyword = result.retrieval_policy.get("keyword") or {}
        status = str(keyword.get("status") or keyword.get("action") or "unknown")
        keyword_statuses[status] = keyword_statuses.get(status, 0) + 1
    return {
        "total": len(results),
        "ok": len(ok),
        "failed": len(failed),
        "wall_p50_ms": round(statistics.median(walls), 1) if walls else 0,
        "wall_p95_ms": round(_percentile(walls, 0.95), 1) if walls else 0,
        "wall_max_ms": round(max(walls), 1) if walls else 0,
        "service_p50_ms": round(statistics.median(service_walls), 1) if service_walls else 0,
        "service_p95_ms": round(_percentile(service_walls, 0.95), 1) if service_walls else 0,
        "queue_wait_p50_ms": round(statistics.median(queue_waits), 1) if queue_waits else 0,
        "queue_wait_p95_ms": round(_percentile(queue_waits, 0.95), 1) if queue_waits else 0,
        "queue_wait_max_ms": round(max(queue_waits), 1) if queue_waits else 0,
        "api_avg_latency_ms": _avg([r.api_latency_ms for r in ok]),
        "pipeline_avg_ms": _avg([r.search_pipeline_ms for r in ok]),
        "avg_results": _avg([r.total_results for r in ok]),
        "avg_phases_ms": phases,
        "keyword_statuses": keyword_statuses,
        "errors": [r.error for r in failed[:10]],
    }


async def run_level(args: argparse.Namespace, concurrency: int, run_id: str) -> list[Result]:
    connector = aiohttp.TCPConnector(limit=max(10, concurrency * 2))
    async with aiohttp.ClientSession(connector=connector) as client:
        semaphore = (
            asyncio.Semaphore(args.request_concurrency)
            if args.request_concurrency and args.request_concurrency > 0
            else None
        )

        async def queued_search(case: dict[str, Any], index: int) -> Result:
            queued_at = time.perf_counter()
            if semaphore is None:
                result = await one_search(
                    client,
                    base_url=args.base_url,
                    case=case,
                    args=args,
                    index=index,
                    run_id=run_id,
                )
                result.service_wall_ms = result.wall_ms
                return result

            async with semaphore:
                queue_wait_ms = (time.perf_counter() - queued_at) * 1000
                result = await one_search(
                    client,
                    base_url=args.base_url,
                    case=case,
                    args=args,
                    index=index,
                    run_id=run_id,
                )
                result.service_wall_ms = result.wall_ms
                result.queue_wait_ms = queue_wait_ms
                result.wall_ms = queue_wait_ms + result.wall_ms
                return result

        tasks = []
        for index in range(concurrency):
            case = QUERIES[index % len(QUERIES)]
            tasks.append(asyncio.create_task(queued_search(case, index)))
            if args.stagger_ms:
                await asyncio.sleep(args.stagger_ms / 1000)
        return await asyncio.gather(*tasks)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--recruiter-id", default="7f23e9d8-d2e5-45a4-8973-54c31f21a4dd")
    parser.add_argument("--levels", default="1,5,10,20,30,50")
    parser.add_argument("--timeout", type=float, default=45.0)
    parser.add_argument("--stagger-ms", type=int, default=0)
    parser.add_argument("--mode", choices=["no-llm", "fast", "quality", "agent-quality"], default="no-llm")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--keyword-policy", choices=["auto", "skip", "force"], default="auto")
    parser.add_argument("--keyword-timeout-ms", type=int, default=0)
    parser.add_argument(
        "--config-overrides-json",
        default="{}",
        help="JSON object passed through to SearchRequest.config_overrides.",
    )
    parser.add_argument(
        "--output-json",
        default="",
        help="Optional path to write summaries and per-request results as JSON.",
    )
    parser.add_argument(
        "--request-concurrency",
        type=int,
        default=0,
        help=(
            "Optional request-level queue cap. The batch still creates N users, "
            "but only this many requests may enter /search at once. wall_ms "
            "includes queue wait; service_ms is the actual HTTP request time."
        ),
    )
    parser.add_argument(
        "--warmup-runs",
        type=int,
        default=0,
        help="Optional number of warmup requests to run before measured levels.",
    )
    parser.add_argument("--enable-reranking", action="store_true")
    parser.add_argument("--include-rank-explanation", action="store_true")
    parser.add_argument("--stop-on-failure", action="store_true")
    parser.add_argument("--summary-only", action="store_true")
    args = parser.parse_args()
    try:
        args.config_overrides = json.loads(args.config_overrides_json or "{}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid --config-overrides-json: {exc}") from exc
    if not isinstance(args.config_overrides, dict):
        raise SystemExit("--config-overrides-json must decode to an object")

    run_id = uuid.uuid4().hex[:8]
    cache_mode = {
        "use_cache": bool(args.config_overrides.get("use_cache", True)),
        "use_retrieval_cache": bool(args.config_overrides.get("use_retrieval_cache", False)),
        "keyword_policy": args.keyword_policy,
    }
    if args.warmup_runs:
        warmup_level = max(1, int(args.warmup_runs))
        print(f"\n== warmup {warmup_level} requests ==", flush=True)
        await run_level(args, warmup_level, f"{run_id}-warmup")
    all_results: dict[int, list[Result]] = {}
    all_summaries: dict[int, dict[str, Any]] = {}
    levels = [int(value.strip()) for value in args.levels.split(",") if value.strip()]
    for level in levels:
        print(f"\n== concurrency {level} ==", flush=True)
        level_started = time.perf_counter()
        results = await run_level(args, level, run_id)
        batch_elapsed_ms = (time.perf_counter() - level_started) * 1000
        all_results[level] = results
        summary = summarize(results)
        summary["batch_elapsed_ms"] = round(batch_elapsed_ms, 1)
        summary["cache_mode"] = dict(cache_mode)
        all_summaries[level] = summary
        print(json.dumps(summary, indent=2), flush=True)
        if not args.summary_only:
            for result in results:
                state = "ok" if result.ok else "FAIL"
                keyword = result.retrieval_policy.get("keyword") or {}
                keyword_status = keyword.get("status") or keyword.get("action") or "?"
                print(
                    f"  #{result.index:02d} {state} {result.name} status={result.status} "
                    f"wall={result.wall_ms:.1f}ms api={result.api_latency_ms}ms "
                    f"queue={result.queue_wait_ms:.1f}ms service={(result.service_wall_ms or result.wall_ms):.1f}ms "
                    f"pipeline={result.search_pipeline_ms}ms results={result.total_results} "
                    f"keyword={keyword_status} trace={result.trace_id or '-'} {result.error}",
                    flush=True,
                )
        if args.stop_on_failure and summary["failed"]:
            break

    print("\n== final ==", flush=True)
    print(json.dumps({
        "run_id": run_id,
        "cache_mode": cache_mode,
        "summaries": {str(k): v for k, v in all_summaries.items()},
    }, indent=2), flush=True)
    if args.output_json:
        payload = {
            "run_id": run_id,
            "cache_mode": cache_mode,
            "summaries": {str(k): v for k, v in all_summaries.items()},
            "results": {
                str(k): [asdict(result) for result in results]
                for k, results in all_results.items()
            },
        }
        with open(args.output_json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")


if __name__ == "__main__":
    asyncio.run(main())
