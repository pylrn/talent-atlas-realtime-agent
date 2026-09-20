"""Populate Langfuse with search queries — sequential, one at a time, with full result summary."""
import sys
import time
import urllib.request
import urllib.error
import json
import random

BASE_URL = "http://localhost:8000"

QUERIES = [
    "senior python engineer with django and postgres",
    "fullstack react developer with typescript and node",
    "machine learning engineer with pytorch and transformers",
    "devops engineer kubernetes aws terraform",
    "data engineer apache spark kafka pipeline",
    "backend golang engineer microservices grpc",
    "ios developer swift swiftui xcode",
    "android developer kotlin jetpack compose",
    "data scientist python pandas scikit-learn",
    "site reliability engineer sre linux monitoring",
    "cloud architect aws gcp multi-cloud",
    "security engineer penetration testing appsec",
    "product manager b2b saas fintech",
    "ux designer figma design systems",
    "frontend engineer vue nuxt css animation",
    "java spring boot microservices kafka",
    "rust systems engineer embedded linux",
    "ruby on rails engineer postgres redis",
    "scala spark data platform engineer",
    "c++ game engine developer opengl",
    "senior software engineer with strong dsa algorithms background",
    "looking for a recruiter with hr and layoff experience",
    "marketing manager with growth hacking experience",
    "finance analyst with excel and sql skills",
    "blockchain developer solidity smart contracts ethereum",
    "ai researcher nlp language models fine-tuning",
    "database administrator postgres mysql performance tuning",
    "network engineer cisco routing switching",
    "qa automation engineer selenium pytest",
    "technical lead with team management and architecture experience",
    "php laravel developer ecommerce",
    "react native mobile developer cross platform",
    "elixir phoenix developer distributed systems",
    "data analyst tableau power bi sql",
    "computer vision engineer opencv deep learning",
    "embedded systems engineer c firmware rtos",
    "solution architect enterprise integration patterns",
    "scrum master agile coach certified",
    "python data pipeline engineer airflow dbt",
    "senior frontend engineer performance web vitals",
    "backend engineer with experience in payment systems",
    "engineer with experience in healthcare tech hipaa",
    "engineer with open source contributions github",
    "remote first engineer strong communication async",
    "engineer with startup experience fast paced",
    "senior engineer with mentoring and code review experience",
    "principal engineer distributed systems consistency",
    "staff engineer platform infrastructure",
    "engineer with experience migrating monolith to microservices",
    "developer with strong testing tdd bdd background",
    "fullstack engineer vue python fastapi",
    "engineer with real time systems low latency trading",
    "backend engineer with strong api design restful graphql",
    "developer with saas multi-tenant architecture experience",
    "cloud native engineer docker kubernetes helm",
    "engineer with observability prometheus grafana datadog",
    "search engineer elasticsearch opensearch relevance tuning",
    "engineer with recommendation systems collaborative filtering",
    "data platform engineer snowflake databricks",
    "engineer with event-driven architecture rabbitmq kafka",
    "python engineer with async concurrency fastapi",
    "typescript node backend engineer prisma",
    "engineer with ci cd github actions jenkins",
    "mobile engineer with offline-first sync experience",
    "computer vision mlops model deployment",
    "senior engineer with 10 years python experience",
    "frontend accessibility wcag engineer",
    "engineer with graph databases neo4j",
    "platform engineer internal developer tools",
    "engineer with vector databases pinecone weaviate",
    "nlp engineer text classification extraction",
    "backend engineer caching redis memcached",
    "data engineer dbt snowflake fivetran",
    "engineer with auth systems oauth saml",
    "python engineer scientific computing numpy scipy",
    "engineer with logistics supply chain tech",
    "engineer with experience in ad tech programmatic",
    "engineer experienced in gaming backend unity",
    "senior engineer with m&a technical due diligence",
    "engineer with iot mqtt embedded sensors",
    "engineer with geospatial postgis mapping",
    "devops engineer terraform azure infrastructure as code",
    "machine learning engineer feature engineering pipelines",
    "we need cpp engineers with strong data structures",
    "someone who knows kubernetes and can lead a platform team",
    "show me candidates with react and graphql",
    "looking for a senior engineer who has worked in fintech",
    "any candidates with both design and engineering skills",
    "engineers who have led migrations to the cloud",
    "find me candidates from london with python skills",
    "candidates who have published research or papers",
    "show me all active candidates with rust experience",
    "any engineers who have worked at faang companies",
    "find candidates with both frontend and backend experience",
    "need someone who can own the entire data pipeline",
    "engineer with strong written communication and docs",
    "engineer who has built products from zero to one",
    "find me a cto level technical leader",
    "candidates with experience in series a startups",
    "someone who can build and scale apis to millions of users",
    "engineer with strong system design for high availability",
]

