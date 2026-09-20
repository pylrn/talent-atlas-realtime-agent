# Scaling & Pricing Reference

A single-page model of **what the system costs to run today** and **what changes as it scales**. Pulled from [docs/pricing.md](pricing.md), [docs/toggle.md](toggle.md), [docs/how_it_works.md](how_it_works.md), [docs/storage.md](storage.md), [pipeline/embedder.py](../pipeline/embedder.py), and the SQL migrations.

Updated: 2026-05-24

> **Status of numbers:** LLM and embedding prices, latency benchmarks, and storage shapes come from the codebase. Cloud-infrastructure prices are best-effort May 2026 list prices for AWS/GCP equivalents and are clearly marked as estimates. Always re-verify before capacity planning.

---

## 1. TL;DR

| Tier | Candidates | Recruiters | QPS (peak) | Infra cost (est. /mo) | Per-search cost | Single-search latency (quality mode) |
|---|---:|---:|---:|---:|---:|---:|
| **Local / dev** | < 5k | 1 | < 0.1 | $0 (laptop) | $0 (cache-warm) → $0.000264 (cache-miss) | 0.9–1.5 s |
| **Small deploy** | 10k | 5–20 | 1–2 | ~$45 (1× small VPS + Postgres on same box) | ~$0.00015 effective (50% cache hit) | 1.0–1.5 s |
| **Medium SaaS** | 100k | 100 | 5–20 | ~$300–700 (managed Postgres + 2× app nodes) | ~$0.00010 (60% cache hit) | 1.0–1.8 s |
| **Large SaaS** | 1M+ | 1000+ | 50–200+ | ~$3k–10k (HA Postgres + auto-scale app + Redis + GPU rerank) | ~$0.00006 (70% cache hit, bulk LLM discount) | 1.2–2.2 s |

Everything below explains how those numbers come together and where the cliffs are.

---

## 2. Per-search cost breakdown

Every request walks the 20-step pipeline in [docs/how_it_works.md](how_it_works.md). The cost sources are:

| Stage | What it costs | When it costs |
|---|---|---|
| 4. LLM planner | $0.000110–$0.001281 per call (Groq) | Only on cache miss |
| 5. Sanitizer/validator/normalizer | $0 (pure Python) | Every request |
| 6. HyDE profile generation | Included in planner output (no separate call) | JD-mode only |
| 10. SQL filter | Postgres CPU | Every request |
| 11a. Dense retrieval | Embedding call + pgvector HNSW lookup | Every search (cache hit avoids embed) |
| 11b. BM25 retrieval | Postgres GIN scan | Every search |
| 11c. Skill exact | Postgres GIN scan on `candidates.skills` | Every search |
| 14. Cross-encoder rerank | ~50 ms CPU per 100 candidates | Quality / explanation modes only |
| 15. Feature ranker | <5 ms in-process | Every search |
| 16. MMR | <2 ms in-process | Every search |
| 17. Explanation | <2 ms in-process | Every search |
| 20. Impression log | 1 INSERT per displayed row (async, non-blocking) | Every search |

### 2.1 LLM planner cost (from [docs/pricing.md](pricing.md))

Token budget per search: ~2,000 input / ~128 output tokens.

| Model | $/search | Searches/$ | Latency | Recommendation |
|---|---:|---:|---:|---|
| `llama-3.1-8b-instant` | $0.000110 | ~9,070 | 14,280 ms avg | **Avoid** — thinking-mode spikes |
| `llama-4-scout-17b-16e-instruct` | **$0.000264** | **~3,790** | **902 ms** | **Default** — perfect score, sub-1 s |
| `qwen/qwen3-32b` | $0.000656 | ~1,525 | 16,701 ms | Avoid — quality + latency issues |
| `llama-3.3-70b-versatile` | $0.001281 | ~781 | 2,731 ms | Use as accuracy fallback |

A warm `plan:` cache (TTL 24 h) means the **effective** per-search LLM cost drops by the cache-hit rate. A 50% hit rate ≈ $0.000132/search at the recommended model.

### 2.2 Embedding cost — per provider ([pipeline/embedder.py](../pipeline/embedder.py))

