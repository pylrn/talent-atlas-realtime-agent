"""Small helpers for consistent DB timing payloads.

`fetch_roundtrip_ms` is the asyncpg call duration after a connection is
acquired. It includes server execution, network travel, and row decoding.
"""

from __future__ import annotations

from typing import Any


def finalize_db_timing(timings: dict[str, Any]) -> dict[str, Any]:
    """Add normalized timing aliases and derived app overhead in-place."""
    if not timings:
        return timings

    pool_wait = _number(timings.get("pool_acquire_ms"))
    if pool_wait is not None:
        timings["pool_wait_ms"] = pool_wait

    roundtrip = _number(timings.get("fetch_roundtrip_ms"))
    if roundtrip is not None:
        timings["db_roundtrip_ms"] = roundtrip

    total = _number(timings.get("total_ms"))
    if total is not None:
        known = 0.0
        for key in (
            "pool_acquire_ms",
            "fetch_roundtrip_ms",
            "embedding_ms",
            "set_local_ms",
            "set_statement_timeout_ms",
        ):
            value = _number(timings.get(key))
            if value is not None:
                known += value
        timings["app_overhead_ms"] = round(max(0.0, total - known), 2)

    timings["timing_version"] = "db-v1"
    return timings


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    return None
