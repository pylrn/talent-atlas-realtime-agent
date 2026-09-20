#!/usr/bin/env python3
"""Deterministic acceptance-gate smoke evaluation for SignalRAG."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.live_rag import INJECTION_CUES, decide_retrieval, decompose_query, extract_hard_filters


DEFAULT_CASES = ROOT / "data" / "live_rag_eval_cases.jsonl"
DEFAULT_OUTPUT = ROOT / "reports" / "live_rag_controller_eval.json"


def load_cases(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def evaluate(cases: list[dict]) -> dict:
    rows: list[dict] = []
    for case in cases:
        category = case["category"]
        passed = False
        observed: object
        if category == "early_retrieval":
            decisions = [decide_retrieval(text, is_final=index == len(case["partials"]) - 1, has_prior_answer=False)[0]
                         for index, text in enumerate(case["partials"])]
            first = next((index for index, decision in enumerate(decisions) if decision == "RETRIEVE"), None)
            observed = {"decisions": decisions, "first_retrieve_index": first}
            passed = first is not None and first <= case["expected_first_retrieve_at_or_before"]
        elif category == "compound":
            subqueries = decompose_query(case["query"])
            observed = {"subqueries": subqueries, "count": len(subqueries)}
            passed = len(subqueries) >= case["min_subqueries"]
        elif category == "constraints":
            filters = extract_hard_filters(case["query"])
            observed = filters
            passed = filters == case["expected"]
        elif category == "suppression":
            decision = decide_retrieval(case["query"], is_final=True, has_prior_answer=True)[0]
            observed = decision
            passed = decision == case["expected"]
        else:
            decision = decide_retrieval(case["query"], is_final=case.get("is_final", True), has_prior_answer=False)[0]
            flagged = bool(INJECTION_CUES.search(case["query"]))
            observed = {"decision": decision, "injection_flagged": flagged}
            passed = (
                decision == case.get("expected") if "expected" in case
                else flagged is case.get("expected_injection_flag")
            )
        rows.append({"id": case["id"], "category": category, "passed": passed, "observed": observed})

    category_scores = {}
    for category in sorted({row["category"] for row in rows}):
        subset = [row for row in rows if row["category"] == category]
        category_scores[category] = {
            "passed": sum(row["passed"] for row in subset),
            "total": len(subset),
            "rate": round(sum(row["passed"] for row in subset) / len(subset), 4),
        }
    return {
        "suite": "signalrag-controller-v1",
        "case_count": len(rows),
        "passed": sum(row["passed"] for row in rows),
        "pass_rate": round(sum(row["passed"] for row in rows) / len(rows), 4),
        "categories": category_scores,
        "rows": rows,
        "limitations": [
            "This suite evaluates deterministic controller behavior, not retrieval relevance.",
            "Groundedness and TTFT require the database-backed integration benchmark.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = evaluate(load_cases(args.cases))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("suite", "case_count", "passed", "pass_rate")}, indent=2))
    raise SystemExit(0 if report["pass_rate"] == 1.0 else 1)


if __name__ == "__main__":
    main()