def post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read())


def run():
    queries = QUERIES.copy()
    random.shuffle(queries)

    total = len(queries)
    results = []

    print(f"Starting {total} sequential queries — no-llm mode / no AI insights", flush=True)
    print("-" * 75, flush=True)

    for i, query in enumerate(queries, 1):
        t = time.perf_counter()
        try:
            data = post("/search", {
                "query": query,
                "top_k": 10,
                "include_ai_insights": False,
                "mode": "no-llm",
            })
            elapsed = round((time.perf_counter() - t) * 1000)
            n_results = len(data.get("results", []))
            ai = data.get("ai_insights") or {}
            insight_status = ai.get("status", "none")
            comparative = bool(ai.get("comparative_reasoning"))
            results.append({"ok": True, "query": query, "results": n_results,
                            "insight_status": insight_status, "comparative": comparative,
                            "ms": elapsed})
            print(f"[{i:03d}/{total}] {elapsed:5d}ms | {n_results:2d} results | "
                  f"insights:{insight_status:<12} comparative:{comparative} | {query[:50]}", flush=True)
        except Exception as e:
            elapsed = round((time.perf_counter() - t) * 1000)
            results.append({"ok": False, "query": query, "error": str(e), "ms": elapsed})
            print(f"[{i:03d}/{total}] {elapsed:5d}ms | ERROR: {str(e)[:60]} | {query[:50]}", flush=True)

        # no-llm mode: no LLM calls, no rate limit concerns — run fast
        time.sleep(0.2)

    # ── Summary ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 75, flush=True)
    print("RESULTS SUMMARY", flush=True)
    print("=" * 75, flush=True)

    ok      = [r for r in results if r.get("ok")]
    failed  = [r for r in results if not r.get("ok")]
    zero    = [r for r in ok if r["results"] == 0]
    with_insights = [r for r in ok if r["insight_status"] == "ok"]
    with_comparative = [r for r in ok if r.get("comparative")]
    avg_ms  = round(sum(r["ms"] for r in ok) / len(ok)) if ok else 0
    max_ms  = max((r["ms"] for r in ok), default=0)
    min_ms  = min((r["ms"] for r in ok), default=0)

    print(f"Total queries:          {total}", flush=True)
    print(f"Successful:             {len(ok)}", flush=True)
    print(f"Failed:                 {len(failed)}", flush=True)
    print(f"Zero results:           {len(zero)}", flush=True)
    print(f"With insights (ok):     {len(with_insights)}", flush=True)
    print(f"With comparative:       {len(with_comparative)}", flush=True)
    print(f"Avg latency:            {avg_ms}ms", flush=True)
    print(f"Min / Max latency:      {min_ms}ms / {max_ms}ms", flush=True)

    if failed:
        print(f"\nFailed queries:", flush=True)
        for r in failed:
            print(f"  - {r['query'][:60]} → {r.get('error','?')[:60]}", flush=True)

    if zero:
        print(f"\nZero-result queries:", flush=True)
        for r in zero:
            print(f"  - {r['query'][:70]}", flush=True)

    print("\nDone. Check Langfuse for traces.", flush=True)


if __name__ == "__main__":
    run()