Local embeddings (default) cost $0 per call but require model weights on disk and CPU/GPU at inference. Remote providers price per million tokens:

| Provider / model | Dims | $/M tokens | Storage per chunk | Notes |
|---|---:|---:|---:|---|
| `local` `all-MiniLM-L6-v2` | 384 | **$0.00** | ~1.5 KB | Default — matches `VECTOR(384)` schema |
| `gemini` `text-embedding-004` | 768 | $0.00 (free tier) | ~3 KB | Larger vectors → larger HNSW |
| `jina` `jina-embeddings-v3` | 1024 | $0.02 | ~4 KB | |
| `voyage` `voyage-3-lite` | 512 | $0.02 | ~2 KB | |
| `voyage` `voyage-3` | 1024 | $0.06 | ~4 KB | |
| `cohere` `embed-english-v3.0` | 1024 | $0.10 | ~4 KB | |
| `openai` `text-embedding-3-small` | 1536 | $0.02 | ~6 KB | |
| `openai` `text-embedding-3-large` | 3072 | $0.13 | ~12 KB | |

> A query embed is ~30 tokens. Embedding 1M queries on Voyage-3 ≈ $1.80. Embedding 1M chunks of ~400 tokens averages ≈ $24 on Voyage-3, $0 on local.

The `embed:` cache (TTL 30 d) means repeat semantic queries skip this entirely.

### 2.3 Cross-encoder rerank

- Default model: `ms-marco-MiniLM-L-6-v2` (local CrossEncoder).
- Cost: **~50 ms CPU** for `cross_encoder_top_k = 100` pairs ([docs/how_it_works.md §184](how_it_works.md)).
- On a single CPU core this caps you at ~20 req/s/core for quality mode. GPU offload (T4 / L4) drops it to ~5 ms and removes the cap.
- `fast` mode disables this stage → saves ~50 ms and ~80% of per-query CPU.

### 2.4 Database query cost

| Query | Index | Cost class |
|---|---|---|
| Structured filter | B-tree / GIN on `candidates` | O(log N) — millisecond-level on 1M rows |
| Dense ANN | HNSW (`m=16, ef_construction=200`, `HNSW_EF_SEARCH=40`) on `document_chunks.embedding` | O(log N) — single-digit ms up to ~10M chunks before tuning |
| BM25 | GIN on `content_tsv` | O(matching docs) — usually <30 ms |
| Skill exact | GIN on `candidates.skills` | <10 ms |

---

## 3. Mode cost & latency matrix

From [docs/toggle.md](toggle.md) and [docs/how_it_works.md §299](how_it_works.md):

| Mode | CE rerank | `final_top_k` | `dense/bm25_top_k` | Typical latency (warm) | Per-search infra cost | Use case |
|---|---|---:|---:|---:|---:|---|
| `fast` | OFF | 25 | 100 / 100 | **300–500 ms** | $0 (no LLM call possible; local infra only) when cache-warm; +$0.000264 on LLM cache miss | Autocomplete, mobile, internal search-as-you-type |
| `quality` | ON | 25 | 200 / 200 | **900–1500 ms** | Same as fast + ~50 ms CE | Default UI |
| `explanation` | ON | 10 | 200 / 200 | **900–1800 ms** | Same as quality | Side panel deep-dive |
| `quality+overrides` (`dense=500, bm25=500, ce=200`) | ON | 25 | 500 / 500 | 1.5–2.5 s | Same model cost; ~3× DB work | Recall-critical eval runs |
| `LLM-free` (`use_llm_planner=False`) | ON | 25 | 200 / 200 | 400–700 ms | **$0 LLM** (regex planner) | Cost-sensitive batch / fallback when LLM provider is down |

Filter-only requests (no `query` text) skip retrieval entirely and are effectively a SQL browse — **~50–150 ms** regardless of mode.

---

## 4. Storage footprint as a function of corpus size

Schema in [docs/storage.md](storage.md). Approximations below assume an average candidate has 1 resume of ~3000 words.

### 4.1 Per-row sizing

