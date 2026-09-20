# No-LLM 20 Query Benchmark After Keyword Policy

## Summary

| Metric | Before | After |
| --- | ---: | ---: |
| avg_ms | 1337.97 | 674.93 |
| p50_ms | 855.73 | 696.87 |
| p75_ms | 1299.97 | 785.7 |
| p90_ms | 4035.85 | 810.05 |
| max_ms | 4941.96 | 858.05 |

- Average top-3 overlap with the saved old run: 1.1/3
- Average current feature score: 53.45
- Current weak results (<55 score): 70
- Keyword actions: {'run': 14, 'skip': 6}

## Rows

| Query | Before ms | After ms | Δ ms | Keyword | Avg score | Top-3 overlap |
| --- | ---: | ---: | ---: | --- | ---: | ---: |
| python_bangalore | 712.97 | 816.08 | 103.11 | run:narrow_hard_filters | 68.20 | 0/3 |
| react_sf | 652.24 | 604.90 | -47.34 | run:narrow_hard_filters | 69.15 | 2/3 |
| sql_hyd | 911.86 | 749.47 | -162.39 | run:narrow_hard_filters | 71.78 | 1/3 |
| java_blr | 799.60 | 713.01 | -86.59 | run:narrow_hard_filters | 62.96 | 1/3 |
| marketing_boston | 548.58 | 858.05 | 309.47 | run:narrow_hard_filters | 75.81 | 2/3 |
| finance_ny | 511.84 | 781.13 | 269.29 | run:narrow_hard_filters | 69.39 | 2/3 |
| blockchain_mumbai | 548.85 | 493.14 | -55.71 | run:narrow_hard_filters | 65.88 | 1/3 |
| salesforce_pune | 483.25 | 392.38 | -90.87 | run:narrow_hard_filters | 69.87 | 2/3 |
| linux_ops | 1192.16 | 798.35 | -393.81 | run:specific_terms_with_medium_filters | 62.06 | 0/3 |
| frontend_vague | 941.14 | 607.79 | -333.35 | run:specific_terms | 38.87 | 3/3 |
| marketing_vague | 4110.22 | 783.50 | -3326.72 | skip:broad_pool_common_terms | 58.99 | 0/3 |
| finance_vague | 3366.47 | 657.28 | -2709.19 | skip:broad_pool_common_terms | 38.43 | 0/3 |
| database_vague | 2005.30 | 673.18 | -1332.12 | skip:broad_pool_common_terms | 38.32 | 1/3 |
| crm_vague | 657.16 | 549.80 | -107.36 | run:specific_terms | 39.07 | 3/3 |
| country_us_generic_it | 4941.96 | 774.21 | -4167.75 | skip:broad_pool_common_terms | 38.33 | 1/3 |
| country_us_generic_leader | 1335.91 | 680.72 | -655.19 | skip:broad_pool_common_terms | 38.53 | 0/3 |
| country_india_generic_sdlc | 1128.69 | 612.35 | -516.34 | skip:broad_pool_common_terms | 38.01 | 1/3 |
| country_india_cloud | 304.95 | 351.56 | 46.61 | run:narrow_hard_filters | 0.00 | 0/3 |
| accounting_ny | 666.27 | 792.29 | 126.02 | run:narrow_hard_filters | 66.27 | 1/3 |
| devops_broad | 939.93 | 809.38 | -130.55 | run:small_filtered_pool | 59.02 | 1/3 |

## Notes

- Quality is measured with proxies because this benchmark has no labeled relevant candidates.
- Proxies used: result count, feature scores, weak-result count, retrieval paths, and top-3 overlap with the saved old run.
- Timing is live against the current database and can move with DB cache warmth and network conditions.
