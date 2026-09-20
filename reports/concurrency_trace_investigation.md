# Concurrency Trace Investigation

Source: Langfuse traces for `/search` load-test sessions on 2026-06-19.

## Short Answer

The 75 -> 100 -> 150/250 pattern is not a DB warmup issue. The DB/cache was warm enough. The degradation is mostly connection-pool queueing inside the backend.

Current local settings during the run:

- Primary asyncpg pool max: 10 connections
- Search pool: same as primary pool because `SEARCH_DATABASE_URL` was not set
- Keyword backend in these traces: `fts`, not `paradedb`

So every request was competing for the same 10 DB connections across:

- personalization/profile lookup
- count query
- dense/vector query
- skill-match query
- keyword/FTS branch
- hydrate chunks
- enrich candidates
- background `_log_search_history`

## Reconstructed Batches From Langfuse

| Batch | Run id | Server traces | p50 server latency | p95 server latency | max server latency |
| --- | --- | ---: | ---: | ---: | ---: |
| 50 | `3dd94a01` | 50 | 6.17s | 7.61s | 8.01s |
| 75 | `78c89b80` | 75 | 16.87s | 19.75s | 20.48s |
| 100 | `78c89b80` | 100 | 10.07s | 19.54s | 21.01s |
| 150 | `1a6d6c1b` | 150 | 12.93s | 22.77s | 23.53s |
| 250 | `5e5d6d20` | 250 | 17.57s | 25.02s | 27.08s |
| 500 | `931baa21` | 500 | 55.67s | 75.61s | 86.79s |

The client-side table showed 500 as `162/500` completed because the client timeout was 45s. Langfuse shows the backend kept processing all 500 traces, with many completing after the client had already given up.

## Representative Trace Evidence

### 50-request batch, p50 trace

Trace: `0d3511c71df63191f06ba771e325ded1`

- request: 6.21s
- personalization: 2.23s
- retrieve: 3.53s
- count pool acquire: 2.33s, fetch: 38ms
- dense pool acquire: 2.35s, fetch: 108ms
- skill pool acquire: 2.35s, fetch: 108ms

### 75-request batch, p50 trace

Trace: `df129df016c3fbfe35abb297acc337e1`

- request: 16.87s
- personalization: 7.90s
- retrieve: 7.02s
- count pool acquire: 6.34s, fetch: 207ms
- dense pool acquire: 6.55s, fetch: 255ms
- skill pool acquire: 6.55s, fetch: 139ms
- keyword branch: ~1.05s timeout/guarded FTS path

### 100-request batch, p50 trace

Trace: `11454a2b6a8b21946a8e9ec248e3f5ef`

- request: 10.14s
- personalization: 3.00s
- retrieve: 6.71s
- count pool acquire: 1.58s, fetch: 38ms
- dense pool acquire: 1.58s, fetch: 94ms
- skill pool acquire: 1.58s, fetch: 57ms
- hydrate pool acquire: 4.30s, fetch: 383ms

### 100-request batch, slowest trace

Trace: `4e2555e6729b0f98348c94ff70f6effc`

- request: 21.01s
- personalization: 4.47s
- retrieve: 16.01s
- count pool acquire: 4.04s, fetch: 251ms
- dense pool acquire: 4.04s, fetch: 326ms
- skill pool acquire: 14.91s, fetch: 275ms
- hydrate pool acquire: 242ms, fetch: 412ms

### 250-request batch, p50 trace

Trace: `cf814b56248fb79036b6f1d6d1cc5d50`

- request: 17.57s
- personalization: 2.38s
- retrieve: 12.57s
- count pool acquire: 5.35s, fetch: 44ms
- dense pool acquire: 5.35s, fetch: 73ms
- hydrate pool acquire: 6.92s, fetch: 78ms

### 500-request batch, p50 trace

Trace: `d6484e77ff90b357ced77cf5c5049e47`

- request: 55.83s
- personalization: 7.97s
- retrieve: 44.47s
- count pool acquire: 13.51s, fetch: 42ms
- dense pool acquire: 13.52s, fetch: 121ms
- skill pool acquire: 13.53s, fetch: 68ms
- hydrate pool acquire: 30.29s, fetch: 233ms

## Why 75 Looks Worse Than 100

The 75 and 100 batches were in the same run id (`78c89b80`) but separated by about 20 seconds.

The 75-request batch had a very bad median because many requests paid two queued DB waits:

1. personalization/profile lookup waited several seconds
2. retrieval branches then waited several more seconds

The 100-request batch did not actually become healthy. Its p95/max stayed basically the same as 75. It only had a better p50 because the queueing shifted: many requests got through initial retrieval sooner, while later work such as hydration or some skill branches absorbed the wait. Example: the 100 p50 trace waited 4.3s at hydration, while the 100 slowest trace waited 14.9s in skill-match pool acquire.

So the correct interpretation is:

- 75 is where the DB pool queue clearly becomes dominant.
- 100 is not a recovery; it has a lower median but the tail is still ~20s.
- 150 and 250 are the same queueing regime, with larger waves.
- 500 exceeds the client timeout, and server work continues after clients disconnect.

## Background Task Interference

During the 75/100 window, Langfuse also recorded many `_log_search_history` background tasks.

These use the same primary DB pool via `get_pool()`. They are not counted in the visible request latency once the response returns, but they compete with the next batch.

Observed background history spans in the 14:09:35-14:10:30 UTC window:

- 177 background history traces
- several buckets had p50 background latency in the 2-8s range
- some background history tasks reached ~13s

This means prior batches can leave DB work behind that slows the next batch.

## What To Fix

1. Separate read/search pool from write/background pool.
   - Set `SEARCH_DATABASE_URL` to the search DB or replica.
   - Give it its own pool sizing.

2. Disable or queue nonessential write work during load tests.
   - `_log_search_history` should not compete with retrieval under load.
   - Use a bounded background queue or skip history logging in benchmark mode.

3. Limit per-request internal DB fanout.
   - A single request currently can need multiple DB connections across count, dense, skill, keyword, hydration, enrichment.
   - At 75 concurrent requests and pool max 10, this explodes into hundreds of queued DB acquisition attempts.

4. Add cancellation handling.
   - At 500 concurrency, clients timed out at 45s but the backend kept processing.
   - The server should stop expensive search work when the client disconnects or when a request-level timeout fires.

5. Track queue wait explicitly in load-test summaries.
   - `pool_acquire_ms` is the smoking gun, but the current top-level summary hides it.
   - Add per-batch averages/p50/p95 for pool acquire by branch: personalization, count, dense, skill, keyword, hydrate.

6. Move keyword to ParadeDB if you want to remove the FTS timeout tail.
   - These traces still show `backend: fts`.
   - ParadeDB will help the keyword branch, but it will not fix DB pool queueing by itself.

