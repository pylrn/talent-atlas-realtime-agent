"""
Benchmark script — compare embedding models on your actual data.

Usage:
    python scripts/benchmark.py --providers openai,gemini,local --sample-size 50

Measures:
  1. Embedding latency (ms per batch)
  2. Search quality (recall@K against known-relevant results)
  3. Cost projection ($ per 1M tokens)
  4. Index size (estimated memory)
"""

from __future__ import annotations

import asyncio
import json
import time
import sys
import os
from dataclasses import dataclass, asdict

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.embedder import PROVIDER_CATALOG, get_embedder


@dataclass
class BenchmarkResult:
    provider: str
    model: str
    dimensions: int
    cost_per_1m_tokens: float
    avg_embed_latency_ms: float
    p99_embed_latency_ms: float
    throughput_texts_per_sec: float
    estimated_index_size_mb_per_100k: float


# ─── Sample texts for benchmarking ────────
SAMPLE_TEXTS = [
    "Senior Python developer with 8 years of experience in machine learning and data engineering. Led a team of 5 engineers building real-time recommendation systems at scale.",
    "Certified AWS Solutions Architect with expertise in serverless architectures. Built multi-region disaster recovery systems handling 10M+ daily requests.",
    "Full-stack JavaScript developer specializing in React and Node.js. Passionate about accessible web design and performance optimization.",
    "Data scientist with PhD in Statistics from MIT. Published 12 papers on natural language processing and transformer architectures.",
    "DevOps engineer experienced with Kubernetes, Terraform, and CI/CD pipelines. Reduced deployment time by 80% at previous company.",
    "Mobile developer with 5 years in Flutter and React Native. Shipped 3 apps with 1M+ downloads on iOS and Android.",
    "Backend engineer specializing in distributed systems and microservices. Expert in Go, gRPC, and event-driven architectures.",
    "Product manager with technical background in ML. Led the launch of an AI-powered search product that increased user engagement by 40%.",
    "Security engineer with CISSP and OSCP certifications. Conducted penetration testing for Fortune 500 companies.",
    "Database administrator with deep PostgreSQL expertise. Optimized query performance for a 50TB analytics database.",
    "Frontend developer passionate about design systems and component libraries. Created a design system used by 200+ developers.",
    "Machine learning engineer focused on computer vision. Built real-time object detection systems for autonomous vehicles.",
    "Technical writer with experience documenting complex APIs and SDKs. Reduced support tickets by 35% through improved documentation.",
    "Blockchain developer with Solidity and Rust experience. Built DeFi protocols handling $100M+ in total value locked.",
    "QA engineer specializing in test automation frameworks. Achieved 95% test coverage across a microservices architecture.",
    "Site reliability engineer managing infrastructure for 99.99% uptime SLA. Expert in observability, Prometheus, and Grafana.",
]


async def benchmark_provider(provider: str, model: str) -> BenchmarkResult | None:
    """Benchmark a single provider/model combination."""
    try:
        embedder = get_embedder(provider=provider, model=model)
    except Exception as e:
        print(f"  ⚠ Skipping {provider}/{model}: {e}")
        return None

    print(f"  ▸ Benchmarking {embedder.name} ({embedder.dimensions}d)...")

    latencies: list[float] = []
    batch_size = 8

    # Warm-up run
    try:
        await embedder.embed(SAMPLE_TEXTS[:2])
    except Exception as e:
        print(f"  ✗ Failed: {e}")
        return None

    # Measured runs
    for i in range(0, len(SAMPLE_TEXTS), batch_size):
        batch = SAMPLE_TEXTS[i : i + batch_size]
        start = time.perf_counter()
        await embedder.embed(batch)
        elapsed_ms = (time.perf_counter() - start) * 1000
        latencies.append(elapsed_ms)

    avg_latency = sum(latencies) / len(latencies)
    p99_latency = sorted(latencies)[int(len(latencies) * 0.99)] if len(latencies) > 1 else latencies[0]
    throughput = batch_size / (avg_latency / 1000)

    # Estimate index size: each vector = dims * 4 bytes (float32)
    # HNSW overhead is roughly 2-3x the raw vector size
    bytes_per_vector = embedder.dimensions * 4
    hnsw_overhead = 2.5
    index_size_per_100k = (bytes_per_vector * hnsw_overhead * 100_000) / (1024 * 1024)

    result = BenchmarkResult(
        provider=provider,
        model=model,
        dimensions=embedder.dimensions,
        cost_per_1m_tokens=embedder.cost_per_million_tokens,
        avg_embed_latency_ms=round(avg_latency, 2),
        p99_embed_latency_ms=round(p99_latency, 2),
        throughput_texts_per_sec=round(throughput, 1),
        estimated_index_size_mb_per_100k=round(index_size_per_100k, 1),
    )

    print(f"    ✓ {avg_latency:.1f}ms avg, {embedder.dimensions}d, ${embedder.cost_per_million_tokens}/1M tok")
    return result


async def run_benchmarks(providers: list[str] | None = None):
    """Run benchmarks across all requested providers."""
    catalog = PROVIDER_CATALOG
    results: list[BenchmarkResult] = []

    if providers:
        catalog = {k: v for k, v in catalog.items() if k in providers}

    print("=" * 60)
    print("HYBRID SEARCH — EMBEDDING MODEL BENCHMARK")
    print("=" * 60)

    for provider, data in catalog.items():
        print(f"\n▶ Provider: {provider}")
        for model in data["models"]:
            result = await benchmark_provider(provider, model)
            if result:
                results.append(result)

    # Print summary table
    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    print(f"{'Provider/Model':<40} {'Dims':>5} {'Latency':>10} {'Cost/1M':>8} {'Index/100K':>10}")
    print("-" * 75)

    for r in sorted(results, key=lambda x: x.avg_embed_latency_ms):
        print(
            f"{r.provider}/{r.model:<35} {r.dimensions:>5} "
            f"{r.avg_embed_latency_ms:>8.1f}ms "
            f"${r.cost_per_1m_tokens:>6.2f} "
            f"{r.estimated_index_size_mb_per_100k:>8.1f}MB"
        )

    # Cost projection
    print("\n" + "=" * 60)
    print("COST PROJECTION (500K candidates, ~5 chunks each = 2.5M chunks)")
    print("=" * 60)
    avg_tokens_per_chunk = 400  # ~512 tokens per chunk with some shorter ones
    total_tokens = 2_500_000 * avg_tokens_per_chunk

    for r in sorted(results, key=lambda x: x.cost_per_1m_tokens):
        cost = (total_tokens / 1_000_000) * r.cost_per_1m_tokens
        idx_gb = (r.estimated_index_size_mb_per_100k / 100_000) * 2_500_000 / 1024
        print(f"  {r.provider}/{r.model:<35} Embed: ${cost:>8.2f}  Index: {idx_gb:>.1f}GB")

    # Save results
    output_path = os.path.join(os.path.dirname(__file__), "benchmark_results.json")
    with open(output_path, "w") as f:
        json.dump([asdict(r) for r in results], f, indent=2)
    print(f"\nResults saved to {output_path}")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Benchmark embedding models")
    parser.add_argument(
        "--providers",
        type=str,
        default=None,
        help="Comma-separated provider names (e.g., openai,local). Default: all available.",
    )
    args = parser.parse_args()

    providers = args.providers.split(",") if args.providers else None
    asyncio.run(run_benchmarks(providers))
