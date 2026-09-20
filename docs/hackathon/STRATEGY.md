# SignalRAG: Samsung PRISM Theme 4 Strategy

## Product thesis

SignalRAG is an interruptible, corpus-grounded search experience built on the
existing Talent Atlas hybrid retrieval engine. It demonstrates the difficult
part of live RAG visibly: retrieval starts from a stable partial utterance,
compound intent fans out into bounded subqueries, late constraints retrieve
only the delta, and presentation-only turns reuse prior evidence.

The interface is an evaluation cockpit, not a generic chatbot. A judge can see
the transcript, controller state, subqueries, function calls, fusion formula,
real chunk IDs, answer versions, latency, citation coverage and session reset
without opening developer tools.

## Why this direction

Three approaches were considered:

1. **Add streaming decoration to the existing chatbot.** Fast, but it would hide
   the very controller and grounding behavior the theme evaluates.
2. **Build a separate voice assistant with a second search stack.** Visually
   novel, but it would duplicate proven retrieval logic and create parity risk.
3. **Recommended and adopted: expose a thin streaming controller over the
   existing deterministic search core.** This preserves the tested data and
   ranking pipeline while making time, tools, evidence and revisions observable.

## Runtime architecture

```text
partial transcript
  -> retrieval controller (WAIT / RETRIEVE / SUPPRESS)
  -> bounded intent decomposition
  -> parallel calls to the existing hybrid corpus retriever
       SQL constraints + vector + keyword + exact skills
  -> candidate-level Reciprocal Rank Fusion
  -> corpus evidence with real chunk/document IDs
  -> versioned grounded answer
```

Each WebSocket owns one in-memory `LiveRAGSession`. A new transcript revision
cancels in-flight work from the previous revision. Completed subqueries are
cached only in that session and reused when a late clause arrives. Reset or
disconnect discards the state.

## Acceptance-gate mapping

| Theme 4 gate | Product evidence | Automated check |
|---|---|---|
| Reproducibility | Docker and one FastAPI process serve backend and UI | clean-start smoke test |
| Early retrieval >=80% | partial transcript events show first retrieval before final event | streaming benchmark |
| Multi-intent >=70% | decomposed subqueries and per-query tool traces are visible | compound-query suite |
| Grounding >=85% | answer versions carry real database `chunk_id` values | citation verifier |
| State continuity | reused/delta labels and answer versions remain visible | late-detail suite |
| 100% trace coverage | every controller, tool, fusion and answer event enters the trace stream | event-schema test |

## Guardrails

- Corpus-only facts: no web search is reachable from the live route.
- Session-only memory: no recruiter ID or persistent memory store is passed to
  the search engine.
- Typed, bounded tools: the UI reads the same registry used to explain runtime
  triggers, schemas, side effects and limits.
- Stale-result protection: only the current revision may update the answer.
- Citation integrity: the grouping layer preserves actual chunk and document
  IDs; the answer cannot manufacture identifiers.
- Prompt portability: optional model instructions live under `prompts/live_rag/`
  rather than being embedded in application code.

## Demo sequence (under five minutes)

1. Start a compound request and pause after “backend engineer.” Show retrieval
   beginning before the sentence ends.
2. Continue with skills and payments context. Show subquery fan-out, parallel
   tool calls and the shortlist changing as evidence arrives.
3. Add a location constraint. Point out reused subqueries, one delta retrieval,
   retained citations and a new answer version.
4. Ask for two bullets. Show `SUPPRESS`, zero new retrieval and identical
   citations.
5. Open the tool registry and one candidate evidence card. Finish on the metrics
   row and session reset.

## Honest scope

The candidate corpus is a transformed public recruitment dataset rather than a
production hiring dataset. The base engine has already been tested for scale and
concurrency, but the Theme 4 benchmark still needs a dedicated private-style
streaming evaluation set. The current first slice uses deterministic
decomposition and grounded synthesis to keep latency, cost and failure modes
inspectable; optional LLM controller/synthesis adapters can be evaluated as
ablations rather than assumed improvements.
