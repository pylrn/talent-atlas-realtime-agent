# Cache Performance Benchmark Report
Generated at: 2026-06-25 16:19:48

## Comparative Scenarios Matrix

| Scenario | Run | p50 | p95 | Avg Latency | Overall Hit Rate | Slowest Branch | Avg Pool Wait | Avg DB RTT | Parity Mismatches |
| :--- | :--- | :---: | :---: | :---: | :---: | :--- | :---: | :---: | :---: |
| **Memory, retrieval cache off** | Cold | 919.99ms | 1660.61ms | 977.58ms | 7.0% | keyword (404.86ms) | 0.04ms | 280.75ms | 4/20 |
| | Warm | 502.07ms | 655.23ms | 442.12ms | 98.2% | keyword (276.95ms) | 0.04ms | 147.18ms | 4/20 |
| **Memory, retrieval cache on** | Cold | 734.97ms | 1067.05ms | 870.3ms | 7.0% | keyword (396.51ms) | 0.05ms | 309.53ms | 5/20 |
| | Warm | 180.01ms | 259.92ms | 134.63ms | 98.3% | enrichment (108.44ms) | 0.01ms | 22.09ms | 5/20 |

## Cache Namespaces Breakdown (Warm Runs)

| Scenario | Embed | Dense | BM25 | Skill | Chunk | Count |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| **Memory, retrieval cache off** | 100.0% | - | - | - | 100.0% | - |
| **Memory, retrieval cache on** | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |

## Correctness & Parity Verification Guard
All scenarios verify exact top-10 candidate list parity against a cache-disabled baseline run. 
Parity mismatch counts represent how many of the 20 test queries did not return the exact same ranking results under that cache setting.