| Object | Approx bytes | Source |
|---|---:|---|
| 1 `candidates` row | ~1 KB | Metadata + skills/interests TEXT[] |
| 1 `candidate_documents` row | ~10–20 KB | Mostly `raw_text` |
| 1 `document_chunks` row (text + tsvector + 384-dim vector + metadata) | ~5 KB | `content` ~2 KB + `content_tsv` ~1 KB + `embedding` 1.5 KB + overhead |
| HNSW overhead per vector | ~1.2× the vector itself | `m = 16` graph |
| GIN index overhead | ~1.3× the indexed column | Postgres typical |
| 1 `search_impressions` row | ~120 B | Small, with FK indexes |
| 1 `search_outcomes` row | ~80 B | |

### 4.2 Projected DB size

Assumes ~10 chunks/document, 1 doc/candidate, ~20% headroom for indexes + WAL.

| Candidates | Chunks | Raw data | With indexes (HNSW + GIN) | Effective DB size |
|---:|---:|---:|---:|---:|
| 1,000 | 10k | ~70 MB | ~110 MB | **~150 MB** |
| 10,000 | 100k | ~700 MB | ~1.1 GB | **~1.5 GB** |
| 100,000 | 1M | ~7 GB | ~11 GB | **~15 GB** |
| 1,000,000 | 10M | ~70 GB | ~110 GB | **~150 GB** |
| 10,000,000 | 100M | ~700 GB | ~1.1 TB | **~1.5 TB** |

> Switching to `text-embedding-3-large` (3072 dims) multiplies the chunk vector size ~8× and roughly doubles total DB size at scale. Picking a 384-dim embedder is a meaningful infra-cost decision.

### 4.3 Other persistent stores

| Store | Default size | Lifetime |
|---|---:|---|
| In-process LRU cache | 2048 entries × ~5 KB ≈ **10 MB** RAM | Process lifetime |
| Reranker / embedding model weights | ~100 MB each (MiniLM-L6) | Disk, HF cache |
| `.env` | <4 KB | Disk |
| `reports/*.json/html` | Single-digit MB per benchmark | Disk |

A future Redis-backed cache (mentioned in `implementation_plan.md` §0.4) would replace the LRU with a managed store; sizing is the same.

---

## 5. Latency budget — quality mode, warm

End-to-end p50 ≈ 1.0–1.5 s breaks down approximately as:

| Stage | p50 ms | Notes |
|---|---:|---|
| Network + FastAPI overhead | 10 | Local |
| Route + sanitize + cache lookup | 5 | In-process |
| LLM planner (cache miss) | **900** | Llama-4-Scout default; 0 on cache hit |
| Validator + normalizer | 5 | |
| Query embed (cache miss) | 30 | Local MiniLM; ~5 ms cache hit |
| SQL filter | 15 | Indexed |
| Dense ANN (HNSW) | 20 | At 1M chunks |
| BM25 | 25 | GIN scan |
| Skill exact | 5 | |
| RRF fusion + grouping | 5 | |
| Cross-encoder rerank | **50** | CPU; ~5 ms on GPU |
| Feature ranker + MMR + explanation | 10 | |
| Impression log enqueue | <1 | Async |
| **Total, cache miss** | **~1080 ms** | |
| **Total, cache hit** | **~170 ms** | LLM and embed both cached |

> Numbers from `docs/pricing.md` (planner latency), `docs/how_it_works.md §184` (CE), `docs/storage.md §186` (cache hit cost). DB rows are reasonable estimates that hold up to ~10M chunks.

`fast` mode strips CE and reduces top-Ks — expect ~300 ms warm.

---

## 6. Deployment topologies

### 6.1 Current state — local laptop (where it lives today)

```
┌───────────────────────────────────────────────────────────┐
│ macOS host                                                │
│ ┌───────────────┐  ┌───────────────┐  ┌──────────────┐   │
│ │ FastAPI       │  │ Docker:       │  │ HF model     │   │
│ │ + uvicorn     │──│  pgvector/pg17│  │ cache        │   │
│ │ + LRU cache   │  │  + pgbouncer  │  │ ~/.cache/hf  │   │
│ └───────────────┘  └───────────────┘  └──────────────┘   │
└───────────────────────────────────────────────────────────┘
```

