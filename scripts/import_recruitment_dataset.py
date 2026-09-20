"""
Download and convert the Kaggle recruitment dataset into this app's shape.

Default behavior is transform-only:
  - downloads/caches the public Kaggle zip
  - writes JSONL records with candidate/document/eval_case objects

Use --load-db to append/import the transformed candidates into Postgres and
ingest their resume documents.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import io
import json
import urllib.request
import zipfile
from pathlib import Path
from typing import Iterable

import asyncpg

from pipeline import settings
from pipeline.ingest import IngestionPipeline
from pipeline.recruitment_dataset import (
    DATASET_FILENAME,
    DATASET_URL,
    TransformedRecruitmentRow,
    transform_recruitment_row,
)


DEFAULT_OUTPUT_DIR = Path("data/recruitment_dataset")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=5000, help="Maximum rows to transform")
    parser.add_argument("--offset", type=int, default=0, help="Rows to skip before transforming")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--load-db", action="store_true", help="Append transformed rows to Postgres")
    parser.add_argument("--dataset-url", default=DATASET_URL)
    return parser.parse_args()


def download_dataset_zip(dataset_url: str) -> bytes:
    request = urllib.request.Request(dataset_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def iter_dataset_rows(zip_bytes: bytes) -> Iterable[dict[str, str]]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        if DATASET_FILENAME not in archive.namelist():
            raise RuntimeError(f"{DATASET_FILENAME} not found in dataset zip")
        with archive.open(DATASET_FILENAME) as csv_file:
            text_stream = io.TextIOWrapper(
                csv_file,
                encoding="utf-8-sig",
                errors="replace",
                newline="",
            )
            yield from csv.DictReader(text_stream)


def transform_rows(
    rows: Iterable[dict[str, str]],
    *,
    offset: int,
    limit: int,
) -> list[TransformedRecruitmentRow]:
    transformed: list[TransformedRecruitmentRow] = []
    for idx, row in enumerate(rows, start=1):
        if idx <= offset:
            continue
        transformed.append(transform_recruitment_row(row, row_number=idx))
        if len(transformed) >= limit:
            break
    return transformed


def write_jsonl_outputs(
    transformed: list[TransformedRecruitmentRow],
    output_dir: Path,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    records_path = output_dir / f"transformed_{len(transformed)}.jsonl"
    eval_path = output_dir / f"eval_cases_{len(transformed)}.jsonl"

    with records_path.open("w", encoding="utf-8") as records_file:
        for item in transformed:
            records_file.write(json.dumps({
                "candidate": item.candidate,
                "document": item.document,
                "eval_case": item.eval_case,
            }, ensure_ascii=False) + "\n")

    with eval_path.open("w", encoding="utf-8") as eval_file:
        for item in transformed:
            eval_file.write(json.dumps(item.eval_case, ensure_ascii=False) + "\n")

    return records_path, eval_path


async def load_transformed_rows(transformed: list[TransformedRecruitmentRow]) -> dict[str, int]:
    pool = await asyncpg.create_pool(dsn=settings.database_url, min_size=2, max_size=5)
    pipeline = IngestionPipeline(pool)
    inserted = 0
    updated = 0
    documents_ingested = 0
    duplicates = 0

    try:
        for item in transformed:
            candidate = item.candidate
            existing_id = await pool.fetchval(
                "SELECT id FROM candidates WHERE email = $1",
                candidate["email"],
            )
            if existing_id:
                candidate_id = str(existing_id)
                updated += 1
                await pool.execute(
                    """
                    UPDATE candidates
                    SET full_name = $2,
                        age = $3,
                        location = $4,
                        city = $5,
                        country = $6,
                        interests = $7,
                        skills = $8,
                        years_exp = $9,
                        salary_min = $10,
                        salary_max = $11,
                        status = $12
                    WHERE id = $1
                    """,
                    candidate_id,
                    candidate["full_name"],
                    candidate["age"],
                    candidate["location"],
                    candidate["city"],
                    candidate["country"],
                    candidate["interests"],
                    candidate["skills"],
                    candidate["years_exp"],
                    candidate["salary_min"],
                    candidate["salary_max"],
                    candidate["status"],
                )
            else:
                row = await pool.fetchrow(
                    """
                    INSERT INTO candidates (
                        full_name, email, age, location, city, country, interests,
                        skills, years_exp, salary_min, salary_max, status
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    RETURNING id
                    """,
                    candidate["full_name"],
                    candidate["email"],
                    candidate["age"],
                    candidate["location"],
                    candidate["city"],
                    candidate["country"],
                    candidate["interests"],
                    candidate["skills"],
                    candidate["years_exp"],
                    candidate["salary_min"],
                    candidate["salary_max"],
                    candidate["status"],
                )
                candidate_id = str(row["id"])
                inserted += 1

            result = await pipeline.ingest(
                candidate_id=candidate_id,
                doc_type=item.document["doc_type"],
                title=item.document["title"],
                raw_text=item.document["raw_text"],
                metadata=item.document["metadata"],
            )
            if result["status"] == "duplicate_skipped":
                duplicates += 1
            else:
                documents_ingested += 1
    finally:
        await pool.close()

    return {
        "candidates_inserted": inserted,
        "candidates_updated": updated,
        "documents_ingested": documents_ingested,
        "duplicate_documents_skipped": duplicates,
    }


async def main() -> None:
    args = parse_args()
    print("Downloading Kaggle recruitment dataset...")
    zip_bytes = download_dataset_zip(args.dataset_url)
    print(f"Downloaded {len(zip_bytes):,} bytes.")

    print(f"Transforming rows offset={args.offset}, limit={args.limit}...")
    transformed = transform_rows(iter_dataset_rows(zip_bytes), offset=args.offset, limit=args.limit)
    records_path, eval_path = write_jsonl_outputs(transformed, args.output_dir)
    print(f"Wrote transformed records: {records_path}")
    print(f"Wrote evaluation cases:    {eval_path}")

    if transformed:
        preview = transformed[0]
        print("\nPreview candidate:")
        print(json.dumps(preview.candidate, indent=2, ensure_ascii=False))
        print("\nPreview document:")
        print(json.dumps({
            **preview.document,
            "raw_text": preview.document["raw_text"][:600],
        }, indent=2, ensure_ascii=False))
        print("\nPreview eval case:")
        print(json.dumps(preview.eval_case, indent=2, ensure_ascii=False))

    if args.load_db:
        print("\nLoading transformed records into Postgres...")
        summary = await load_transformed_rows(transformed)
        print(json.dumps(summary, indent=2))
    else:
        print("\nDatabase load skipped. Re-run with --load-db to import into Postgres.")


if __name__ == "__main__":
    asyncio.run(main())
