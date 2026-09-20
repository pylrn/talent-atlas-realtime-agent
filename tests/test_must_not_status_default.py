"""must_not.status must default to EMPTY, not ['active'].

Regression: _coerce_status_list defaults empty -> ['active'], which is correct for
must.status but was also used for must_not.status. That produced
"status = active AND status != active" — an always-false filter that zeroed out
every LLM-planned candidate_search.
"""

from __future__ import annotations

from pipeline.validator import _parse_must, _parse_must_not


def test_must_not_status_defaults_to_empty():
    mn = _parse_must_not({})
    assert mn.status == [], f"must_not.status should be empty, got {mn.status!r}"


def test_must_status_still_defaults_to_active():
    m = _parse_must({})
    assert m.status == ["active"]


def test_must_not_status_keeps_explicit_values():
    mn = _parse_must_not({"status": ["archived", "hired"]})
    assert mn.status == ["archived", "hired"]


def test_no_contradiction_when_both_default():
    # The core of the bug: required-active must NOT collide with excluded-active.
    m = _parse_must({})
    mn = _parse_must_not({})
    overlap = set(m.status) & set(mn.status)
    assert not overlap, f"must.status and must_not.status overlap: {overlap}"
