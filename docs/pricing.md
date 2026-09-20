# Groq LLM Pricing — Searches per Dollar

Groq pricing as of May 2026 (pay-as-you-go, no batch discount).

## Token budget per search

| Component | Tokens |
|-----------|--------|
| System prompt (PLANNER_SYSTEM_PROMPT) | 1,872 |
| Typical user query | ~14–61 |
| **Total input (avg)** | **~2,000** |
| Output JSON | ~128 |

Measured with `tiktoken cl100k_base` against the actual system prompt text and five representative benchmark queries (shortest 29 chars / 14 tokens, longest 265 chars / 61 tokens). Output token count is the median across benchmark responses.

## Cost per search

| Model | Input $/M | Output $/M | Input cost | Output cost | **Total/search** | **Searches/$1** |
|-------|-----------|------------|------------|-------------|-----------------|-----------------|
| `llama-3.1-8b-instant` | $0.05 | $0.08 | $0.000100 | $0.000010 | **$0.000110** | **~9,070** |
| `llama-4-scout-17b-16e-instruct` | $0.11 | $0.34 | $0.000220 | $0.000044 | **$0.000264** | **~3,790** |
| `qwen/qwen3-32b` | $0.29 | $0.59 | $0.000580 | $0.000076 | **$0.000656** | **~1,525** |
| `llama-3.3-70b-versatile` | $0.59 | $0.79 | $0.001180 | $0.000101 | **$0.001281** | **~781** |

## Benchmark quality + latency (from `scripts/benchmark_llm_models.py`, 5 queries)

| Model | Score | Avg latency | Errors |
|-------|-------|-------------|--------|
| `llama-4-scout-17b-16e-instruct` | **1.000** | **902 ms** | 0 |
| `llama-3.3-70b-versatile` | 0.980 | 2,731 ms | 0 |
| `llama-3.1-8b-instant` | 1.000 | 14,280 ms | 0 |
| `qwen/qwen3-32b` | 0.780 | 16,701 ms | 1 |

`llama-3.1-8b-instant` and `qwen3-32b` engage chain-of-thought ("thinking mode") on complex/ambiguous queries, causing 20–26 s spikes even though simple queries finish in ~1 s. The advertised cost advantage of 8b-instant is mostly eroded by those slow queries generating far more output tokens.

## Recommendation

**`llama-4-scout-17b-16e-instruct`** is the best overall choice: perfect extraction score, sub-1 s latency on all query types, and ~3,790 searches per dollar — roughly 5× cheaper than 70b-versatile with no quality loss.

Use `llama-3.3-70b-versatile` if you need a fallback with near-perfect accuracy and can tolerate occasional 2–10 s latency.

Avoid `llama-3.1-8b-instant` and `qwen3-32b` in production: their thinking-mode spikes make p95 latency unpredictable and provide no real cost saving at the actual token volumes.

## Assumptions

1. Token counts are measured with `tiktoken cl100k_base` (OpenAI tokeniser). Groq uses its own tokeniser per model; actual billed tokens may differ by ±5–10%.
2. Pricing sourced from the Groq console pricing page. Prices change — verify before capacity planning.
3. "Searches per dollar" assumes 100% of searches invoke the LLM planner (no cache hits). With a warm plan cache hitting 30–50% of repeat queries, effective cost per unique query is lower.
4. Output token count (~128) is the median across the five benchmark queries. JD-paste queries produce slightly longer output (~160 tokens); single-skill queries are shorter (~90 tokens).
5. No batch / volume discounts applied.
