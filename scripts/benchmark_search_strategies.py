"""
End-to-end benchmark for candidate search strategies.

Compares semantic, RRF, reranked, and optional LLM-planned search paths on
quality, latency, stage timings, and estimated LLM cost.

Usage:
    HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 python3 scripts/benchmark_search_strategies.py
    python3 scripts/benchmark_search_strategies.py --strategies rrf,llm_rrf --imported-sample-size 20
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import math
import random
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.database import close_pool, get_pool
from pipeline.embedder import get_embedder
from pipeline.query_planner import LLMQueryPlanner, PlannedSearch, fallback_plan
from pipeline.reranker import Reranker, get_reranker
from pipeline.search import HybridSearchEngine, SearchFilters, SearchResult
from scripts.evaluate_rankers import QUERIES as MANUAL_QUERIES


REPORT_DIR = Path("reports")
JSON_OUT = REPORT_DIR / "search_strategy_benchmark.json"
HTML_OUT = REPORT_DIR / "search_strategy_benchmark.html"
IMPORTED_EVAL_PATH = Path("data/recruitment_dataset/eval_cases_5000.jsonl")


@dataclass(frozen=True)
class EvalCase:
    id: str
    source: str
    query: str
    expected_name: str
    expected_email: str | None
    expected_city: str | None
    expected_country: str | None
    filters: SearchFilters


@dataclass(frozen=True)
class Strategy:
    id: str
    name: str
    use_rrf: bool
    use_planner: bool = False
    use_reranker: bool = False


STRATEGIES = [
    Strategy("semantic", "Semantic only", use_rrf=False),
    Strategy("rrf", "RRF hybrid", use_rrf=True),
    Strategy("rrf_rerank", "RRF + rerank", use_rrf=True, use_reranker=True),
    Strategy("llm_rrf", "LLM planner + RRF", use_rrf=True, use_planner=True),
    Strategy(
        "llm_rrf_rerank",
        "LLM planner + RRF + rerank",
        use_rrf=True,
        use_planner=True,
        use_reranker=True,
    ),
]


def evaluate_rank(ranked: list[dict[str, Any]], expected_id: str) -> dict[str, Any]:
    rank: int | None = None
    for idx, candidate in enumerate(ranked, start=1):
        if candidate["candidate_id"] == expected_id:
            rank = idx
            break
    if rank is None:
        return {
            "rank": None,
            "top1": 0.0,
            "hit3": 0.0,
            "hit10": 0.0,
            "mrr10": 0.0,
            "ndcg10": 0.0,
        }
    return {
        "rank": rank,
        "top1": 1.0 if rank == 1 else 0.0,
        "hit3": 1.0 if rank <= 3 else 0.0,
        "hit10": 1.0 if rank <= 10 else 0.0,
        "mrr10": 1.0 / rank if rank <= 10 else 0.0,
        "ndcg10": 1.0 / math.log2(rank + 1) if rank <= 10 else 0.0,
    }


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


def summarize_strategy_runs(rows: list[dict[str, Any]], strategy_ids: list[str]) -> list[dict[str, Any]]:
    summary = []
    for strategy_id in strategy_ids:
        strategy_rows = [row["strategies"][strategy_id] for row in rows]
        ranks = [item["rank"] if item["rank"] is not None else 999 for item in strategy_rows]
        total_cost = sum(item["estimated_cost_usd"] for item in strategy_rows)
        count = len(strategy_rows) or 1
        summary.append({
            "strategy_id": strategy_id,
            "top1": round(sum(item["top1"] for item in strategy_rows) / count, 4),
            "hit3": round(sum(item["hit3"] for item in strategy_rows) / count, 4),
            "hit10": round(sum(item["hit10"] for item in strategy_rows) / count, 4),
            "mrr10": round(sum(item["mrr10"] for item in strategy_rows) / count, 4),
            "ndcg10": round(sum(item["ndcg10"] for item in strategy_rows) / count, 4),
            "avg_rank": round(sum(ranks) / count, 2),
            "median_rank": round(statistics.median(ranks), 2),
            "p50_total_ms": round(percentile([item["total_ms"] for item in strategy_rows], 50), 3),
            "p95_total_ms": round(percentile([item["total_ms"] for item in strategy_rows], 95), 3),
            "p99_total_ms": round(percentile([item["total_ms"] for item in strategy_rows], 99), 3),
            "p95_planning_ms": round(
                percentile([item["planning_ms"] for item in strategy_rows], 95), 3
            ),
            "p95_embedding_ms": round(
                percentile([item["embedding_ms"] for item in strategy_rows], 95), 3
            ),
            "p95_retrieval_ms": round(
                percentile([item["retrieval_ms"] for item in strategy_rows], 95), 3
            ),
            "p95_rerank_ms": round(
                percentile([item["rerank_ms"] for item in strategy_rows], 95), 3
            ),
            "llm_calls": sum(item["llm_calls"] for item in strategy_rows),
            "estimated_cost_usd": round(total_cost, 6),
            "estimated_cost_per_1k": round((total_cost / count) * 1000.0, 6),
            "planner_fallbacks": sum(
                1 for item in strategy_rows if item.get("planner_used_fallback", False)
            ),
        })
    return sorted(summary, key=lambda item: (-item["mrr10"], -item["top1"], item["p95_total_ms"]))


def compare_planned_filters(expected: SearchFilters, planned: SearchFilters) -> dict[str, Any]:
    checks: list[bool] = []
    for field in [
        "location",
        "country",
        "city",
        "skills",
        "min_years_exp",
        "max_years_exp",
        "min_salary",
        "max_salary",
    ]:
        expected_value = getattr(expected, field)
        if expected_value in (None, [], ""):
            continue
        planned_value = getattr(planned, field)
        if field == "skills":
            checks.append(set(expected_value or []) == set(planned_value or []))
        else:
            checks.append(expected_value == planned_value)
    correct = sum(1 for item in checks if item)
    total = len(checks)
    return {
        "fields": total,
        "correct": correct,
        "accuracy": (correct / total) if total else None,
    }


def load_manual_cases(limit: int | None = None) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for query in MANUAL_QUERIES[:limit]:
        cases.append(EvalCase(
            id=query.id,
            source="manual",
            query=query.query,
            expected_name=query.expected_name,
            expected_email=None,
            expected_city=query.expected_city,
            expected_country=query.expected_country,
            filters=SearchFilters(
                country=query.country,
                city=query.city,
                skills=query.skills or None,
                skills_match=query.skills_match,  # type: ignore[arg-type]
                min_years_exp=query.min_years_exp,
            ),
        ))
    return cases


def load_imported_cases(path: Path, sample_size: int, seed: int) -> list[EvalCase]:
    if sample_size <= 0 or not path.exists():
        return []
    candidates: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            item = json.loads(line)
            if item.get("is_best_match") is True:
                candidates.append(item)
    rng = random.Random(seed)
    sample = rng.sample(candidates, min(sample_size, len(candidates)))
    return [
        EvalCase(
            id=f"imported-{item['row_number']}",
            source="imported",
            query=item["query"],
            expected_name=item["expected_candidate_name"],
            expected_email=item["expected_candidate_email"],
            expected_city=None,
            expected_country=None,
            filters=SearchFilters(),
        )
        for item in sample
    ]


async def resolve_expected_id(pool: Any, case: EvalCase) -> str | None:
    if case.expected_email:
        row = await pool.fetchrow("SELECT id FROM candidates WHERE email = $1", case.expected_email)
    else:
        row = await pool.fetchrow(
            """
            SELECT id
            FROM candidates
            WHERE full_name = $1
              AND city = $2
              AND country = $3
              AND status = 'active'
            """,
            case.expected_name,
            case.expected_city,
            case.expected_country,
        )
    return str(row["id"]) if row else None


async def run_strategy(
    engine: HybridSearchEngine,
    case: EvalCase,
    strategy: Strategy,
    expected_id: str,
    planner: LLMQueryPlanner,
    reranker: Reranker | None,
    top_k: int,
    rerank_pool_size: int,
    max_chunks_per_candidate: int,
) -> dict[str, Any]:
    total_start = time.perf_counter()
    planned = fallback_plan(case.query, case.filters)

    planning_ms = 0.0
    if strategy.use_planner:
        planning_start = time.perf_counter()
        planned = await planner.plan(case.query, case.filters)
        planning_ms = (time.perf_counter() - planning_start) * 1000.0

    query_text = planned.query
    filters = planned.filters
    fetch_limit = max(rerank_pool_size, top_k) * max_chunks_per_candidate * 2

    embedding_start = time.perf_counter()
    query_embedding = (await engine.embedder.embed([query_text]))[0]
    embedding_ms = (time.perf_counter() - embedding_start) * 1000.0

    retrieval_start = time.perf_counter()
    if strategy.use_rrf:
        rows = await engine._search_rrf(query_text, query_embedding, filters, fetch_limit)
    else:
        rows = await engine._search_semantic(query_embedding, filters, fetch_limit)
    retrieval_ms = (time.perf_counter() - retrieval_start) * 1000.0

    candidates = engine._deduplicate(
        rows,
        max_chunks=max_chunks_per_candidate,
        query_text=query_text,
    )

    rerank_ms = 0.0
    if strategy.use_reranker and reranker and candidates:
        rerank_start = time.perf_counter()
        candidates = await reranker.rerank(query_text, candidates[:rerank_pool_size])
        rerank_ms = (time.perf_counter() - rerank_start) * 1000.0

    ranked = [_result_to_ranked_item(result) for result in candidates[:top_k]]
    metrics = evaluate_rank(ranked, expected_id)
    total_ms = (time.perf_counter() - total_start) * 1000.0
    filter_comparison = (
        compare_planned_filters(case.filters, filters)
        if strategy.use_planner
        else {"fields": 0, "correct": 0, "accuracy": None}
    )

    top = ranked[0] if ranked else None
    return {
        **metrics,
        "total_ms": round(total_ms, 3),
        "planning_ms": round(planning_ms, 3),
        "embedding_ms": round(embedding_ms, 3),
        "retrieval_ms": round(retrieval_ms, 3),
        "rerank_ms": round(rerank_ms, 3),
        "llm_calls": planned.usage.llm_calls,
        "input_tokens": planned.usage.input_tokens,
        "output_tokens": planned.usage.output_tokens,
        "estimated_cost_usd": round(planned.usage.estimated_cost_usd, 8),
        "planner_used_fallback": planned.used_fallback if strategy.use_planner else False,
        "planner_confidence": round(planned.confidence, 4),
        "planner_reasoning": planned.reasoning,
        "planner_filter_comparison": filter_comparison,
        "planned_query": query_text,
        "planned_filters": filters_to_dict(filters),
        "result_count": len(ranked),
        "top_candidate": top["full_name"] if top else None,
        "top_candidate_id": top["candidate_id"] if top else None,
        "top_score": top["score"] if top else 0.0,
    }


def _result_to_ranked_item(result: SearchResult) -> dict[str, Any]:
    return {
        "candidate_id": result.candidate_id,
        "full_name": result.full_name,
        "city": result.city,
        "country": result.country,
        "skills": result.skills,
        "score": result.rerank_score or result.rank_score or result.similarity_score,
    }


def filters_to_dict(filters: SearchFilters) -> dict[str, Any]:
    return {
        "location": filters.location,
        "country": filters.country,
        "city": filters.city,
        "skills": filters.skills or [],
        "skills_match": filters.skills_match,
        "min_years_exp": filters.min_years_exp,
        "max_years_exp": filters.max_years_exp,
        "min_salary": filters.min_salary,
        "max_salary": filters.max_salary,
    }


async def run_benchmark(args: argparse.Namespace) -> dict[str, Any]:
    selected_strategies = [
        strategy for strategy in STRATEGIES if strategy.id in set(args.strategies.split(","))
    ]
    if not selected_strategies:
        raise ValueError("No valid strategies selected")

    cases = load_manual_cases(args.manual_limit)
    cases.extend(load_imported_cases(args.imported_eval_path, args.imported_sample_size, args.seed))
    if args.max_cases:
        cases = cases[:args.max_cases]

    pool = await get_pool()
    try:
        embedder = get_embedder()
        engine = HybridSearchEngine(pool, embedder=embedder)
        planner = LLMQueryPlanner(
            provider=args.llm_provider,
            min_confidence=args.planner_min_confidence,
            input_cost_per_million=args.llm_input_cost_per_million,
            output_cost_per_million=args.llm_output_cost_per_million,
        )
        reranker = None
        if any(strategy.use_reranker for strategy in selected_strategies):
            reranker = get_reranker(args.reranker)

        query_rows: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for case in cases:
            expected_id = await resolve_expected_id(pool, case)
            if not expected_id:
                skipped.append({
                    "id": case.id,
                    "source": case.source,
                    "expected_candidate": case.expected_name,
                    "reason": "Expected candidate not found in database",
                })
                continue

            strategy_results = {}
            for strategy in selected_strategies:
                strategy_results[strategy.id] = await run_strategy(
                    engine=engine,
                    case=case,
                    strategy=strategy,
                    expected_id=expected_id,
                    planner=planner,
                    reranker=reranker,
                    top_k=args.top_k,
                    rerank_pool_size=args.rerank_pool_size,
                    max_chunks_per_candidate=args.max_chunks_per_candidate,
                )

            query_rows.append({
                "id": case.id,
                "source": case.source,
                "query": case.query,
                "expected_candidate_id": expected_id,
                "expected_candidate": case.expected_name,
                "base_filters": filters_to_dict(case.filters),
                "strategies": strategy_results,
            })
    finally:
        await close_pool()

    strategy_ids = [strategy.id for strategy in selected_strategies]
    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dataset": {
            "manual_cases": sum(1 for case in cases if case.source == "manual"),
            "imported_cases": sum(1 for case in cases if case.source == "imported"),
            "evaluated_cases": len(query_rows),
            "skipped_cases": len(skipped),
        },
        "settings": {
            "top_k": args.top_k,
            "rerank_pool_size": args.rerank_pool_size,
            "max_chunks_per_candidate": args.max_chunks_per_candidate,
            "llm_provider": args.llm_provider,
            "planner_min_confidence": args.planner_min_confidence,
        },
        "strategies": [{"id": s.id, "name": s.name} for s in selected_strategies],
        "summary": summarize_strategy_runs(query_rows, strategy_ids) if query_rows else [],
        "planner_accuracy": summarize_planner_accuracy(query_rows, strategy_ids),
        "queries": query_rows,
        "skipped": skipped,
    }
    return report


def summarize_planner_accuracy(
    query_rows: list[dict[str, Any]],
    strategy_ids: list[str],
) -> list[dict[str, Any]]:
    summary = []
    for strategy_id in strategy_ids:
        comparisons = [
            row["strategies"][strategy_id]["planner_filter_comparison"]
            for row in query_rows
            if row["strategies"][strategy_id]["planner_filter_comparison"]["fields"]
        ]
        fields = sum(item["fields"] for item in comparisons)
        correct = sum(item["correct"] for item in comparisons)
        summary.append({
            "strategy_id": strategy_id,
            "fields": fields,
            "correct": correct,
            "accuracy": round(correct / fields, 4) if fields else None,
        })
    return summary


def write_reports(report: dict[str, Any], json_path: Path, html_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    html_path.write_text(render_html(report), encoding="utf-8")


def render_html(report: dict[str, Any]) -> str:
    rows = "\n".join(
        "<tr>"
        f"<td><code>{html.escape(item['strategy_id'])}</code></td>"
        f"<td>{item['top1']:.3f}</td>"
        f"<td>{item['hit3']:.3f}</td>"
        f"<td>{item['hit10']:.3f}</td>"
        f"<td>{item['mrr10']:.3f}</td>"
        f"<td>{item['ndcg10']:.3f}</td>"
        f"<td>{item['p95_total_ms']:.1f} ms</td>"
        f"<td>{item['p95_planning_ms']:.1f} ms</td>"
        f"<td>{item['p95_rerank_ms']:.1f} ms</td>"
        f"<td>${item['estimated_cost_per_1k']:.4f}</td>"
        f"<td>{item['planner_fallbacks']}</td>"
        "</tr>"
        for item in report["summary"]
    )
    query_rows = "\n".join(render_query_row(row, report["strategies"]) for row in report["queries"])
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Search Strategy Benchmark</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #17202a; }}
    table {{ border-collapse: collapse; width: 100%; margin: 18px 0; }}
    th, td {{ border: 1px solid #d8dee4; padding: 8px; vertical-align: top; }}
    th {{ background: #f6f8fa; text-align: left; }}
    code {{ background: #f6f8fa; padding: 2px 4px; border-radius: 4px; }}
    .good {{ color: #116329; font-weight: 700; }}
    .miss {{ color: #8a1f11; font-weight: 700; }}
    .muted {{ color: #687076; }}
  </style>
</head>
<body>
  <h1>Search Strategy Benchmark</h1>
  <p class="muted">Generated {html.escape(report["generated_at"])} over {report["dataset"]["evaluated_cases"]} evaluated cases.</p>
  <h2>Summary</h2>
  <table>
    <thead><tr><th>Strategy</th><th>Top-1</th><th>Hit@3</th><th>Hit@10</th><th>MRR@10</th><th>nDCG@10</th><th>p95 total</th><th>p95 plan</th><th>p95 rerank</th><th>Cost / 1K</th><th>Planner fallbacks</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <h2>Per Query</h2>
  <table>
    <thead><tr><th>Query</th><th>Expected</th>{''.join(f"<th>{html.escape(s['id'])}</th>" for s in report["strategies"])}</tr></thead>
    <tbody>{query_rows}</tbody>
  </table>
</body>
</html>"""


