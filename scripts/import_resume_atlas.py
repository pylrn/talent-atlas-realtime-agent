"""Transform ahmedheakl/resume-atlas into this app's candidate/document JSONL.

Default behavior is transform-only. Use --load-db to append/import into
Postgres through the existing ingestion path.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Iterable

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.resume_corpus_import import transform_source_rows, write_jsonl_outputs
from scripts.import_recruitment_dataset import load_transformed_rows


DATASET_URL = "https://huggingface.co/datasets/ahmedheakl/resume-atlas/resolve/main/train.csv"
DEFAULT_OUTPUT_DIR = Path("data/resume_corpus")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5000, help="Maximum transformed rows")
    parser.add_argument("--offset", type=int, default=0, help="Source rows to skip")
    parser.add_argument(
        "--max-per-role",
        type=int,
        default=1000,
        help="Optional cap per canonical role; use 0 to disable",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--dataset-url", default=DATASET_URL)
    parser.add_argument("--load-db", action="store_true", help="Append transformed rows to Postgres")
    return parser.parse_args()


def download_csv(dataset_url: str) -> str:
    request = urllib.request.Request(dataset_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=180) as response:
        return response.read().decode("utf-8-sig", errors="replace")


def iter_csv_rows(csv_text: str) -> Iterable[dict[str, str]]:
    yield from csv.DictReader(io.StringIO(csv_text))


async def main() -> None:
    args = parse_args()
    print("Downloading ahmedheakl/resume-atlas...")
    csv_text = download_csv(args.dataset_url)
    print(f"Downloaded {len(csv_text):,} characters.")

    max_per_role = None if args.max_per_role <= 0 else args.max_per_role
    print(
        f"Transforming source=atlas offset={args.offset}, "
        f"limit={args.limit}, max_per_role={max_per_role}..."
    )
    transformed = transform_source_rows(
        "atlas",
        iter_csv_rows(csv_text),
        limit=args.limit,
        offset=args.offset,
        max_per_role=max_per_role,
    )
    records_path, eval_path = write_jsonl_outputs(
        transformed,
        args.output_dir,
        source="atlas",
    )
    print(f"Wrote transformed records: {records_path}")
    print(f"Wrote evaluation cases:    {eval_path}")

    if transformed:
        preview = transformed[0]
        print("\nPreview candidate:")
        print(json.dumps(preview.candidate, indent=2, ensure_ascii=False))
        print("\nPreview document:")
        print(json.dumps({
            **preview.document,
            "raw_text": preview.document["raw_text"][:800],
        }, indent=2, ensure_ascii=False))

    if args.load_db:
        print("\nLoading transformed records into Postgres...")
        summary = await load_transformed_rows(transformed)
        print(json.dumps(summary, indent=2))
    else:
        print("\nDatabase load skipped. Re-run with --load-db to import into Postgres.")


if __name__ == "__main__":
    asyncio.run(main())
