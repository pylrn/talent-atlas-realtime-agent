# DigitalOcean Capacity Reference

What this exact DigitalOcean deployment of the hybrid-search app handles: concurrency, sustained QPS, peak QPS, latency, connection pooling, and where the cliffs are. Sized for the 80k-candidate / 300-recruiter tier from [docs/scaling.md §6.3](scaling.md).

Updated: 2026-05-24

> All numbers are derived from the architecture in [pipeline/search.py](../pipeline/search.py), latency from [docs/pricing.md](pricing.md) + [docs/how_it_works.md](how_it_works.md), and DigitalOcean's published per-plan specs. Cloud prices are list as of May 2026.

---

## 1. The stack

| Layer | Pick | Spec | Monthly |
|---|---|---|---:|
| App boxes | **3× Droplets** (Regular Intel, `s-4vcpu-8gb`) | 4 vCPU / 8 GB / 160 GB SSD / 5 TB transfer | ~$144 |
| Load balancer | **DO Managed Load Balancer** (small) | HTTPS termination, health checks | ~$12 |
| Database | **DO Managed PostgreSQL** (`db-s-2vcpu-4gb`) | 2 vCPU / 4 GB / 38 GB storage, pgvector extension | ~$60 |
| Connection pooler | Built into DO Managed PG | PgBouncer, transaction mode | (included) |
| Cache | **DO Managed Redis** (basic 1 GB) | 1 GB RAM | ~$15 |
| Blob storage | **DO Spaces** (S3-compatible) | 250 GB included, 1 TB outbound | ~$5 |
| LLM | Groq API (external) | `llama-4-scout-17b-16e-instruct` | usage |
| Outbound bandwidth | included with Droplets | 5 TB × 3 = 15 TB pooled | $0 |
| | | **Infra total** | **~$236/mo** |

Plus Groq (~$140/mo at worst-case traffic) → **~$376/mo all-in**.

---

## 2. The concurrency model

Each Droplet runs Uvicorn with **`--workers 4`** (one worker per vCPU). Each worker is a separate Python process with its own:

- Event loop (asyncio)
- Embedding model in RAM (~90 MB)
- Cross-encoder reranker in RAM (~80 MB)
- L1 LRU cache (in-process dict, ~10 MB)
- asyncpg connection pool (default 5 connections)

```
3 Droplets × 4 workers = 12 worker processes cluster-wide
```

Each worker can hold **hundreds of in-flight requests** simultaneously — most are parked on an `await` waiting for Groq, Postgres, or Redis to respond. Only one request per worker is actively executing Python code at any instant (Python GIL).

**The implication:** I/O-heavy work scales beautifully (network waits overlap), CPU-heavy work serializes per worker (reranker, embedding).

### Why 12 workers, not 24 or 100

| Worker count per box | Outcome |
|---:|---|
| 1 | Wastes 3 of 4 CPUs |
| 2 | Wastes ~half the CPU |
| **4** | **Matches vCPU count — full CPU utilization** |
| 8+ | More context switching, no extra throughput, more model duplication in RAM |

This is the universal Python web-app rule: workers = vCPUs on the box.

---

## 3. Connection management — the silent failure mode

This is the thing that kills most multi-box Postgres deployments. Worth being explicit.

### The naive math

```
12 workers × 5 connections each = 60 connections to Postgres
```

### DO Managed Postgres connection limits (per plan)

| Plan | Max connections |
|---|---:|
| `db-s-1vcpu-1gb` ($15) | 22 |
| `db-s-1vcpu-2gb` ($25) | 25 |
| **`db-s-2vcpu-4gb` ($60)** | **47** ← what you picked |
| `db-s-4vcpu-8gb` ($120) | 97 |
| `db-s-6vcpu-16gb` ($240) | 197 |

Your 60 desired connections **exceed the 47 limit** on the `$60` plan. Without pooling, the app would intermittently refuse connections and you'd chase a confusing bug.