- **Cost:** $0 infra. Optional Groq spend at $0.000264/search (cache-aware).
- **Capacity:** comfortably handles 1 user, < 10k candidates, < 1 QPS.
- **Persistence:** Docker volume `pgdata`. Loss on `docker volume rm`.
- **Failure modes:** single point of everything; no redundancy; cold-start latency on first request after restart (model load + DB cache warm).

### 6.2 Small deploy — single VPS

```
┌──────────────────────────────────────────────────┐
│ 1× e.g. Hetzner CX31 (4 vCPU / 8 GB / 80 GB SSD) │
│  - FastAPI + uvicorn (2 workers)                 │
│  - Postgres 17 + pgvector (same host)            │
│  - Local LRU cache                               │
└──────────────────────────────────────────────────┘
```

- **Cost (est.):** $25–40/mo compute, $5 backups, **~$30–45/mo total**.
- **LLM cost (est.):** 10k searches/mo × $0.000132 (50% cache hit) ≈ **$1.32/mo**.
- **Capacity:** 10k candidates / 100k chunks / 1–2 sustained QPS.
- **Limits before re-architecting:**
  - DB and app fighting for RAM > 10k candidates with rerank-heavy traffic.
  - Single worker can't run two CE batches concurrently — second request queues.
  - No HA. Restart = downtime.

### 6.3 Medium SaaS — split app and DB

```
┌────────────────┐    ┌───────────────────────────┐
│ 2× app nodes   │    │ Managed Postgres          │
│ (FastAPI +     │────│  (e.g. RDS db.t4g.medium, │
│  uvicorn +     │    │   100 GB gp3)             │
│  LRU cache)    │    └───────────────────────────┘
└────────────────┘
        │
        ▼
   ALB / NLB
```

- **Cost (est., AWS list prices May 2026):**
  - 2× t3.medium app ≈ $60/mo
  - RDS db.t4g.medium + 100 GB gp3 ≈ $80–110/mo
  - ALB ≈ $20/mo
  - Egress / CloudWatch ≈ $20–40/mo
  - **~$200–250/mo infra**
- **LLM cost (est.):** 200k searches/mo × $0.0001 effective ≈ **$20/mo**.
- **Capacity:** 100k candidates / 1M chunks, 5–20 QPS sustained.
- **Limits & fixes:**
  - LRU cache is per-app-node → cache-hit rate drops with more replicas. **Fix:** add Redis (the fallback already scoped in `implementation_plan.md` §0.4) shared across nodes.
  - Cross-encoder on CPU becomes the bottleneck at 10+ QPS. **Fix:** pin one node to GPU (e.g. g5.xlarge) and route quality/explanation traffic there.
  - HNSW build/maintain on 1M+ chunks needs `maintenance_work_mem` bump and partial-index rebuilds during ingest.

### 6.4 Large SaaS — fully separated

```
┌─────────────────────────────┐
│ Auto-scaled app tier        │
│ (5–20 FastAPI pods,         │
│  CPU-only, stateless)       │
└──────────┬──────────────────┘
           │
           ▼
┌─────────────────────────────┐    ┌──────────────────┐
│ Redis (cache:               │    │ GPU rerank pool  │
│  plan / hyde / embed)       │    │ (1–4 L4 / T4)    │
└─────────────────────────────┘    └──────────────────┘
           │
           ▼
┌─────────────────────────────┐
│ Postgres primary + replicas │
│  (e.g. db.r6g.2xlarge,      │
│   500 GB–2 TB io2)          │
│  pgbouncer in front         │
└─────────────────────────────┘
```

- **Cost (est., AWS-style):**
  - App tier: 5–20 m6i.large ≈ $400–1,600/mo
  - GPU rerank: 1–2 g5.xlarge ≈ $750–1,500/mo (or ECS Inferentia for cheaper)
  - Postgres primary + 1 read replica (r6g.2xlarge + 1 TB io2) ≈ $1,500–2,500/mo
  - Redis (cache.t4g.small cluster) ≈ $50–150/mo
  - PgBouncer task + ALB + CloudWatch + S3 backups ≈ $200–500/mo
  - **~$3,000–6,500/mo baseline** before traffic spikes
