#!/usr/bin/env python3
"""Benchmark Cache Performance

Runs the 20-query no-LLM benchmark across 4 caching scenarios:
1. Memory, retrieval cache off (L1 benefit only)
2. Memory, retrieval cache on (L1 + rowset cache)
3. Redis, retrieval cache off (L2 benefit only)
4. Redis, retrieval cache on (L2 + rowset cache)

For each scenario, it performs:
- A Cold Run (with a unique namespace/cleared cache)
- A Warm Run (immediate execution of the same queries)

It compares latencies, hit rates, and database/pool statistics, while
verifying result correctness (parity) against a cache-disabled baseline.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import time
from pathlib import Path
from typing import Any

from pipeline import settings
from pipeline import cache as _cache
from pipeline.database import create_pool, close_pool
from pipeline.search import HybridSearchEngine

# Setup basic logging to reduce noise
logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("benchmark_cache")

ROOT = Path(__file__).resolve().parents[1]
OLD_REPORT = ROOT / "reports" / "no_llm_warm_20_query_latency.json"
REPORT_MD = ROOT / "reports" / "cache_benchmark.md"
REPORT_JSON = ROOT / "reports" / "cache_benchmark.json"


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _filters_from_old(row: dict[str, Any]) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if row.get("must_skills"):
        filters["skills"] = row["must_skills"]
    if row.get("country"):
        filters["country"] = row["country"]
    if row.get("city"):
        filters["city"] = row["city"]
    return filters


def _overlap_ratio(actual: list[str], expected: list[str], k: int) -> float:
    if not expected:
        return 1.0
    actual_set = set(actual[:k])
    expected_set = set(expected[:k])
    return len(actual_set & expected_set) / max(1, min(k, len(expected_set)))


def _avg_rank_displacement(actual: list[str], expected: list[str], k: int = 10) -> float:
    expected_pos = {candidate_id: idx for idx, candidate_id in enumerate(expected[:k])}
    displacements = [
        abs(idx - expected_pos[candidate_id])
        for idx, candidate_id in enumerate(actual[:k])
        if candidate_id in expected_pos
    ]
    if not displacements:
        return float(k)
    return statistics.mean(displacements)


async def run_scenario_batch(
    engine: HybridSearchEngine,
    queries: list[dict[str, Any]],
    baseline_results: list[list[str]],
    use_cache: bool,
) -> tuple[list[dict[str, Any]], int]:
    """Run a batch of queries, collecting timings and verifying correctness."""
    results = []
    mismatches = 0

    for idx, row in enumerate(queries):
        query = row["semantic_query"]
        filters = _filters_from_old(row)
        
        start_time = time.perf_counter()
        resp = await engine.smart_search(
            query=query,
            explicit_filters=filters or None,
            mode="no-llm",
            top_k=10,
            config_overrides={
                "use_cache": use_cache,
                "use_impression_logging": False,
            },
        )
        elapsed_ms = (time.perf_counter() - start_time) * 1000

        candidate_ids = [r.candidate_id for r in resp.results]
        
        # Correctness parity check
        is_correct = True
        top10_overlap = 1.0
        top3_overlap = 1.0
        rank_displacement = 0.0
        if baseline_results:
            expected_ids = baseline_results[idx]
            top10_overlap = _overlap_ratio(candidate_ids, expected_ids, 10)
            top3_overlap = _overlap_ratio(candidate_ids, expected_ids, 3)
            rank_displacement = _avg_rank_displacement(candidate_ids, expected_ids, 10)
            # Match top-k IDs
            if candidate_ids != expected_ids:
                is_correct = False
                mismatches += 1

        timing_summary = (resp.retrieval_policy or {}).get("timing_summary", {}) or {}
        
        results.append({
            "elapsed_ms": elapsed_ms,
            "correct": is_correct,
            "slowest_branch": timing_summary.get("slowest_branch"),
            "slowest_branch_ms": timing_summary.get("slowest_branch_elapsed_ms"),
            "largest_pool_wait_branch": timing_summary.get("largest_pool_wait_branch"),
            "largest_pool_wait_ms": timing_summary.get("largest_pool_wait_ms"),
            "largest_db_roundtrip_branch": timing_summary.get("largest_db_roundtrip_branch"),
            "largest_db_roundtrip_ms": timing_summary.get("largest_db_roundtrip_ms"),
            "top10_overlap": top10_overlap,
            "top3_overlap": top3_overlap,
            "avg_rank_displacement": rank_displacement,
        })

    return results, mismatches


def compute_metrics(run_results: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [r["elapsed_ms"] for r in run_results]
    
    # DB timing averages
    slowest_branch_elapsed = [r["slowest_branch_ms"] for r in run_results if r["slowest_branch_ms"] is not None]
    pool_waits = [r["largest_pool_wait_ms"] for r in run_results if r["largest_pool_wait_ms"] is not None]
    db_roundtrips = [r["largest_db_roundtrip_ms"] for r in run_results if r["largest_db_roundtrip_ms"] is not None]
    top10_overlaps = [r["top10_overlap"] for r in run_results]
    top3_overlaps = [r["top3_overlap"] for r in run_results]
    rank_displacements = [r["avg_rank_displacement"] for r in run_results]

    # Collect names of slowest branch, pool wait, db roundtrip
    slowest_branches = [r["slowest_branch"] for r in run_results if r["slowest_branch"]]
    pool_wait_branches = [r["largest_pool_wait_branch"] for r in run_results if r["largest_pool_wait_branch"]]
    db_roundtrip_branches = [r["largest_db_roundtrip_branch"] for r in run_results if r["largest_db_roundtrip_branch"]]

    most_common_slowest = max(set(slowest_branches), key=slowest_branches.count) if slowest_branches else "N/A"
    most_common_pool = max(set(pool_wait_branches), key=pool_wait_branches.count) if pool_wait_branches else "N/A"
    most_common_db = max(set(db_roundtrip_branches), key=db_roundtrip_branches.count) if db_roundtrip_branches else "N/A"

    return {
        "p50_ms": round(statistics.median(latencies), 2),
        "p95_ms": round(_percentile(latencies, 0.95), 2),
        "avg_ms": round(statistics.mean(latencies), 2),
        "slowest_branch": f"{most_common_slowest} ({round(statistics.mean(slowest_branch_elapsed), 2)}ms)" if slowest_branch_elapsed else "N/A",
        "avg_pool_wait_ms": round(statistics.mean(pool_waits), 2) if pool_waits else 0.0,
        "max_pool_wait_ms": round(max(pool_waits), 2) if pool_waits else 0.0,
        "avg_db_roundtrip_ms": round(statistics.mean(db_roundtrips), 2) if db_roundtrips else 0.0,
        "max_db_roundtrip_ms": round(max(db_roundtrips), 2) if db_roundtrips else 0.0,
        "top10_overlap": round(statistics.mean(top10_overlaps), 4) if top10_overlaps else 1.0,
        "top3_overlap": round(statistics.mean(top3_overlaps), 4) if top3_overlaps else 1.0,
        "avg_rank_displacement": round(statistics.mean(rank_displacements), 2) if rank_displacements else 0.0,
    }


def diff_stats(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    summary_before = before.get("summary", {})
    summary_after = after.get("summary", {})
    
    delta_hits = (summary_after.get("l1_hits", 0) + summary_after.get("l2_hits", 0)) - \
                 (summary_before.get("l1_hits", 0) + summary_before.get("l2_hits", 0))
    delta_misses = summary_after.get("misses", 0) - summary_before.get("misses", 0)
    delta_total = delta_hits + delta_misses
    hit_rate = round(delta_hits / delta_total, 4) if delta_total > 0 else 0.0

    # Namespace hit rates
    ns_rates = {}
    ns_before = before.get("namespaces", {})
    ns_after = after.get("namespaces", {})
    for ns in set(ns_before.keys()) | set(ns_after.keys()):
        b = ns_before.get(ns, {})
        a = ns_after.get(ns, {})
        
        h = (a.get("l1_hits", 0) + a.get("l2_hits", 0)) - (b.get("l1_hits", 0) + b.get("l2_hits", 0))
        m = a.get("misses", 0) - b.get("misses", 0)
        t = h + m
        ns_rates[ns] = round(h / t, 4) if t > 0 else 0.0

    return {
        "hit_rate": hit_rate,
        "namespaces": ns_rates,
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="Cache Performance Benchmark")
    parser.add_argument("--redis-url", type=str, default="redis://localhost:6379", help="Redis URL to use for benchmark")
    parser.add_argument(
        "--scenarios",
        default="memory-off,memory-on,redis-off,redis-on",
        help="Comma-separated scenario IDs: memory-off,memory-on,redis-off,redis-on",
    )
    parser.add_argument("--output-md", default=str(REPORT_MD), help="Markdown report output path")
    parser.add_argument("--output-json", default=str(REPORT_JSON), help="JSON report output path")
    args = parser.parse_args()

    # Load 20 queries from report
    if not OLD_REPORT.exists():
        print(f"Error: {OLD_REPORT} not found. Please run seed/benchmark scripts to populate it first.")
        return
    
    old_data = json.loads(OLD_REPORT.read_text())
    queries = list(old_data["rows"])
    print(f"Loaded {len(queries)} queries for benchmarking.")

    # Initialize PostgreSQL Pool
    pool = await create_pool()
    engine = HybridSearchEngine(pool)

    # 1. Establish baseline (no cache) & record candidate ID lists for correctness checks
    print("Running baseline (no cache) for result correctness validation...")
    settings.cache_backend = "memory"
    settings.search_use_retrieval_cache = False
    await _cache.start()
    _cache.clear_all()

    baseline_results = []
    for row in queries:
        resp = await engine.smart_search(
            query=row["semantic_query"],
            explicit_filters=_filters_from_old(row) or None,
            mode="no-llm",
            top_k=10,
            config_overrides={
                "use_cache": False,
                "use_impression_logging": False,
            },
        )
        baseline_results.append([r.candidate_id for r in resp.results])

    scenarios = [
        {"id": "memory-off", "name": "Memory, retrieval cache off", "backend": "memory", "retrieval_cache": False},
        {"id": "memory-on", "name": "Memory, retrieval cache on", "backend": "memory", "retrieval_cache": True},
        {"id": "redis-off", "name": "Redis, retrieval cache off", "backend": "redis", "retrieval_cache": False},
        {"id": "redis-on", "name": "Redis, retrieval cache on", "backend": "redis", "retrieval_cache": True},
    ]
    selected_scenarios = {item.strip() for item in args.scenarios.split(",") if item.strip()}
    scenarios = [scenario for scenario in scenarios if scenario["id"] in selected_scenarios]

    report_data = []

    for sc in scenarios:
        name = sc["name"]
        backend = sc["backend"]
        retrieval_cache = sc["retrieval_cache"]

        print(f"\nSetting up Scenario: {name} ...")
        
        # Configure dynamically
        settings.cache_backend = backend
        settings.redis_url = args.redis_url
        settings.search_use_retrieval_cache = retrieval_cache

        # Re-initialize the cache backend
        await _cache.start()

        if backend == "redis" and not _cache._redis_enabled:
            print(f"  [Skipped] Redis is not available/running. Skipping scenario: {name}")
            continue

        # Isolate namespace & clear cache to ensure completely cold start
        timestamp = int(time.time())
        ns_name = f"bench-{backend}-ret-{str(retrieval_cache).lower()}-{timestamp}"
        _cache._namespace_override = ns_name
        _cache.clear_all()

        # Run COLD
        print("  Running Cold batch...")
        before_cold = _cache.cache_snapshot()
        cold_res, cold_mismatches = await run_scenario_batch(engine, queries, baseline_results, use_cache=True)
        after_cold = _cache.cache_snapshot()
        
        cold_metrics = compute_metrics(cold_res)
        cold_cache = diff_stats(before_cold, after_cold)

        # Run WARM (immediately after)
        print("  Running Warm batch...")
        before_warm = _cache.cache_snapshot()
        warm_res, warm_mismatches = await run_scenario_batch(engine, queries, baseline_results, use_cache=True)
        after_warm = _cache.cache_snapshot()

        warm_metrics = compute_metrics(warm_res)
        warm_cache = diff_stats(before_warm, after_warm)

        # Record findings
        report_data.append({
            "scenario": name,
            "cold": {
                "p50_ms": cold_metrics["p50_ms"],
                "p95_ms": cold_metrics["p95_ms"],
                "avg_ms": cold_metrics["avg_ms"],
                "slowest_branch": cold_metrics["slowest_branch"],
                "avg_pool_wait_ms": cold_metrics["avg_pool_wait_ms"],
                "avg_db_roundtrip_ms": cold_metrics["avg_db_roundtrip_ms"],
                "hit_rate": cold_cache["hit_rate"],
                "namespaces": cold_cache["namespaces"],
                "mismatches": cold_mismatches,
                "top10_overlap": cold_metrics["top10_overlap"],
                "top3_overlap": cold_metrics["top3_overlap"],
                "avg_rank_displacement": cold_metrics["avg_rank_displacement"],
            },
            "warm": {
                "p50_ms": warm_metrics["p50_ms"],
                "p95_ms": warm_metrics["p95_ms"],
                "avg_ms": warm_metrics["avg_ms"],
                "slowest_branch": warm_metrics["slowest_branch"],
                "avg_pool_wait_ms": warm_metrics["avg_pool_wait_ms"],
                "avg_db_roundtrip_ms": warm_metrics["avg_db_roundtrip_ms"],
                "hit_rate": warm_cache["hit_rate"],
                "namespaces": warm_cache["namespaces"],
                "mismatches": warm_mismatches,
                "top10_overlap": warm_metrics["top10_overlap"],
                "top3_overlap": warm_metrics["top3_overlap"],
                "avg_rank_displacement": warm_metrics["avg_rank_displacement"],
            }
        })

    # Close PostgreSQL connection pool
    await close_pool()

    # Generate report markdown
    md_lines = [
        "# Cache Performance Benchmark Report",
        f"Generated at: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Comparative Scenarios Matrix",
        "",
        "| Scenario | Run | p50 | p95 | Avg Latency | Overall Hit Rate | Slowest Branch | Avg Pool Wait | Avg DB RTT | Top-10 Overlap | Top-3 Overlap | Avg Rank Shift | Exact Mismatches |",
        "| :--- | :--- | :---: | :---: | :---: | :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: |"
    ]

    for item in report_data:
        s_name = item["scenario"]
        c = item["cold"]
        w = item["warm"]

        md_lines.append(
            f"| **{s_name}** | Cold | {c['p50_ms']}ms | {c['p95_ms']}ms | {c['avg_ms']}ms | {c['hit_rate'] * 100:.1f}% | {c['slowest_branch']} | {c['avg_pool_wait_ms']}ms | {c['avg_db_roundtrip_ms']}ms | {c['top10_overlap'] * 100:.1f}% | {c['top3_overlap'] * 100:.1f}% | {c['avg_rank_displacement']} | {c['mismatches']}/20 |"
        )
        md_lines.append(
            f"| | Warm | {w['p50_ms']}ms | {w['p95_ms']}ms | {w['avg_ms']}ms | {w['hit_rate'] * 100:.1f}% | {w['slowest_branch']} | {w['avg_pool_wait_ms']}ms | {w['avg_db_roundtrip_ms']}ms | {w['top10_overlap'] * 100:.1f}% | {w['top3_overlap'] * 100:.1f}% | {w['avg_rank_displacement']} | {w['mismatches']}/20 |"
        )

    md_lines.extend([
        "",
        "## Cache Namespaces Breakdown (Warm Runs)",
        "",
        "| Scenario | Embed | Dense | BM25 | Skill | Chunk | Count |",
        "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |"
    ])

    for item in report_data:
        s_name = item["scenario"]
        w_ns = item["warm"]["namespaces"]
        
        def ns_pct(key: str) -> str:
            val = w_ns.get(key)
            return f"{val * 100:.1f}%" if val is not None else "-"

        md_lines.append(
            f"| **{s_name}** | {ns_pct('embed')} | {ns_pct('dense')} | {ns_pct('bm25')} | {ns_pct('skill')} | {ns_pct('chunk')} | {ns_pct('count')} |"
        )

    md_lines.extend([
        "",
        "## Correctness & Parity Verification Guard",
        "All scenarios verify exact top-10 candidate list parity against a cache-disabled baseline run. ",
        "Parity mismatch counts represent how many of the 20 test queries did not return the exact same ranking results under that cache setting.",
        ""
    ])

    # Write report files
    report_md = Path(args.output_md)
    report_json = Path(args.output_json)
    report_md.parent.mkdir(parents=True, exist_ok=True)
    report_json.parent.mkdir(parents=True, exist_ok=True)
    report_md.write_text("\n".join(md_lines) + "\n")
    report_json.write_text(json.dumps(report_data, indent=2) + "\n")

    print(f"\nCache benchmark report successfully generated at:\n  - {report_md}\n  - {report_json}")


if __name__ == "__main__":
    asyncio.run(main())
