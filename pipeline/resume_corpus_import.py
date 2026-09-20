"""Shared import helpers for public resume corpus transformers."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable

from pipeline.recruitment_dataset import TransformedRecruitmentRow
from pipeline.resume_corpus import transform_resume_atlas_row, transform_resume_inference_row


Transformer = Callable[[dict[str, Any], int], TransformedRecruitmentRow | None]

TRANSFORMERS: dict[str, Transformer] = {
    "atlas": transform_resume_atlas_row,
    "inference": transform_resume_inference_row,
}


def transform_source_rows(
    source: str,
    rows: Iterable[dict[str, Any]],
    *,
    limit: int,
    offset: int = 0,
    max_per_role: int | None = None,
) -> list[TransformedRecruitmentRow]:
    """Transform rows, dropping duplicate resume text and optionally capping roles."""
    if source not in TRANSFORMERS:
        raise ValueError(f"Unsupported resume source: {source}")
    transformer = TRANSFORMERS[source]
    transformed: list[TransformedRecruitmentRow] = []
    seen_hashes: set[str] = set()
    role_counts: Counter[str] = Counter()

    for row_number, row in enumerate(rows, start=1):
        if row_number <= offset:
            continue
        item = transformer(row, row_number)
        if item is None:
            continue

        text_hash = _document_hash(item.document["raw_text"])
        if text_hash in seen_hashes:
            continue
        seen_hashes.add(text_hash)

        role = item.eval_case["job_role"]
        if max_per_role is not None and role_counts[role] >= max_per_role:
            continue
        role_counts[role] += 1

        transformed.append(item)
        if len(transformed) >= limit:
            break

    return transformed


def write_jsonl_outputs(
    transformed: list[TransformedRecruitmentRow],
    output_dir: Path,
    *,
    source: str,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    records_path = output_dir / f"{source}_transformed_{len(transformed)}.jsonl"
    eval_path = output_dir / f"{source}_eval_cases_{len(transformed)}.jsonl"

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


def _document_hash(raw_text: str) -> str:
    normalized = " ".join(raw_text.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