- **LLM cost (est.):** 5M searches/mo × $0.00006 (70% cache hit, possibly batch tier) ≈ **$300/mo**.
- **Capacity:** 1M+ candidates / 10M+ chunks, 50–200+ QPS sustained.
- **Limits & fixes:**
  - Single Postgres write primary becomes the ceiling for ingest. **Fix:** route reads to replicas; partition `document_chunks` by `candidate_id` hash or `created_at`.
  - HNSW recall degrades past ~10–20M vectors with default `m=16`. **Fix:** bump `m` (rebuild cost) or shard the chunks table; alternative: switch retrieval to a dedicated vector DB (Qdrant / Vespa) only if Postgres truly bottlenecks.
  - Impression-log inserts at 200 QPS × 25 results = 5k writes/s. **Fix:** batch into a queue (Kinesis / SQS) and bulk-insert.
  - Reranker GPU is now the dominant infra cost. **Fix:** distill to a smaller CE, or push reranking to a serverless GPU endpoint and only invoke for paid tiers.

---

## 7. Cost-driver cheat sheet

If a budget is being blown, the line item is almost certainly one of:

| Driver | What blows it up | First lever |
|---|---|---|
| **LLM planner** | Low cache-hit rate, expensive model | Switch to llama-4-scout, tune `PLANNER_VERSION` discipline so you don't invalidate the cache needlessly |
| **Remote embeddings** | Switched off `local` provider | Move query/ingest embeds back to `local`; pay only for capability you need |
| **Cross-encoder CPU** | Quality mode at high QPS | Route latency-sensitive paths to `fast` mode; move CE to GPU for the rest |
| **DB storage** | Bigger embedding dims, no chunk-size discipline | Stay at 384 dims unless quality demands more; tune `CHUNK_SIZE`/`CHUNK_OVERLAP` |
| **DB I/O** | Missing indexes after migration changes | Confirm HNSW + GIN indexes from [003_indexes.sql](../db/migrations/003_indexes.sql) actually exist; `EXPLAIN` slow queries |
| **Impression write load** | Per-result INSERT at high QPS | Batch into a queue before persisting |

---

## 8. Knobs that move cost and latency directly

| Knob (default) | Cost effect | Latency effect |
|---|---|---|
| `use_cache` (on) | Largest single lever — cuts LLM and embed bills | Cache-hit path ~50 ms vs ~900 ms cold |
| `use_llm_planner` (on) | Disable → $0 LLM cost, lower quality | Saves ~900 ms per cache miss |
| `use_cross_encoder` (on) | Disable → frees a CPU core | Saves ~50 ms per request |
| `dense_top_k`, `bm25_top_k` (200) | Bigger pool → more DB + CE work | +5–15 ms per +100 |
| `cross_encoder_top_k` (100) | Heaviest knob for CE cost | Linear in top_k |
| `EMBEDDING_MODEL` / dims | Vector size & DB footprint × N chunks | Larger dims = slower ANN |
| `HNSW_EF_SEARCH` (40) | No cost change | Higher = better recall, slower lookup |
| `final_top_k` (25) | More impression writes | Negligible per request |
| `PLANNER_VERSION` bump | Invalidates `plan:` cache → temporary spike | Cold cache for ~24 h |

See [docs/toggle.md §415](toggle.md) for the full numeric-knob table.

---

## 9. What still needs work to scale cleanly

Honest gaps the current code/docs flag:

1. **Cache is in-process only.** Multi-node deploys lose hit-rate until the optional Redis backing (scoped in `implementation_plan.md` §0.4, not yet implemented) lands.
2. **No batch / async ingest pipeline.** Ingestion is synchronous via the API ([docs/storage.md §263](storage.md)). Bulk imports of >100k documents will need a worker / queue.
3. **No long-term log store.** Observability is in-process counters + `/metrics` ([docs/how_it_works.md §292](how_it_works.md)). For a real SaaS you'd want Prometheus + a log sink.
4. **Reranker on CPU.** Fine to single-digit QPS, painful past 10 QPS quality-mode.
5. **HNSW maintenance.** Default `m=16, ef_construction=200` is good for the current scale. At 10M+ vectors expect to revisit.
6. **PgBouncer is optional today.** At the medium tier it becomes required to keep connection count predictable behind multiple app replicas.

