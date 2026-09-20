"""Register custom model pricing for Gemini + Groq in Langfuse.

Run once (or whenever you change providers/models, or pricing changes):
  python scripts/langfuse_register_models.py

Prices are per 1M tokens (input / output). Update from provider pricing pages:
  - Gemini: https://ai.google.dev/pricing
  - Groq:   https://console.groq.com/docs/models
"""

from langfuse import get_client

MODELS = [
    # ── Gemini ────────────────────────────────────────────────────────────
    {
        "model_name":    "gemini-2.0-flash",
        "match_pattern": "gemini-2.0-flash",
        "unit":          "TOKENS",
        "input_price":   0.075 / 1_000,   # $0.075 per 1M tokens → per 1K
        "output_price":  0.30  / 1_000,
    },
    {
        "model_name":    "gemini-1.5-pro",
        "match_pattern": "gemini-1.5-pro",
        "unit":          "TOKENS",
        "input_price":   1.25 / 1_000,
        "output_price":  5.00 / 1_000,
    },
    {
        "model_name":    "gemini-1.5-flash",
        "match_pattern": "gemini-1.5-flash",
        "unit":          "TOKENS",
        "input_price":   0.075 / 1_000,
        "output_price":  0.30  / 1_000,
    },
    # ── Groq ─────────────────────────────────────────────────────────────
    {
        "model_name":    "llama-3.1-70b-versatile",
        "match_pattern": "llama-3.1-70b-versatile",
        "unit":          "TOKENS",
        "input_price":   0.59 / 1_000,
        "output_price":  0.79 / 1_000,
    },
    {
        "model_name":    "llama-3.3-70b-versatile",
        "match_pattern": "llama-3.3-70b-versatile",
        "unit":          "TOKENS",
        "input_price":   0.59 / 1_000,
        "output_price":  0.79 / 1_000,
    },
    {
        "model_name":    "llama3-70b-8192",
        "match_pattern": "llama3-70b-8192",
        "unit":          "TOKENS",
        "input_price":   0.59 / 1_000,
        "output_price":  0.79 / 1_000,
    },
]


def main():
    client = get_client()
    for m in MODELS:
        try:
            client.api.models.create(
                model_name=m["model_name"],
                match_pattern=m["match_pattern"],
                unit=m["unit"],
                input_price=m["input_price"],
                output_price=m["output_price"],
            )
            print(f"✓ Registered {m['model_name']}")
        except Exception as exc:
            # Model already exists → update is not required; skip with a note.
            print(f"  {m['model_name']}: {exc}")


if __name__ == "__main__":
    import os, sys
    if not os.environ.get("LANGFUSE_PUBLIC_KEY"):
        print("LANGFUSE_PUBLIC_KEY not set", file=sys.stderr)
        sys.exit(1)
    main()
