"""
Quality evaluation: no-llm vs fast-llm vs insights.
Runs 15 queries across 4 types, scores relevance via LLM-as-judge,
compares filter extraction, result overlap, and insights agreement.
"""
import json, time, urllib.request, urllib.error, statistics, textwrap, os

BASE = "http://localhost:8000"
GROQ_KEY = os.getenv("GROQ_API_KEY", "")

# ── 15 eval queries across 4 types ───────────────────────────────────────────
QUERIES = [
    # Type A — keyword/structured (no-llm should handle fine)
    {"q": "python engineer",                                          "type": "keyword"},
    {"q": "senior javascript developer",                              "type": "keyword"},
    {"q": "data engineer",                                            "type": "keyword"},
    {"q": "backend engineer golang",                                  "type": "keyword"},

    # Type B — natural language intent (LLM advantage)
    {"q": "someone who can own the data pipeline end to end",         "type": "nl_intent"},
    {"q": "we need a tech lead who has grown engineering teams",      "type": "nl_intent"},
    {"q": "looking for engineers with startup scrappiness",           "type": "nl_intent"},
    {"q": "candidates who have shipped products to millions of users","type": "nl_intent"},

    # Type C — multi-constraint (LLM advantage)
    {"q": "senior python engineer with django at least 5 years",      "type": "multi_constraint"},
    {"q": "backend engineer microservices and strong api design",      "type": "multi_constraint"},
    {"q": "fullstack developer react python with open source work",   "type": "multi_constraint"},
    {"q": "engineer with ml and strong software engineering skills",  "type": "multi_constraint"},

    # Type D — vague/conversational (stress test)
    {"q": "someone scrappy who gets things done",                     "type": "vague"},
    {"q": "a generalist who can wear many hats",                      "type": "vague"},
    {"q": "engineers with good communication skills",                 "type": "vague"},
]


def post(path, body, timeout=60):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BASE}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read()), None
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return None, str(e)[:200]