def render_query_row(row: dict[str, Any], strategies: list[dict[str, str]]) -> str:
    cells = []
    for strategy in strategies:
        result = row["strategies"][strategy["id"]]
        rank = result["rank"]
        cls = "good" if rank == 1 else "miss" if rank is None else ""
        label = "miss" if rank is None else str(rank)
        cells.append(
            f"<td class=\"{cls}\">{label}<br><small>{html.escape(str(result['top_candidate'] or ''))}</small></td>"
        )
    return (
        "<tr>"
        f"<td><strong>{html.escape(row['id'])}</strong><br>{html.escape(row['query'][:240])}</td>"
        f"<td>{html.escape(row['expected_candidate'])}</td>"
        f"{''.join(cells)}"
        "</tr>"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strategies",
        default="semantic,rrf,rrf_rerank,llm_rrf,llm_rrf_rerank",
        help="Comma-separated strategy ids to run.",
    )
    parser.add_argument("--manual-limit", type=int, default=None)
    parser.add_argument("--imported-sample-size", type=int, default=50)
    parser.add_argument("--imported-eval-path", type=Path, default=IMPORTED_EVAL_PATH)
    parser.add_argument("--seed", type=int, default=20260515)
    parser.add_argument("--max-cases", type=int, default=None)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--rerank-pool-size", type=int, default=30)
    parser.add_argument("--max-chunks-per-candidate", type=int, default=3)
    parser.add_argument("--reranker", default="local-fast")
    parser.add_argument("--llm-provider", choices=["openai", "gemini"], default="gemini")
    parser.add_argument("--planner-min-confidence", type=float, default=0.35)
    parser.add_argument("--llm-input-cost-per-million", type=float, default=None)
    parser.add_argument("--llm-output-cost-per-million", type=float, default=None)
    parser.add_argument("--json-out", type=Path, default=JSON_OUT)
    parser.add_argument("--html-out", type=Path, default=HTML_OUT)
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    report = await run_benchmark(args)
    write_reports(report, args.json_out, args.html_out)
    print(json.dumps({
        "json": str(args.json_out),
        "html": str(args.html_out),
        "dataset": report["dataset"],
        "summary": report["summary"],
        "planner_accuracy": report["planner_accuracy"],
    }, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
