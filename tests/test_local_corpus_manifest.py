import json

from scripts.import_recruitment_dataset import write_manifest


def test_manifest_records_source_transform_counts_and_output_hashes(tmp_path):
    records = tmp_path / "records.jsonl"
    evaluations = tmp_path / "eval.jsonl"
    records.write_text('{"candidate": 1}\n', encoding="utf-8")
    evaluations.write_text('{"case": 1}\n', encoding="utf-8")

    path = write_manifest(
        output_dir=tmp_path,
        dataset_url="https://example.test/dataset.zip",
        source_sha256="a" * 64,
        offset=0,
        requested_count=10_000,
        actual_count=10_000,
        records_path=records,
        eval_path=evaluations,
    )
    manifest = json.loads(path.read_text())

    assert manifest["source"]["sha256"] == "a" * 64
    assert manifest["transform_version"]
    assert manifest["requested_count"] == 10_000
    assert manifest["actual_count"] == 10_000
    assert len(manifest["outputs"]["records"]["sha256"]) == 64
    assert len(manifest["outputs"]["eval_cases"]["sha256"]) == 64