---

## Source documents

- [docs/pricing.md](pricing.md) — Groq token + latency benchmarks
- [docs/toggle.md](toggle.md) — modes, numeric knobs, common recipes
- [docs/how_it_works.md](how_it_works.md) — 20-step pipeline + observability + modes table
- [docs/storage.md](storage.md) — what is persisted and where
- [pipeline/embedder.py](../pipeline/embedder.py) — per-provider model + cost table
- [pipeline/config.py](../pipeline/config.py) and [pipeline/modes.py](../pipeline/modes.py) — `SEARCH_CONFIG` and the three named modes
- [db/migrations/](../db/migrations/) — schema and indexes

---

## 10. Glossary — terms used above, explained from scratch

If anything above used words like "worker", "vCPU", "PgBouncer" without explaining them, this section is the long-form version. **Use the coffee-shop analogy throughout.**

### 10.1 Worker

A **worker** = one barista. It's a copy of your Python program running on the computer.

- 1 worker = 1 barista. Can only make 1 coffee at a time. Customers queue.
- 4 workers = 4 baristas working in parallel. 4 customers get coffee simultaneously.

Why you need more than one: Python has a rule called the **GIL** (Global Interpreter Lock) — *one Python program can only execute one Python instruction at a time, even if the computer has 8 CPU cores*. So if you have 4 CPU cores but only 1 Python worker, you're using 25% of your computer. Running 4 workers uses all 4 cores.

The command is literally:

```bash
uvicorn api.main:app --workers 4
```

That spawns 4 baristas. Each is identical, each has its own copy of the embedding model and reranker in memory. The load balancer (Uvicorn itself, or Fly) hands incoming requests to whichever barista is free.

**Rule of thumb:** workers = number of CPU cores on the box.

### 10.2 Box / node / vCPU / "shared CPU"

A "box" / "node" / "instance" / "server" — all the same thing. **One computer running in a data center somewhere.**

