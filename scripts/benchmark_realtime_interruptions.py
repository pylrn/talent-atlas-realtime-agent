"""Run the deterministic interruption benchmark and print the headline metrics.

Every number here is reproducible except the two wall-clock ones, because the
branch lifecycle runs on a virtual clock. That is what makes the cancellation
latency a measurement rather than a coincidence.

Usage:
    python scripts/benchmark_realtime_interruptions.py
    python scripts/benchmark_realtime_interruptions.py --output reports/realtime_benchmark.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.realtime_benchmark import InterruptionBenchmark

# The metrics the design actually claims, in the order a reader wants them:
# how fast it answers, what it cancelled, what it kept, what it refused to
# duplicate, and whether the closing number is grounded.
HEADLINE: tuple[tuple[str, str, str, str], ...] = (
    ("fast_path_acknowledgement", "first_ack_ms", "ms", "first acknowledgement latency"),
    ("speculative_retrieval_start", "calls_before_tool_call", "", "retrieval started before the tool call"),
    ("selective_cancellation", "invalidated_branches", "", "branches invalidated by a query rewrite"),
    ("selective_cancellation", "cancellation_latency_ms", "ms", "cancellation latency"),
    ("selective_cancellation", "work_avoided_ms", "ms", "work avoided by cancelling"),
    ("slot_retention", "changed_fields", "", "slots changed by a refinement"),
    ("duplicate_call_id", "duplicate_calls", "", "duplicate calls detected"),
    ("duplicate_call_id", "side_effect_log", "", "audit trail of the retried write"),
    ("superseded_write", "after", "", "shortlist after the correction"),
    ("tool_failure", "status_after_failure", "", "state after a tool failure"),
    ("burst_barge_in", "cancellations", "", "cancellations caused by three barge-ins"),
    ("dropped_transport", "close_ms", "ms", "time to close a dropped transport"),
    ("final_grounding", "candidate_count", "", "candidates in the closing snapshot"),
)


def _render(value: object, unit: str) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, list):
        return f"{len(value)}: {value}"
    return f"{value}{unit}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Optional path to write the full JSON report")
    parser.add_argument(
        "--budget-ms",
        type=float,
        default=250.0,
        help="Fast-path acknowledgement budget in milliseconds (default: 250)",
    )
    args = parser.parse_args()

    report = InterruptionBenchmark(acknowledgement_budget_ms=args.budget_ms).run()

    print("headline metrics")
    print("-" * 78)
    for scenario, metric, unit, label in HEADLINE:
        value = report["scenarios"][scenario]["metrics"].get(metric)
        print(f"  {label:<48} {_render(value, unit)}")

    print()
    print("scenarios")
    print("-" * 78)
    for name, scenario in report["scenarios"].items():
        print(f"  {'PASS' if scenario['passed'] else 'FAIL'}  {name}")
        if not scenario["passed"]:
            for check, ok in scenario["checks"].items():
                if not ok:
                    print(f"          failed check: {check}")

    totals = report["totals"]
    print()
    print(f"{totals['scenarios'] - totals['failed']}/{totals['scenarios']} scenarios passed")

    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path}")

    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
