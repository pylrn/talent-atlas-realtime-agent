"""Verify the external-SSD Talent Atlas corpus and its reproducibility manifest."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import asyncpg

from pipeline import settings


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-candidates", type=int, default=10_000)
    parser.add_argument("--require-external-root", action="store_true")
    parser.add_argument("--manifest", type=Path)
    return parser.parse_args()


def verify_manifest(path: Path, expected_count: int) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("actual_count") != expected_count:
        raise RuntimeError(
            f"Manifest contains {manifest.get('actual_count')} rows, expected {expected_count}"
        )
    for output in manifest.get("outputs", {}).values():
        output_path = Path(output["path"])
        if not output_path.is_absolute():
            output_path = PROJECT_ROOT / output_path
        actual = file_sha256(output_path)
        if actual != output.get("sha256"):
            raise RuntimeError(f"Checksum mismatch for {output_path}")
    return manifest


async def database_counts() -> dict[str, int]:
    connection = await asyncpg.connect(settings.database_url)
    try:
        row = await connection.fetchrow(
            """
            SELECT
              (SELECT COUNT(*) FROM candidates) AS candidates,
              (SELECT COUNT(*) FROM candidate_documents) AS documents,
              (SELECT COUNT(*) FROM document_chunks) AS chunks,
              (SELECT COUNT(*) FROM document_chunks WHERE embedding IS NOT NULL) AS embeddings
            """
        )
        return {key: int(row[key]) for key in row.keys()}
    finally:
        await connection.close()


async def main() -> None:
    args = parse_args()
    if args.require_external_root and not str(PROJECT_ROOT.resolve()).startswith("/Volumes/MAC/"):
        raise RuntimeError(f"Project is not on the required external SSD: {PROJECT_ROOT}")

    manifest_path = args.manifest or (
        PROJECT_ROOT / "data/recruitment_dataset" / f"manifest_{args.expected_candidates}.json"
    )
    manifest = verify_manifest(manifest_path, args.expected_candidates)
    counts = await database_counts()
    if counts["candidates"] != args.expected_candidates:
        raise RuntimeError(
            f"Database contains {counts['candidates']} candidates, expected {args.expected_candidates}"
        )
    for key in ("documents", "chunks", "embeddings"):
        if counts[key] < args.expected_candidates:
            raise RuntimeError(
                f"Database contains only {counts[key]} {key}; expected at least {args.expected_candidates}"
            )

    runtime = PROJECT_ROOT / ".runtime"
    result = {
        "project_root": str(PROJECT_ROOT),
        "runtime_root": str(runtime),
        "postgres_bind_mount": str(runtime / "postgres"),
        "transform_version": manifest["transform_version"],
        "source_sha256": manifest["source"]["sha256"],
        "counts": counts,
        "verified": True,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