def groq_judge(query, results_a, results_b, label_a="no-llm", label_b="fast-llm"):
    """Ask Groq to compare two result sets. Returns (score_a, score_b, reasoning)."""
    if not GROQ_KEY:
        return None, None, "no groq key"

    def fmt_results(results):
        out = []
        for i, r in enumerate(results[:3], 1):
            skills = ", ".join((r.get("skills") or [])[:6])
            chunk = (r.get("best_chunk") or "")[:200]
            out.append(f"  {i}. {r.get('full_name','?')} | skills: {skills}\n     excerpt: {chunk}")
        return "\n".join(out) if out else "  (no results)"

    prompt = f"""You are evaluating search results for a recruiter query.

Query: "{query}"

Result set A ({label_a}):
{fmt_results(results_a)}

Result set B ({label_b}):
{fmt_results(results_b)}

Score each result set 1-10 for how well the top candidates match the query intent.
Consider: skill match, relevance of experience, query intent understanding.

Respond in JSON only:
{{"score_a": <1-10>, "score_b": <1-10>, "reasoning": "<1-2 sentences>", "winner": "A" | "B" | "tie"}}"""

    req_data = json.dumps({
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 200,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=req_data,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {GROQ_KEY}"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
            content = resp["choices"][0]["message"]["content"]
            parsed = json.loads(content)
            return parsed.get("score_a"), parsed.get("score_b"), parsed.get("reasoning",""), parsed.get("winner","?")
    except Exception as e:
        return None, None, str(e)[:100], "?"


def compare_filters(spec_a, spec_b):
    """Return dict of filter differences between two planner specs."""
    if not spec_a or not spec_b:
        return {}
    keys = ["must", "should", "semantic_query", "lexical_terms", "intent"]
    diffs = {}
    for k in keys:
        va = spec_a.get(k)
        vb = spec_b.get(k)
        if va != vb:
            diffs[k] = {"no_llm": va, "fast_llm": vb}
    return diffs


def candidate_overlap(res_a, res_b):
    ids_a = {r.get("candidate_id") for r in (res_a or [])}
    ids_b = {r.get("candidate_id") for r in (res_b or [])}
    if not ids_a and not ids_b:
        return 0.0
    if not ids_a or not ids_b:
        return 0.0
    return len(ids_a & ids_b) / len(ids_a | ids_b)


def run():
    results = []
    total = len(QUERIES)

    print(f"Running quality eval: {total} queries × 3 modes")
    print("=" * 70)

    for i, item in enumerate(QUERIES, 1):
        q = item["q"]
        qtype = item["type"]
        print(f"\n[{i:02d}/{total}] [{qtype}] {q[:60]}")

        # ── no-llm ──────────────────────────────────────────────────────
        t = time.perf_counter()
        resp_nollm, err_nollm = post("/search", {
            "query": q, "top_k": 10, "mode": "no-llm",
            "include_rank_explanation": True,
        })
        ms_nollm = round((time.perf_counter() - t) * 1000)
        n_nollm = len((resp_nollm or {}).get("results", []))
        spec_nollm = (resp_nollm or {}).get("planner_spec") or {}
        print(f"  no-llm:   {ms_nollm:5d}ms | {n_nollm} results")

        time.sleep(0.3)

        # ── fast (groq 8b) ──────────────────────────────────────────────
        t = time.perf_counter()
        resp_fast, err_fast = post("/search", {
            "query": q, "top_k": 10, "mode": "fast",
            "llm_provider": "groq", "llm_model": "llama-3.1-8b-instant",
            "include_rank_explanation": True,
        }, timeout=25)
        ms_fast = round((time.perf_counter() - t) * 1000)
        n_fast = len((resp_fast or {}).get("results", []))
        spec_fast = (resp_fast or {}).get("planner_spec") or {}
        print(f"  fast-llm: {ms_fast:5d}ms | {n_fast} results | err: {err_fast or 'none'}")

        time.sleep(4)  # groq rate limit buffer

        # ── insights on no-llm results ───────────────────────────────────
        insight_best = None
        insight_status = "skipped"
        if n_nollm > 0:
            t = time.perf_counter()
            resp_ins, err_ins = post("/search/insights", {
                "query": q, "top_k": 10, "mode": "no-llm",
            }, timeout=60)
            ms_ins = round((time.perf_counter() - t) * 1000)
            if resp_ins:
                insight_status = resp_ins.get("status", "?")
                insight_best = resp_ins.get("best_candidate_id")
                print(f"  insights: {ms_ins:5d}ms | status: {insight_status} | best: {(insight_best or 'none')[:8]}")
            else:
                print(f"  insights: error — {err_ins}")
            time.sleep(4)

        # ── LLM-as-judge ─────────────────────────────────────────────────
        score_nollm, score_fast, judge_reasoning, winner = None, None, "skipped", "?"
        if n_nollm > 0 and n_fast > 0 and GROQ_KEY:
            score_nollm, score_fast, judge_reasoning, winner = groq_judge(
                q,
                (resp_nollm or {}).get("results", []),
                (resp_fast or {}).get("results", []),
            )
            print(f"  judge:    no-llm={score_nollm} fast={score_fast} winner={winner}")
            time.sleep(3)

        # ── filter diff ───────────────────────────────────────────────────
        filter_diffs = compare_filters(spec_nollm, spec_fast)
        overlap = candidate_overlap(
            (resp_nollm or {}).get("results", []),
            (resp_fast or {}).get("results", []),
        )

        # ── top candidate agreement ───────────────────────────────────────
        top_nollm = ((resp_nollm or {}).get("results") or [{}])[0].get("candidate_id")
        top_fast = ((resp_fast or {}).get("results") or [{}])[0].get("candidate_id")
        top_agree = top_nollm == top_fast == insight_best if insight_best else (top_nollm == top_fast)

        results.append({
            "query": q,
            "type": qtype,
            "ms_nollm": ms_nollm,
            "ms_fast": ms_fast,
            "n_nollm": n_nollm,
            "n_fast": n_fast,
            "overlap": round(overlap, 2),
            "spec_nollm": spec_nollm,
            "spec_fast": spec_fast,
            "filter_diffs": filter_diffs,
            "score_nollm": score_nollm,
            "score_fast": score_fast,
            "judge_winner": winner,
            "judge_reasoning": judge_reasoning,
            "insight_status": insight_status,
            "insight_best": insight_best,
            "top_nollm": top_nollm,
            "top_fast": top_fast,
            "top_agree": top_agree,
            "err_fast": err_fast,
        })

    # ── write raw results ─────────────────────────────────────────────────────
    with open("/tmp/eval_results.json", "w") as f:
        json.dump(results, f, indent=2, default=str)

    # ── summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    by_type = {}
    for r in results:
        by_type.setdefault(r["type"], []).append(r)

    scored = [r for r in results if r["score_nollm"] is not None]
    nollm_wins = sum(1 for r in scored if r["judge_winner"] == "A")
    fast_wins  = sum(1 for r in scored if r["judge_winner"] == "B")
    ties       = sum(1 for r in scored if r["judge_winner"] == "tie")

    print(f"Judged queries: {len(scored)}/{len(results)}")
    print(f"Winner: no-llm={nollm_wins}  fast-llm={fast_wins}  tie={ties}")
    if scored:
        print(f"Avg score no-llm: {round(statistics.mean(r['score_nollm'] for r in scored), 1)}")
        print(f"Avg score fast:   {round(statistics.mean(r['score_fast']   for r in scored), 1)}")
    print()

    for qtype, rows in by_type.items():
        s = [r for r in rows if r["score_nollm"] is not None]
        overlap_avg = round(statistics.mean(r["overlap"] for r in rows), 2)
        diff_count  = sum(1 for r in rows if r["filter_diffs"])
        print(f"[{qtype}]  overlap={overlap_avg}  filter_diffs={diff_count}/{len(rows)}", end="")
        if s:
            print(f"  judge: nollm={round(statistics.mean(r['score_nollm'] for r in s),1)} fast={round(statistics.mean(r['score_fast'] for r in s),1)}", end="")
        print()

    agree = sum(1 for r in results if r.get("top_agree"))
    print(f"\nTop-1 candidate agreement (all modes): {agree}/{len(results)}")
    print(f"\nResults saved to /tmp/eval_results.json")


if __name__ == "__main__":
    import sys
    # load groq key from .env if not in env
    if not GROQ_KEY:
        try:
            for line in open("/Volumes/MAC/Projects_devolopment/hybrid search/.env"):
                if line.startswith("GROQ_API_KEY="):
                    GROQ_KEY = line.strip().split("=", 1)[1]
                    break
        except:
            pass
    if not GROQ_KEY:
        print("WARNING: GROQ_API_KEY not found, judge scoring disabled")
    run()