### How DO's built-in pooler fixes it

DO Managed Postgres ships PgBouncer transparently. You enable it in the database dashboard (Settings → Connection pools → Create), pick **transaction-mode pooling**, set **pool size to ~15**, and use the pooled connection string instead of the direct one.

After that:

```
12 workers × 5 "fake" connections each = 60 connections to PgBouncer  ✓
PgBouncer multiplexes onto ~15 real Postgres connections              ✓
Postgres sees ~15 connections, far under its 47-connection ceiling    ✓
```

Same physical traffic, fits comfortably.

### The DATABASE_URL change

```bash
# Direct (don't use from the app — only for migrations)
postgres://doadmin:pass@db-postgresql-nyc3-xxxxx.b.db.ondigitalocean.com:25060/defaultdb?sslmode=require

# Pooled (use this in the app)
postgres://doadmin:pass@db-postgresql-nyc3-xxxxx-pool.b.db.ondigitalocean.com:25061/defaultdb?sslmode=require
                                                  ^^^^                                ^^^^^
                                            different host                        different port
```

You set `DATABASE_URL` to the pooled one on each Droplet. Migrations use the direct URL.

> Reference: [DO docs on connection pooling](https://docs.digitalocean.com/products/databases/postgresql/how-to/manage-connection-pools/).

---

## 4. QPS ceiling — per-stage math

Each stage of the search pipeline has its own throughput limit. Cluster QPS is set by the **slowest CPU-bound stage**.

| Stage | Type | Time/req | Per-worker QPS | Cluster QPS (×12) |
|---|---|---:|---:|---:|
| Sanitize / route / cache lookup | CPU | ~5 ms | 200 | 2,400 |
| Validate / normalize | CPU | ~5 ms | 200 | 2,400 |
| LLM call (Groq) | I/O wait | ~900 ms | ~1,000* | ~1,000 (Groq-side) |
| Query embedding (local MiniLM) | CPU | ~30 ms | 33 | 400 |
| SQL filter + dense + BM25 + skill | I/O wait | ~50 ms | ~200* | ~2,000 (Postgres-side) |
| RRF fusion / dedupe / group | CPU | ~5 ms | 200 | 2,400 |
| **Cross-encoder rerank** | **CPU** | **~50 ms** | **~12** | **~140** ← bottleneck |
| Feature ranker / MMR / explanation | CPU | ~15 ms | 65 | 780 |
| Impression log | async fire-and-forget | 0 | ∞ | ∞ |

\* I/O stages don't burn worker capacity 1:1 — async parks them. The number is the order-of-magnitude of *concurrent* in-flight waits per worker.

### The bottleneck explained

The cross-encoder is the only stage that's *all three* of: slow, CPU-bound, and per-request (uncacheable across users):

- **50 ms of pure transformer math.** No I/O to overlap.
- **Holds the worker's core** the whole time. PyTorch releases Python's GIL during the matrix multiply, but the underlying CPU is pegged.
- **Different candidates every search.** L1 / L2 caches don't help.
- 1 core processes 1 batch per 50 ms → ~20 reranks/sec theoretical → **~12 reranks/sec** after Python + asyncio overhead.
- 12 workers × ~12 = **~140 QPS sustained ceiling in quality mode**.

### Mode-specific ceilings

| Mode | What's on | Sustained ceiling | Bottleneck stage |
|---|---|---:|---|
| `quality` (default) | LLM + CE + everything | **~140 QPS** | Cross-encoder |
| `fast` | LLM on, CE off | **~400 QPS** | Query embedding |
| `LLM-free` (regex planner, CE on) | CE only | **~140 QPS** | Cross-encoder |
| `LLM-free` + `fast` | only retrieval + ranker | **~400 QPS** | Embedding then Postgres |

### Why 100 pairs in the reranker take 50 ms, not 5 seconds

The cross-encoder doesn't process candidates one at a time. The 100 `(query, candidate_text)` pairs are batched into **one big matrix** and run through a single forward pass. On modern CPU with SIMD this takes ~50 ms total, not 100 × 50 ms = 5 s. (See [pipeline/reranker.py](../pipeline/reranker.py) — calls `cross_encoder.predict(pairs)` once on the whole list.)

If batching were ever broken, you'd see the cluster ceiling drop from 140 QPS to ~14 QPS and latency balloon 10×.

---

## 5. Latency under load

End-to-end response time, by percentile, at different load levels.

### Idle / typical (≤ 30 QPS quality mode, ~25% utilization)

| Percentile | Cold cache | Warm cache |
|---|---:|---:|
| p50 | 1.10 s | 170 ms |
| p75 | 1.25 s | 200 ms |
| p95 | 1.55 s | 380 ms |
| p99 | 2.10 s | 750 ms |
| p99.9 | 4.5 s | 1.5 s |

### Heavy / peak (~80 QPS quality mode, ~60% utilization)

| Percentile | Cold cache | Warm cache |
|---|---:|---:|
| p50 | 1.15 s | 180 ms |
| p75 | 1.40 s | 250 ms |
| p95 | 1.85 s | 600 ms |
| p99 | 2.80 s | 1.2 s |
| p99.9 | 5–6 s | 2.5 s |

### Overload (~130 QPS quality mode, ~93% utilization — don't operate here)

| Percentile | Cold cache | Warm cache |
|---|---:|---:|
| p50 | 1.6 s | 280 ms |
| p95 | 4–5 s | 1.8 s |
| p99 | 7–10 s | 4 s |

Queue formation kicks in past ~75% utilization, then degrades nonlinearly.

### Why p50 is ~1.1 s even when idle

The LLM call dominates. There is **no way to be faster than ~900 ms in quality mode without a cache hit**, because Groq itself takes ~900 ms to produce the plan. The other ~200 ms is everything else combined.

| Component | Contribution to p50 cold |
|---|---:|
| Network + FastAPI overhead | 10 ms |
| Route / sanitize / cache lookup | 5 ms |
| **Groq LLM call** | **900 ms** |
| Validate / normalize | 5 ms |
| Query embed (MiniLM CPU) | 30 ms |
| SQL filter | 15 ms |
| Dense + BM25 + skill (parallel) | 50 ms |
| RRF / group | 5 ms |
| **Cross-encoder rerank** | **50 ms** |
| Feature ranker / MMR / explanation | 15 ms |
| Impression log enqueue | <1 ms |
| **Total** | **~1,085 ms** |

A warm cache skips the Groq call and the embedding → drops p50 to **~170 ms**.

---

## 6. Capacity for the 80k / 300 recruiter target

| Metric | Cluster provides | Your demand at 300 recruiters | Headroom |
|---|---:|---:|---:|
| Sustained quality-mode QPS | 140 | ~5 avg, ~10 busy-hour avg | **~14×** |
| Peak burst (30 sec) | ~250 | ~30–50 | **~5–8×** |
| Simultaneous in-flight requests | 1,000+ | ~50 | **~20×** |
| Postgres connections (pooled) | ~200 effective | ~60 from workers | **~3×** |
| DB query p95 latency | <50 ms | — | safe |
| Open browser sessions | 5,000+ | ~300 | **~16×** |
| DB storage on $60 plan | 38 GB | ~12 GB used | room to grow |

You are sized for the **worst-case 1.32 M searches/mo** with comfortable room for either a hiring blitz or growth toward 500 recruiters before scaling.

---

## 7. How it degrades

The system degrades **gracefully**, not catastrophically. Predictable order of failure:

| Load level | Symptom | What's failing |
|---|---|---|
| 0–50% util | Normal | nothing |
| 50–75% util | p95 starts to creep (1.5 → 2 s) | Reranker queue depth = 1–2 |
| 75–90% util | p95 noticeable (2 → 3 s), p99 doubles | Reranker queue depth = 3–5 |
| 90–100% util | p50 inflates, p99 hits 5+ s | Reranker fully saturated |
| 100–110% util | Requests start timing out (default 30 s) | Some get dropped |
| 110%+ util sustained | Cascading failure: DB connections exhaust, Redis backs up | Total outage in minutes |

**Mitigation built into the app**: nothing automatic. **You need to monitor** p95 latency from the load balancer and add a 4th Droplet when sustained util crosses ~70%.

---

## 8. Scaling levers — cheapest to most disruptive

When your peak inches up, apply these in order:

### Free / single config edit

1. **Drop `cross_encoder_top_k` from 100 → 50** ([pipeline/config.py](../pipeline/config.py)). Roughly doubles cluster ceiling from 140 → 280 QPS. Small accuracy cost.
2. **Route autocomplete / typeahead to `fast` mode.** Those endpoints stop using the CE entirely.
3. **Raise the connection pool size** on DO's PgBouncer config (from 15 → 25) if you see "no connection available" errors.

### Add resources (~$15–60/mo each)

4. **Add a 4th Droplet** (`fly scale count`'s DO equivalent is just creating another Droplet behind the load balancer): +33% capacity, +$48/mo.
5. **Upgrade DO Managed Postgres** to `db-s-4vcpu-8gb` ($120/mo) when DB CPU > 70% sustained.
6. **Upgrade DO Managed Redis** to 4 GB ($30/mo) if cache evictions start showing in metrics.

### Architectural ($300+/mo)

7. **GPU rerank pool** — beyond your tier; only needed past ~1,000 recruiters or sub-500 ms p95 requirement. DO doesn't currently sell GPU droplets, so you'd add a Modal / Replicate / Banana endpoint and call it from the workers.

---

## 9. Connection diagram

```
                     recruiter browsers
                            │
                            │ HTTPS
                            ▼
              ┌─────────────────────────────┐
              │  DO Managed Load Balancer   │   $12
              │  TLS termination + health   │
              └─┬───────────┬───────────┬───┘
                │           │           │
       ┌────────▼─┐  ┌──────▼───┐  ┌────▼─────┐
       │ Droplet 1│  │ Droplet 2│  │ Droplet 3│   3× $48 = $144
       │ 4 vCPU   │  │ 4 vCPU   │  │ 4 vCPU   │
       │ 8 GB RAM │  │ 8 GB RAM │  │ 8 GB RAM │
       │          │  │          │  │          │
       │ Uvicorn  │  │ Uvicorn  │  │ Uvicorn  │
       │ 4 workers│  │ 4 workers│  │ 4 workers│
       │          │  │          │  │          │
       │ MiniLM   │  │ MiniLM   │  │ MiniLM   │   models in RAM
       │ CE       │  │ CE       │  │ CE       │
       │ L1 LRU   │  │ L1 LRU   │  │ L1 LRU   │
       └──┬─────┬─┘  └──┬─────┬─┘  └──┬─────┬─┘
          │     │       │     │       │     │
          │     └───────┼─────┴───────┼─────┘
          │             │             │
          │     ┌───────▼─────────────▼───────┐
          │     │   DO Managed Redis (1 GB)   │   $15  (shared L2 cache)
          │     │   plan: / hyde: / embed:    │
          │     └─────────────────────────────┘
          │
          │ pooled connection string
          ▼
   ┌─────────────────────────────────────────┐
   │   DO Managed PostgreSQL                 │   $60
   │   db-s-2vcpu-4gb                        │
   │  ┌────────────────────────────────────┐ │
   │  │  PgBouncer (DO-managed, included)  │ │
   │  │  60 fake conns in → 15 real out    │ │
   │  └─────────────┬──────────────────────┘ │
   │                ▼                        │
   │  ┌────────────────────────────────────┐ │
   │  │  Postgres 17 + pgvector            │ │
   │  │  ~12 GB used / 38 GB provisioned   │ │
   │  └────────────────────────────────────┘ │
   └─────────────────────────────────────────┘

         External services (outbound HTTPS only):
         ─────────────────────────────────────────
         api.groq.com           LLM planner       usage
         {your-bucket}.digitaloceanspaces.com    $5  (resume PDFs)
```

---

## 10. Per-region latency to managed services

DigitalOcean regions are single-datacenter (no multi-AZ within a region). All managed services and Droplets in the **same region** are typically <2 ms RTT.

| Hop | Typical RTT |
|---|---:|
| Droplet → DO Postgres (same region) | 1–2 ms |
| Droplet → DO Redis (same region) | 1–2 ms |
| Droplet → DO Spaces (same region) | 5–10 ms |
| Droplet → Groq (us-east region) | 20–40 ms (network only; total call ~900 ms) |
| Droplet → Spaces (different region) | 50–150 ms |

**Pick a region all four (Droplets / Postgres / Redis / Spaces) are in, and the same one Groq's closest edge serves.** For US: `nyc3` or `sfo3`. For Europe: `fra1` or `ams3`. For Asia: `sgp1` or `blr1`.

---

## 11. What this setup is NOT sized for

Honest ceilings. Past these, you need different architecture (not just bigger Droplets):

| Workload | Why this setup falls over |
|---|---|
| **2,000+ active recruiters** (~200+ peak QPS quality) | Cross-encoder CPU saturates. Need 6+ Droplets or GPU rerank. |
| **Per-keystroke autocomplete at 100+ active users** | Groq rate limit on your account; need to negotiate or switch to fully cached/regex planner for that endpoint. |
| **Sub-300 ms p95 requirement** | LLM call alone is ~900 ms. Would need to skip planner or self-host a faster LLM. |
| **5+ regions with sub-200 ms p95 in each** | Single-region Postgres becomes the floor. Need read replicas per region (DO doesn't offer cross-region managed read replicas — would have to move to AWS RDS Global, CockroachDB, or Aurora Global). |
| **Massive bulk imports** (>500k documents in one shot) | Synchronous ingest API ties up app workers. Need a background worker tier. |

---

## 12. TL;DR for this stack

- **~$236/mo infra + ~$140/mo Groq = ~$376/mo all-in** at worst-case 1.32 M searches/mo.
- **Cluster ceiling: ~140 QPS quality, ~400 QPS fast.** Bottleneck is cross-encoder on CPU.
- **Your peak: ~30–50 QPS.** Headroom is 3–5× in quality, 8× in fast.
- **Latency p50: ~1.1 s cold, ~170 ms warm.** Dominated by the LLM call.
- **Connections: 60 from workers → 15 to Postgres** via DO's built-in PgBouncer.
- **First scaling lever: drop `cross_encoder_top_k` to 50** — doubles capacity for free.
- **First real upgrade: add a 4th Droplet (+$48/mo)** when sustained util > 70%.
- **Cliff at ~1,000 recruiters** — past that you need GPU rerank, not bigger boxes.

---

## Source documents

- [docs/scaling.md](scaling.md) — full tier breakdown, modes, latency budget
- [docs/storage.md](storage.md) — what is persisted and where
- [docs/pricing.md](pricing.md) — Groq token + latency benchmarks
- [docs/toggle.md](toggle.md) — `SEARCH_CONFIG` knobs and modes
- [docs/how_it_works.md](how_it_works.md) — pipeline step ordering
- [pipeline/cache.py](../pipeline/cache.py), [pipeline/reranker.py](../pipeline/reranker.py), [pipeline/config.py](../pipeline/config.py)
- DigitalOcean: [connection pooling docs](https://docs.digitalocean.com/products/databases/postgresql/how-to/manage-connection-pools/), [Droplet plans](https://www.digitalocean.com/pricing/droplets), [managed PostgreSQL plans](https://www.digitalocean.com/pricing/managed-databases)