- **CPU core** = a real physical thing on a chip. Like a real burner on a stove.
- **vCPU** = "virtual CPU" = a slice of a real core the cloud company sells you. They cut a 16-core chip into 16 vCPUs and sell each one to a different customer.
- **Shared-cpu** (Fly's term) = "your vCPU lives on the same physical core as someone else's vCPU." Cheaper, slightly slower, occasionally laggy if your neighbor is busy.
- **Dedicated** (or "performance") = "you own this physical core, nobody else touches it." Costs ~3× more, predictable.

So `shared-cpu-4x` on Fly = **one box with 4 vCPUs, sharing physical cores with other customers.** ~$60/mo. Good enough for almost everything.

"3× shared-cpu-4x" means **three separate computers**, each with 4 vCPUs. Load balancer in front sprays requests across them. If one dies, the other two keep working.

### 10.3 How it fits together

```
                          customers (requests)
                                 │
                          ┌──────▼──────┐
                          │ Load balancer│  ← receptionist directing traffic
                          └──┬───┬───┬───┘
                  ┌──────────┘   │   └──────────┐
            ┌─────▼─────┐  ┌─────▼─────┐  ┌─────▼─────┐
            │   Box 1   │  │   Box 2   │  │   Box 3   │   ← 3 separate computers
            │  4 vCPU   │  │  4 vCPU   │  │  4 vCPU   │
            │           │  │           │  │           │
            │ 4 workers │  │ 4 workers │  │ 4 workers │   ← 4 baristas per box = 12 total
            └─────┬─────┘  └─────┬─────┘  └─────┬─────┘
                  └──────────────┼──────────────┘
                                 │
                       ┌─────────▼─────────┐
                       │     Postgres      │   ← shared database
                       └───────────────────┘
```

So **3 boxes × 4 workers = 12 baristas making coffee in parallel.** That's your real concurrency ceiling.

### 10.4 PgBouncer — what it actually does

**The problem:** Postgres has a hard limit on how many programs can connect to it at once. Default is **100 connections**. After that, new connection attempts get rejected — Postgres just slams the door.

Each Python worker wants to keep ~5 connections open to the database (so it doesn't have to reopen them on every query, which is slow). So:

```
12 workers × 5 connections each = 60 connections   ✓ fits in 100
```

OK at this scale. But grow to 6 boxes:

```
24 workers × 5 = 120 connections   ✗ Postgres rejects everything past 100
```

And the app dies in a really confusing way.

**PgBouncer solution:** put a middleman between the app and Postgres.

```
                    your apps (120 connections to PgBouncer)
                              │
                              ▼
                       ┌─────────────┐
                       │  PgBouncer  │   ← keeps only ~10–20 real connections open to Postgres
                       └─────┬───────┘
                             ▼
                       ┌─────────────┐
                       │  Postgres   │
                       └─────────────┘
```

How it works: PgBouncer accepts 1000 connections from your app but only opens, say, 15 real connections to Postgres. When your app sends a query, PgBouncer grabs a free Postgres connection, runs the query, returns the result, and immediately gives that connection back to the pool. Most app connections are idle most of the time, so 15 real ones cover 1000 fake ones easily.

**You don't install PgBouncer yourself with Neon.** Neon gives you two connection strings:

- One ends in `.neon.tech` (direct — for migrations, etc.)
- One ends in `-pooler.neon.tech` (pooled — what your app uses)

You literally just use the pooled URL. PgBouncer is already running, you don't see it.

**When do you need it?** Once you have **more than ~8 workers total across your cluster**, switch to the pooled connection string. Below that, direct is fine.

### 10.5 GPU decision tree

The cross-encoder reranker is the only thing that ever benefits from a GPU. It takes **~50 ms per request on a CPU core**. On a GPU it'd take ~5 ms. The question is whether 50 ms per request is too slow for your traffic.

**Dumb-simple math:**

- 1 CPU core can do **~12 quality-mode reranks per second** (1000 ms ÷ 50 ms, minus overhead).
- Your total CPU rerank capacity = `(boxes × vCPUs) × 12`.
- Compare that to your **peak QPS in quality mode**.

| Your peak quality-mode QPS | Action |
|---|---|
| **< 50% of cluster capacity** | CPU. Don't think about it. |
| **50–80% of capacity** | CPU, but watch p95 latency. Consider dropping `cross_encoder_top_k` from 100 → 50. |
| **80–100% of capacity** | Add another box first. GPU only if adding boxes is more expensive than a GPU. |
| **> 100% (queueing forms)** | Either add boxes OR move reranker to GPU. |

#### Plugged in for the two reference deployments

**10k candidates / 50 recruiters:**
- Cluster: 1 box × 4 vCPU = 4 cores × 12 = **48 reranks/sec ceiling**
- Peak QPS: ~5–8
- Headroom: 6–10×
- **GPU: no, never, not close.**

**80k candidates / 300 recruiters:**
- Cluster: 3 boxes × 4 vCPU = 12 cores × 12 = **~140 reranks/sec ceiling**
- Peak QPS: ~30–50
- Headroom: 3–5×
- **GPU: no.**

**When GPU would actually matter:**

- 80k tier grows to **~1,000 recruiters** (~150 peak QPS) → cluster saturated, add boxes or GPU
- You want **sub-500 ms p95 latency** under load (GPU rerank cuts ~45 ms off every request)
- You're paying for **8+ app boxes just to handle rerank** — at that point one GPU box is cheaper

### 10.6 TL;DR with the analogy

- **Worker** = one barista (a Python process). You want as many baristas as you have stove burners (CPUs).
- **Box / node / vCPU** = the kitchen. Bigger kitchen = more burners = more baristas.
- **Shared CPU** = renting a burner that you share with the kitchen next door. Cheaper but neighbor noise.
- **Multiple boxes** = multiple kitchens with a receptionist (load balancer) sending customers to whichever is least busy.
- **PgBouncer** = the receptionist at the warehouse (Postgres) who turns "1000 baristas asking for ingredients" into "15 actual people walking into the warehouse." Without it, the warehouse locks the door at 100 visitors.
- **GPU** = an industrial espresso machine. Only buy one when normal baristas can't keep up — and you won't hit that until you have **~10× more recruiters than the 80k/300 scenario**.
