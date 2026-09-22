# AI Disclosure

## Project use of AI

Talent Atlas is itself an AI application. Its runtime uses:

- **Gemini Live** for full-duplex audio conversation, streamed transcripts,
  model-selected typed tools and audio responses.
- **Gemini vision** for extracting visible role requirements from an uploaded
  PNG or JPEG before those requirements enter the typed search plan.
- **Optional Gemini, DeepSeek, Groq or OpenAI-compatible language models** for
  the non-realtime planner and recruiting copilot. The active provider is
  configuration-controlled; no provider secret is stored in this repository.
- **Sentence Transformers `all-MiniLM-L6-v2`** for local 384-dimensional query
  and document embeddings.
- **`cross-encoder/ms-marco-MiniLM-L-6-v2`** for bounded second-stage reranking.

The language model does not execute SQL or mutate application state directly.
It can only request declared tools. Pydantic schemas validate tool arguments,
the application owns retrieval and ranking, and state-changing calls are
idempotent and recorded in an effect log.

## AI assistance during development

AI coding assistants, including OpenAI Codex and other configured language
models, were used during architecture exploration, implementation, debugging,
test generation, documentation drafting and UI iteration. Generated work was
checked through repository tests, deterministic benchmarks, linting, browser
verification and direct inspection of the running application. The submitting
team remains responsible for the final implementation, claims and presentation.

## Data disclosure

The demonstration corpus is a transformed subset of a public recruitment
dataset. It is not a production hiring database and is not presented as one.
Candidate names and contact fields in the demo are generated or transformed for
the prototype. Results must not be used for real employment decisions.

## Limitations

- Model output can be incorrect; final claims are restricted to retrieved
  evidence where the product path supports it.
- The deterministic interruption benchmark measures orchestration correctness,
  not production-scale retrieval quality.
- The official hidden Samsung evaluator was not available when this disclosure
  was written, so hidden-set performance is not claimed.
- Voice synthesis quality and wake-word detection are outside Theme 05 scope.
