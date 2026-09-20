"""
Evaluate ranking quality against curated cases and recruiter judgments.

Usage:
    venv/bin/python scripts/eval_ranking_quality.py
    venv/bin/python scripts/eval_ranking_quality.py --mode no-llm --limit 30

Optional curated cases:
    data/eval_cases.jsonl

Each JSONL line should look like:
    {
      "id": "case-1",
      "query": "senior python backend engineer",
      "filters": {"country": "india", "skills": ["python", "fastapi"]},
      "relevant_candidate_ids": ["..."],
      "mode": "no-llm"
    }
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pipeline.database import close_pool, get_pool
from pipeline.embedder import get_embedder
from pipeline.search import HybridSearchEngine


ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data" / "eval_cases.jsonl"
REPORT_DIR = ROOT / "reports"
LATEST_JSON = REPORT_DIR / "ranking_quality_latest.json"
LATEST_MD = REPORT_DIR / "ranking_quality_latest.md"
HISTORY_JSONL = REPORT_DIR / "ranking_quality_history.jsonl"


@dataclass
class EvalCase:
    id: str
    query: str
    filters: dict[str, Any] = field(default_factory=dict)
    relevant_candidate_ids: list[str] = field(default_factory=list)
    source: str = "curated"
    mode: str = "no-llm"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate ranking quality against labeled relevance.")
    parser.add_argument("--mode", default="no-llm", choices=["no-llm", "fast", "quality", "agent-quality"])
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--limit", type=int, default=50, help="Max DB-derived cases to use.")
    parser.add_argument("--history-window-minutes", type=int, default=15)
    return parser.parse_args()


def _load_curated_cases() -> list[EvalCase]:
    if not DATA_PATH.exists():
        return []
    cases: list[EvalCase] = []
    for idx, line in enumerate(DATA_PATH.read_text().splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        payload = json.loads(line)
        cases.append(EvalCase(
            id=str(payload.get("id") or f"curated-{idx}"),
            query=str(payload.get("query") or "").strip(),
            filters=dict(payload.get("filters") or {}),
            relevant_candidate_ids=[str(v) for v in payload.get("relevant_candidate_ids") or [] if v],
            source="curated",
            mode=str(payload.get("mode") or "no-llm"),
        ))
    return [case for case in cases if case.query and case.relevant_candidate_ids]


async def _load_db_cases(limit: int, history_window_minutes: int) -> list[EvalCase]:
    pool = await get_pool()
    judgment_rows = await pool.fetch(
        """
        SELECT
            i.search_id::text AS search_id,
            i.recruiter_id::text AS recruiter_id,
            i.candidate_id::text AS candidate_id,
            i.position,
            i.shown_at,
            r.relevant
        FROM relevance_judgments r
        JOIN search_impressions i ON i.id = r.impression_id
        WHERE i.search_id IS NOT NULL
        ORDER BY i.shown_at DESC
        """
    )
    if not judgment_rows:
        return []

    grouped: dict[str, dict[str, Any]] = {}
    recruiter_ids: set[str] = set()
    for row in judgment_rows:
        search_id = str(row["search_id"])
        entry = grouped.setdefault(search_id, {
            "recruiter_id": str(row["recruiter_id"] or ""),
            "shown_at": row["shown_at"],
            "relevant_ids": set(),
            "seen_ids": set(),
        })
        candidate_id = str(row["candidate_id"])
        entry["seen_ids"].add(candidate_id)
        if row["relevant"] is True:
            entry["relevant_ids"].add(candidate_id)
        recruiter_id = entry["recruiter_id"]
        if recruiter_id:
            recruiter_ids.add(recruiter_id)

    if not recruiter_ids:
        return []

    history_rows = await pool.fetch(
        """
        SELECT recruiter_id, query, filters_json, results_json, timestamp
        FROM search_history
        WHERE recruiter_id = ANY($1::text[])
        ORDER BY timestamp DESC
        """,
        list(recruiter_ids),
    )
    history_by_recruiter: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in history_rows:
        history_by_recruiter[str(row["recruiter_id"])].append({
            "query": str(row["query"] or "").strip(),
            "filters": row["filters_json"] or {},
            "results": row["results_json"] or [],
            "timestamp": row["timestamp"],
        })

    window = timedelta(minutes=history_window_minutes)
    cases: list[EvalCase] = []
    for search_id, payload in grouped.items():
        if not payload["relevant_ids"]:
            continue
        recruiter_id = payload["recruiter_id"]
        shown_at = payload["shown_at"]
        candidates = payload["seen_ids"]
        best = None
        for history in history_by_recruiter.get(recruiter_id, []):
            query = str(history["query"] or "").strip()
            if not query:
                continue
            delta = abs(history["timestamp"] - shown_at)
            if delta > window:
                continue
            result_ids = {
                str(item.get("id"))
                for item in history["results"]
                if isinstance(item, dict) and item.get("id")
            }
            overlap = len(result_ids & candidates)
            if overlap <= 0:
                continue
            key = (overlap, -delta.total_seconds())
            if best is None or key > best[0]:
                best = (key, history)
        if best is None:
            continue
        history = best[1]
        cases.append(EvalCase(
            id=f"judgment:{search_id}",
            query=str(history["query"]),
            filters=dict(history["filters"] or {}),
            relevant_candidate_ids=sorted(payload["relevant_ids"]),
            source="judgment",
        ))
        if len(cases) >= limit:
            break
    return cases


def _dcg(relevances: list[int]) -> float:
    return sum(rel / math.log2(idx + 2) for idx, rel in enumerate(relevances))


def _ndcg_at_k(ranked_ids: list[str], relevant_ids: set[str], k: int) -> float:
    rels = [1 if candidate_id in relevant_ids else 0 for candidate_id in ranked_ids[:k]]
    ideal = sorted(rels, reverse=True)
    actual = _dcg(rels)
    best = _dcg(ideal)
    return actual / best if best > 0 else 0.0


def _mrr_at_k(ranked_ids: list[str], relevant_ids: set[str], k: int) -> float:
    for idx, candidate_id in enumerate(ranked_ids[:k], start=1):
        if candidate_id in relevant_ids:
            return 1.0 / idx
    return 0.0


def _hit_at_k(ranked_ids: list[str], relevant_ids: set[str], k: int) -> float:
    return 1.0 if any(candidate_id in relevant_ids for candidate_id in ranked_ids[:k]) else 0.0


async def _evaluate_case(
    engine: HybridSearchEngine,
    case: EvalCase,
    *,
    top_k: int,
    default_mode: str,
) -> dict[str, Any]:
    resp = await engine.smart_search(
        query=case.query,
        explicit_filters=case.filters,
        mode=case.mode or default_mode,
        top_k=top_k,
    )
    ranked_ids = list(resp.candidate_ids or [result.candidate_id for result in resp.results])
    relevant_ids = set(case.relevant_candidate_ids)
    return {
        "id": case.id,
        "query": case.query,
        "source": case.source,
        "mode": case.mode or default_mode,
        "relevant_candidate_ids": sorted(relevant_ids),
        "ranked_candidate_ids": ranked_ids[:top_k],
        "ndcg@10": round(_ndcg_at_k(ranked_ids, relevant_ids, min(10, top_k)), 4),
        "mrr@10": round(_mrr_at_k(ranked_ids, relevant_ids, min(10, top_k)), 4),
        "hit@3": round(_hit_at_k(ranked_ids, relevant_ids, min(3, top_k)), 4),
        "hit@10": round(_hit_at_k(ranked_ids, relevant_ids, min(10, top_k)), 4),
    }


def _summarize(results: list[dict[str, Any]], *, top_k: int) -> dict[str, Any]:
    count = max(len(results), 1)
    return {
        "case_count": len(results),
        "ndcg@10": round(sum(item["ndcg@10"] for item in results) / count, 4),
        "mrr@10": round(sum(item["mrr@10"] for item in results) / count, 4),
        "hit@3": round(sum(item["hit@3"] for item in results) / count, 4),
        "hit@10": round(sum(item["hit@10"] for item in results) / count, 4),
        "top_k": top_k,
    }


def _markdown_report(summary: dict[str, Any], results: list[dict[str, Any]]) -> str:
    lines = [
        "# Ranking Quality Report",
        "",
        f"- Cases: {summary['case_count']}",
        f"- nDCG@10: {summary['ndcg@10']}",
        f"- MRR@10: {summary['mrr@10']}",
        f"- Hit@3: {summary['hit@3']}",
        f"- Hit@10: {summary['hit@10']}",
        "",
        "| Case | Source | nDCG@10 | MRR@10 | Hit@3 | Hit@10 | Query |",
        "| --- | --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in results:
        lines.append(
            f"| {item['id']} | {item['source']} | {item['ndcg@10']} | {item['mrr@10']} | "
            f"{item['hit@3']} | {item['hit@10']} | {item['query']} |"
        )
    return "\n".join(lines) + "\n"


async def main() -> None:
    args = _parse_args()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)

    curated = _load_curated_cases()
    db_cases = await _load_db_cases(args.limit, args.history_window_minutes)
    cases = curated + [case for case in db_cases if case.id not in {c.id for c in curated}]
    if not cases:
        raise SystemExit(
            "No eval cases found. Add data/eval_cases.jsonl or collect recruiter relevance judgments first."
        )

    pool = await get_pool()
    engine = HybridSearchEngine(pool=pool, embedder=get_embedder())
    try:
        results = []
        for case in cases:
            results.append(await _evaluate_case(engine, case, top_k=args.top_k, default_mode=args.mode))
    finally:
        await close_pool()

    summary = _summarize(results, top_k=args.top_k)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "cases": results,
    }
    LATEST_JSON.write_text(json.dumps(payload, indent=2))
    LATEST_MD.write_text(_markdown_report(summary, results))
    with HISTORY_JSONL.open("a") as handle:
        handle.write(json.dumps({
            "generated_at": payload["generated_at"],
            **summary,
        }) + "\n")

    print(_markdown_report(summary, results))


if __name__ == "__main__":
    asyncio.run(main())
