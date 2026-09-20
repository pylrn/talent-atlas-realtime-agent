"""Tests for RRF fusion + search-target weighting."""

from pipeline.fusion import rrf_fuse
from pipeline.group import group_by_candidate


def _row(chunk_id, doc_type="resume", cid="c1"):
    return {
        "chunk_id":        chunk_id,
        "candidate_id":    cid,
        "content":         "",
        "document_id":     "d1",
        "doc_type":        doc_type,
        "document_title":  "",
        "distance":        0.1,
    }


def test_rrf_score_math():
    """A chunk at rank 1 across all three paths should accumulate
    three full-weight 1/(60+1) contributions."""
    rows = [_row("c1")]
    fused = rrf_fuse(
        dense_rows=rows, bm25_rows=rows, skill_rows=rows,
        spec_targets=[], rrf_k=60,
    )
    expected = 3.0 * (1.0 / (60 + 1))
    assert abs(fused[0]["fused_rrf_score"] - expected) < 1e-9


def test_off_target_chunks_get_half_weight():
    """When spec_targets is set, off-target doc_types get 0.5× weight."""
    on  = [_row("on",  doc_type="resume",     cid="a")]   # in target
    off = [_row("off", doc_type="transcript", cid="b")]   # off target

    fused = rrf_fuse(
        dense_rows=on, bm25_rows=off, skill_rows=[],
        spec_targets=["resume_chunks"],   # maps to ['resume']
        rrf_k=60,
    )
    by_id = {f["chunk_id"]: f["fused_rrf_score"] for f in fused}
    assert by_id["on"]  > by_id["off"]
    # ratio should be ~2× (full vs half weight, identical rank 1)
    assert by_id["on"] / by_id["off"] == 2.0


def test_no_weighting_when_disabled():
    on  = [_row("on",  doc_type="resume",     cid="a")]
    off = [_row("off", doc_type="transcript", cid="b")]
    fused = rrf_fuse(
        dense_rows=on, bm25_rows=off, skill_rows=[],
        spec_targets=["resume_chunks"],
        rrf_k=60,
        use_search_target_weighting=False,
    )
    by_id = {f["chunk_id"]: f["fused_rrf_score"] for f in fused}
    assert by_id["on"] == by_id["off"]


def test_retrieval_paths_recorded():
    rows = [_row("c1")]
    fused = rrf_fuse(
        dense_rows=rows, bm25_rows=rows, skill_rows=[],
        spec_targets=[], rrf_k=60,
    )
    assert set(fused[0]["retrieval_paths"]) == {"dense", "bm25"}


def test_chunks_sorted_by_fused_score():
    a = _row("a", cid="a"); b = _row("b", cid="b")
    fused = rrf_fuse(
        dense_rows=[a, b], bm25_rows=[a], skill_rows=[],
        spec_targets=[], rrf_k=60,
    )
    # 'a' appears on two lists, 'b' on one → 'a' wins
    assert fused[0]["chunk_id"] == "a"
    assert fused[1]["chunk_id"] == "b"


def test_fusion_ties_sort_by_candidate_then_chunk_id():
    a = _row("chunk-a", cid="candidate-a")
    b = _row("chunk-b", cid="candidate-b")

    fused = rrf_fuse(
        dense_rows=[b], bm25_rows=[a], skill_rows=[],
        spec_targets=[], rrf_k=60,
    )

    assert [row["candidate_id"] for row in fused] == ["candidate-a", "candidate-b"]


def test_group_ties_sort_by_candidate_id():
    rows = [
        {**_row("chunk-b", cid="candidate-b"), "fused_rrf_score": 1.0, "retrieval_paths": ["dense"]},
        {**_row("chunk-a", cid="candidate-a"), "fused_rrf_score": 1.0, "retrieval_paths": ["dense"]},
    ]

    grouped = group_by_candidate(rows)

    assert [item.candidate_id for item in grouped] == ["candidate-a", "candidate-b"]
