#!/usr/bin/env python3
"""
Replay a search trace from Langfuse.

Usage:
    python3 scripts/replay_trace.py <trace_id>

Fetches the trace inputs from Langfuse and re-runs the search pipeline locally,
printing the top results for comparison.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


async def replay_trace(trace_id: str):
    from langfuse import Langfuse

    print(f"Fetching trace {trace_id} from Langfuse...")
    lf = Langfuse()
    try:
        trace = lf.client.trace.get(trace_id)
        if not trace:
            print(f"Trace {trace_id} not found.")
            return
    except Exception as exc:
        print(f"Failed to fetch trace: {exc}")
        return

    # Extract query from trace input
    query = ""
    if trace.input and isinstance(trace.input, dict):
        query = trace.input.get("query", "")
    elif isinstance(trace.input, str):
        query = trace.input

    print(f"Found query: {query!r}")
    if not query:
        print("No query found in trace input. Aborting.")
        return

    # Extract mode if present
    mode = "quality"
    if trace.input and isinstance(trace.input, dict):
        mode = trace.input.get("mode", "quality")

    print(f"Mode: {mode}")
    print("Re-running search locally...\n")

    from pipeline.database import get_pool, close_pool
    from pipeline.embedder import get_embedder
    from pipeline.search import HybridSearchEngine

    pool = await get_pool()
    try:
        embedder = get_embedder()
        engine = HybridSearchEngine(pool, embedder=embedder)

        if hasattr(engine, "smart_search"):
            result = await engine.smart_search(
                query=query,
                jd=None,
                explicit_filters=None,
                mode=mode,
                top_k=20,
                recruiter_id="00000000-0000-0000-0000-000000000000",
                config_overrides={},
            )
            candidates = result.results
        else:
            candidates = await engine.search(
                query=query,
                filters=None,
                top_k=20,
            )

        print(f"Search returned {len(candidates)} candidates.\n")
        for idx, r in enumerate(candidates[:10], 1):
            name = getattr(r, "full_name", "?")
            cid = getattr(r, "candidate_id", "?")
            score = getattr(r, "rank_score", None) or getattr(r, "similarity_score", 0)
            city = getattr(r, "city", "")
            country = getattr(r, "country", "")
            print(f"  {idx:2d}. {name} ({city}, {country}) — score={score:.4f}  [{cid}]")
    finally:
        await close_pool()
        lf.flush()


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/replay_trace.py <trace_id>")
        sys.exit(1)

    trace_id = sys.argv[1]
    asyncio.run(replay_trace(trace_id))


if __name__ == "__main__":
    main()
