"""Rerun the saved 20-query no-LLM benchmark against current retrieval policy.

Inputs:
  reports/no_llm_warm_20_query_latency.json

Outputs:
  reports/no_llm_policy_20_current.json
  reports/no_llm_policy_20_current.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from typing import Any

from pipeline import cache as _cache
from pipeline.database import create_pool
from pipeline.search import HybridSearchEngine
import pipeline.search as search_mod


ROOT = Path(__file__).resolve().parents[1]
OLD_REPORT = ROOT / "reports" / "no_llm_warm_20_query_latency.json"
JSON_OUT = ROOT / "reports" / "no_llm_policy_20_current.json"
MD_OUT = ROOT / "reports" / "no_llm_policy_20_current.md"


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


def _summary(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    values = [float(r[key]) for r in rows]
    return {
        "count": len(values),
        "min_ms": round(min(values), 2),
        "p50_ms": round(statistics.median(values), 2),
        "p75_ms": round(_percentile(values, 0.75), 2),
        "p90_ms": round(_percentile(values, 0.90), 2),
        "max_ms": round(max(values), 2),
        "avg_ms": round(statistics.mean(values), 2),
    }


def _filters_from_old(row: dict[str, Any]) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if row.get("must_skills"):
        filters["skills"] = row["must_skills"]
    if row.get("country"):
        filters["country"] = row["country"]
    if row.get("city"):
        filters["city"] = row["city"]
    return filters


async def _run_case(
    engine: HybridSearchEngine,
    row: dict[str, Any],
    *,
    branch_timings: dict[str, Any],
) -> dict[str, Any]:
    branch_timings.clear()
    query = row["semantic_query"]
    filters = _filters_from_old(row)
    start = time.perf_counter()
    resp = await engine.smart_search(
        query=query,
        explicit_filters=filters or None,
        mode="no-llm",
        top_k=10,
        config_overrides={
            "use_cache": False,
            "use_impression_logging": False,
        },
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    results = list(resp.results or [])
    scores = [float(getattr(r, "feature_score", 0.0) or 0.0) for r in results]
    top3 = [getattr(r, "full_name", "") for r in results[:3]]
    old_top3 = row.get("top3") or []
    overlap = len(set(top3) & set(old_top3))
    retrieval_paths: dict[str, int] = {}
    for result in results:
        for path in getattr(result, "retrieval_paths", []) or []:
            retrieval_paths[path] = retrieval_paths.get(path, 0) + 1

    keyword_policy = (resp.retrieval_policy or {}).get("keyword", {})
    return {
        "idx": row["idx"],
        "name": row["name"],
        "query": query,
        "filters": filters,
        "old_elapsed_ms": row["elapsed_ms"],
        "elapsed_ms": round(elapsed_ms, 2),
        "delta_ms": round(elapsed_ms - float(row["elapsed_ms"]), 2),
        "old_scanned": row.get("scanned"),
        "scanned": getattr(resp, "total_candidates_scanned", 0),
        "old_results": row.get("results"),
        "results": len(results),
        "phase_timings": dict(resp.phase_timings or {}),
        "branch_timings": dict(branch_timings),
        "keyword_policy": keyword_policy,
        "top_feature_score": round(max(scores), 2) if scores else 0.0,
        "avg_feature_score": round(statistics.mean(scores), 2) if scores else 0.0,
        "weak_result_count": sum(1 for s in scores if s < 55),
        "old_top3": old_top3,
        "top3": top3,
        "top3_overlap": overlap,
        "retrieval_paths": retrieval_paths,
    }


def _write_markdown(old: dict[str, Any], current: dict[str, Any]) -> None:
    rows = current["rows"]
    lines = [
        "# No-LLM 20 Query Benchmark After Keyword Policy",
        "",
        "## Summary",
        "",
        "| Metric | Before | After |",
        "| --- | ---: | ---: |",
    ]
    for key in ("avg_ms", "p50_ms", "p75_ms", "p90_ms", "max_ms"):
        lines.append(f"| {key} | {old['summary'][key]} | {current['summary'][key]} |")
    lines.extend([
        "",
        f"- Average top-3 overlap with the saved old run: {current['quality_summary']['avg_top3_overlap']}/3",
        f"- Average current feature score: {current['quality_summary']['avg_feature_score']}",
        f"- Current weak results (<55 score): {current['quality_summary']['weak_result_count']}",
        f"- Keyword actions: {current['keyword_action_counts']}",
        "",
        "## Rows",
        "",
        "| Query | Before ms | After ms | Δ ms | Keyword | Avg score | Top-3 overlap |",
        "| --- | ---: | ---: | ---: | --- | ---: | ---: |",
    ])
    for r in rows:
        kp = r["keyword_policy"]
        action = kp.get("action", "?")
        reason = kp.get("reason", "")
        lines.append(
            f"| {r['name']} | {r['old_elapsed_ms']:.2f} | {r['elapsed_ms']:.2f} | "
            f"{r['delta_ms']:.2f} | {action}:{reason} | "
            f"{r['avg_feature_score']:.2f} | {r['top3_overlap']}/3 |"
        )
    lines.extend([
        "",
        "## Notes",
        "",
        "- Quality is measured with proxies because this benchmark has no labeled relevant candidates.",
        "- Proxies used: result count, feature scores, weak-result count, retrieval paths, and top-3 overlap with the saved old run.",
        "- Timing is live against the current database and can move with DB cache warmth and network conditions.",
    ])
    MD_OUT.write_text("\n".join(lines) + "\n")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-warmup", action="store_true")
    args = parser.parse_args()

    old = json.loads(OLD_REPORT.read_text())
    old_rows = list(old["rows"])
    branch_timings: dict[str, Any] = {}

    orig_count = HybridSearchEngine._count_filtered
    orig_dense = search_mod.retrieve_dense
    orig_bm25 = search_mod.retrieve_bm25
    orig_skill = search_mod.retrieve_skill

    async def timed_count(self, *a, **kw):
        t = time.perf_counter()
        result = await orig_count(self, *a, **kw)
        branch_timings["count_ms"] = round((time.perf_counter() - t) * 1000, 2)
        branch_timings["count"] = result
        return result

    async def timed_dense(*a, **kw):
        t = time.perf_counter()
        result = await orig_dense(*a, **kw)
        branch_timings["dense_ms"] = round((time.perf_counter() - t) * 1000, 2)
        branch_timings["dense_rows"] = len(result)
        return result

    async def timed_bm25(*a, **kw):
        t = time.perf_counter()
        result = await orig_bm25(*a, **kw)
        branch_timings["keyword_ms"] = round((time.perf_counter() - t) * 1000, 2)
        branch_timings["keyword_rows"] = len(result)
        return result

    async def timed_skill(*a, **kw):
        t = time.perf_counter()
        result = await orig_skill(*a, **kw)
        branch_timings["skill_ms"] = round((time.perf_counter() - t) * 1000, 2)
        branch_timings["skill_rows"] = len(result)
        return result

    HybridSearchEngine._count_filtered = timed_count
    search_mod.retrieve_dense = timed_dense
    search_mod.retrieve_bm25 = timed_bm25
    search_mod.retrieve_skill = timed_skill

    pool = await create_pool()
    try:
        engine = HybridSearchEngine(pool)
        if not args.skip_warmup:
            for row in old_rows:
                await engine.smart_search(
                    query=row["semantic_query"],
                    explicit_filters=_filters_from_old(row) or None,
                    mode="no-llm",
                    top_k=3,
                    config_overrides={
                        "use_cache": False,
                        "use_impression_logging": False,
                    },
                )
            _cache.clear_all()

        rows: list[dict[str, Any]] = []
        for row in old_rows:
            current = await _run_case(engine, row, branch_timings=branch_timings)
            rows.append(current)
            kp = current["keyword_policy"]
            print(
                f"{current['idx']:02d} {current['name']:<28} "
                f"{current['elapsed_ms']:8.2f}ms "
                f"old={current['old_elapsed_ms']:8.2f}ms "
                f"keyword={kp.get('action')}:{kp.get('reason')} "
                f"score={current['avg_feature_score']:.1f} "
                f"overlap={current['top3_overlap']}/3"
            )

        action_counts: dict[str, int] = {}
        for row in rows:
            action = row["keyword_policy"].get("action", "unknown")
            action_counts[action] = action_counts.get(action, 0) + 1

        current = {
            "source_baseline": str(OLD_REPORT),
            "summary": _summary(rows, "elapsed_ms"),
            "old_summary": old["summary"],
            "keyword_action_counts": action_counts,
            "quality_summary": {
                "avg_top3_overlap": round(statistics.mean(r["top3_overlap"] for r in rows), 2),
                "avg_feature_score": round(statistics.mean(r["avg_feature_score"] for r in rows), 2),
                "weak_result_count": sum(r["weak_result_count"] for r in rows),
                "zero_result_queries": [r["name"] for r in rows if r["results"] == 0],
            },
            "rows": rows,
        }
        JSON_OUT.write_text(json.dumps(current, indent=2))
        _write_markdown(old, current)
        print("\nSUMMARY")
        print(json.dumps(current["summary"], indent=2))
        print("QUALITY")
        print(json.dumps(current["quality_summary"], indent=2))
        print(f"Wrote {JSON_OUT}")
        print(f"Wrote {MD_OUT}")
    finally:
        await pool.close()
        HybridSearchEngine._count_filtered = orig_count
        search_mod.retrieve_dense = orig_dense
        search_mod.retrieve_bm25 = orig_bm25
        search_mod.retrieve_skill = orig_skill


if __name__ == "__main__":
    asyncio.run(main())
