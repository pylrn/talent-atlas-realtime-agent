"""
Benchmark all LLM planner models on quality + latency.

Scores each model on:
  - Latency (ms)
  - JSON validity
  - Required fields present
  - Skills extracted correctly
  - Location extracted correctly
  - Semantic query non-empty + relevant
  - Confidence score reasonable

Run:
  python scripts/benchmark_llm_models.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Load .env
from dotenv import load_dotenv
load_dotenv()

from pipeline.prompts import PLANNER_SYSTEM_PROMPT, build_user_message
from pipeline.sanitize import sanitize_input

# ── Test queries with expected extractions ─────────────────────────────────────

TEST_QUERIES = [
    {
        "label": "Simple location+skill",
        "input": "python developer in bangalore with 3 years experience",
        "expect_skills": ["python"],
        "expect_city": "bangalore",
        "expect_has_exp": True,
    },
    {
        "label": "Multi-skill fullstack",
        "input": "senior fullstack engineer react node postgres 5 to 8 years",
        "expect_skills": ["react", "node", "postgres"],
        "expect_city": None,
        "expect_has_exp": True,
    },
    {
        "label": "JD paste",
        "input": (
            "We are looking for a machine learning engineer with experience in PyTorch, "
            "scikit-learn, and MLflow. The candidate should have at least 4 years of "
            "experience building production ML pipelines. Strong Python skills required. "
            "Remote-friendly, based in India preferred."
        ),
        "expect_skills": ["python", "pytorch"],
        "expect_city": None,
        "expect_has_exp": True,
    },
    {
        "label": "Ambiguous (should clarify)",
        "input": "good engineer",
        "expect_skills": [],
        "expect_city": None,
        "expect_has_exp": False,
        "expect_low_confidence": True,
    },
    {
        "label": "Filter + status",
        "input": "hired frontend engineers in mumbai",
        "expect_skills": ["frontend"],
        "expect_city": "mumbai",
        "expect_has_exp": False,
    },
]

# ── Models to test ─────────────────────────────────────────────────────────────

MODELS = {
    "groq": [
        "llama-3.1-8b-instant",
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "moonshotai/kimi-k2-instruct",
        "qwen/qwen3-32b",
        "llama-3.3-70b-versatile",
    ],
    "gemini": [
        "gemini-3-flash",
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
    ],
}

# ── LLM callers ───────────────────────────────────────────────────────────────

async def call_groq(model: str, sanitized: str) -> tuple[dict | None, float]:
    from openai import AsyncOpenAI
    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        return None, 0.0
    client = AsyncOpenAI(api_key=key, base_url="https://api.groq.com/openai/v1")
    t0 = time.perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
                {"role": "user",   "content": build_user_message(sanitized)},
            ],
            temperature=0.1,
            max_tokens=800,
            response_format={"type": "json_object"},
        )
        ms = (time.perf_counter() - t0) * 1000
        return _parse(resp.choices[0].message.content or ""), ms
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        return {"_error": str(e)}, ms


async def call_gemini(model: str, sanitized: str) -> tuple[dict | None, float]:
    from google import genai
    key = os.environ.get("GOOGLE_API_KEY", "")
    if not key:
        return None, 0.0
    client = genai.Client(api_key=key)
    loop = asyncio.get_running_loop()
    prompt = f"{PLANNER_SYSTEM_PROMPT}\n\n{build_user_message(sanitized)}"
    t0 = time.perf_counter()
    try:
        resp = await loop.run_in_executor(
            None,
            lambda: client.models.generate_content(
                model=model,
                contents=prompt,
                config={"temperature": 0.1, "max_output_tokens": 800},
            ),
        )
        ms = (time.perf_counter() - t0) * 1000
        return _parse(resp.text or ""), ms
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        return {"_error": str(e)}, ms


def _parse(raw: str) -> dict:
    import re
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        s, e = text.find("{"), text.rfind("}")
        if s == -1 or e <= s:
            return {"_error": "no JSON found"}
        text = text[s:e+1]
    try:
        return json.loads(text)
    except Exception as ex:
        return {"_error": f"JSON parse: {ex}"}


# ── Scoring ───────────────────────────────────────────────────────────────────

def score(result: dict | None, query_meta: dict) -> dict[str, Any]:
    if result is None:
        return {"total": 0, "valid_json": 0, "fields": 0, "skills": 0, "location": 0, "semantic": 0, "error": "no response"}
    if "_error" in result:
        return {"total": 0, "valid_json": 0, "fields": 0, "skills": 0, "location": 0, "semantic": 0, "error": result["_error"]}

    scores = {}
    scores["valid_json"] = 1

    # Required fields
    required = {"must", "should", "semantic_query", "confidence", "intent"}
    scores["fields"] = 1 if required.issubset(result.keys()) else 0

    # Skills extraction
    expected_skills = query_meta.get("expect_skills", [])
    if expected_skills:
        got_skills = [s.lower() for s in (result.get("must", {}).get("skills", []) +
                                           result.get("should", {}).get("skills", []))]
        matched = sum(1 for s in expected_skills if any(s in g or g in s for g in got_skills))
        scores["skills"] = round(matched / len(expected_skills), 2)
    else:
        scores["skills"] = 1.0  # nothing to extract = pass

    # City extraction
    expected_city = query_meta.get("expect_city")
    if expected_city:
        got_city = (result.get("must", {}).get("city") or "").lower()
        scores["location"] = 1 if expected_city.lower() in got_city or got_city in expected_city.lower() else 0
    else:
        scores["location"] = 1.0

    # Semantic query quality
    sem = result.get("semantic_query", "")
    scores["semantic"] = 1 if sem and len(sem) > 10 else 0

    # Confidence gate for ambiguous query
    if query_meta.get("expect_low_confidence"):
        conf = float(result.get("confidence", 1.0))
        scores["confidence_gate"] = 1 if conf < 0.6 or result.get("clarify") else 0
    else:
        scores["confidence_gate"] = 1

    scores["total"] = round(
        scores["valid_json"] * 0.1 +
        scores["fields"]     * 0.2 +
        scores["skills"]     * 0.3 +
        scores["location"]   * 0.2 +
        scores["semantic"]   * 0.1 +
        scores["confidence_gate"] * 0.1,
        3,
    )
    return scores


# ── Runner ────────────────────────────────────────────────────────────────────

async def test_model(provider: str, model: str) -> dict:
    results = []
    for q in TEST_QUERIES:
        sanitized = sanitize_input(q["input"])
        if provider == "groq":
            raw, ms = await call_groq(model, sanitized)
        else:
            raw, ms = await call_gemini(model, sanitized)
        s = score(raw, q)
        results.append({"query": q["label"], "ms": round(ms), "score": s, "raw": raw})
        # small delay to avoid rate limits
        await asyncio.sleep(0.3)
    return {"model": model, "provider": provider, "results": results}


async def run_all():
    print(f"\n{'='*70}")
    print("LLM PLANNER BENCHMARK")
    print(f"{'='*70}\n")

    summary = []

    for provider, models in MODELS.items():
        for model in models:
            print(f"Testing {provider} / {model} ...")
            data = await test_model(provider, model)

            latencies = [r["ms"] for r in data["results"]]
            total_scores = [r["score"]["total"] for r in data["results"]]
            errors = [r for r in data["results"] if "error" in r["score"]]

            avg_ms     = round(sum(latencies) / len(latencies))
            avg_score  = round(sum(total_scores) / len(total_scores), 3)
            error_rate = f"{len(errors)}/{len(TEST_QUERIES)} failed"

            summary.append({
                "provider": provider,
                "model": model,
                "avg_ms": avg_ms,
                "avg_score": avg_score,
                "errors": len(errors),
                "detail": data["results"],
            })

            status = "✓" if len(errors) == 0 else "✗"
            print(f"  {status} avg {avg_ms}ms  score {avg_score:.3f}  {error_rate}")

            # Per-query detail
            for r in data["results"]:
                s = r["score"]
                err = f" ← ERROR: {s.get('error','')[:60]}" if "error" in s else ""
                print(f"      [{r['ms']:4d}ms] {r['query']:<35} total={s['total']:.2f}  "
                      f"skills={s.get('skills','-')}  loc={s.get('location','-')}{err}")
            print()

    # ── Final leaderboard ──────────────────────────────────────────────────────
    print(f"\n{'='*70}")
    print("LEADERBOARD  (sorted by score desc, then latency asc)")
    print(f"{'='*70}")
    ranked = sorted(summary, key=lambda x: (-x["avg_score"], x["avg_ms"]))
    print(f"{'Rank':<5} {'Provider':<8} {'Model':<45} {'Score':>6} {'Avg ms':>7} {'Errors':>7}")
    print("-" * 80)
    for i, r in enumerate(ranked, 1):
        model_short = r["model"].split("/")[-1][:44]
        print(f"  {i:<4} {r['provider']:<8} {model_short:<45} {r['avg_score']:>6.3f} {r['avg_ms']:>7} {r['errors']:>7}")

    best = ranked[0]
    print(f"\n→ Recommended: {best['provider']} / {best['model']}")
    print(f"  Score {best['avg_score']:.3f}, avg {best['avg_ms']}ms\n")

    return ranked


if __name__ == "__main__":
    asyncio.run(run_all())
