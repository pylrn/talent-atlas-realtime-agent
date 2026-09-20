# Streaming Live RAG Research Brief

Date: 2026-09-21  
Scope: architecture and presentation choices for Samsung PRISM Y2026 Theme 4

## What the official brief actually rewards

The Theme 4 materials define live RAG as a stateful retrieval problem, not a
typing animation. The system should decide when retrieval is warranted, start
eligible work before the utterance ends, decompose compound requests, fuse and
rerank the evidence, preserve state when supplementary details arrive, suppress
retrieval for presentation-only follow-ups, and expose observability. The guide
also requires corpus isolation, real source identifiers, session-only ephemeral
memory and architectural parsimony. Its acceptance gates quantify early
retrieval, multi-intent handling, grounded citations, state continuity and trace
coverage. [S01][S02]

This changes the demo priority. A polished final answer is insufficient. The
submission must make intermediate decisions and evidence inspectable while the
utterance is still forming.

## Evidence that shaped the architecture

Question decomposition can materially improve retrieval for complex questions.
A 2025 evaluation reports improvements in MRR@10 and answer F1 from decomposing,
retrieving per subquestion, then merging and reranking. [S03] However, a 2026
industry study found RAG Fusion improved raw recall without improving the final
top-k after reranking and truncation, while adding latency. [S04] SignalRAG
therefore caps decomposition at four queries, always keeps a whole-request
semantic anchor, and displays both retrieval gain and latency rather than
presenting fan-out as automatically better.

The evaluation must separate retrieval quality from answer quality. RAGAS
formalizes component-level evaluation, while GroUSE shows that broad LLM judges
can miss grounded-QA failure modes. [S05][S06] The planned benchmark therefore
records early-retrieval rate, recall, citation support, TTFR/TTFT, cost and state
continuity separately. Citation evaluation literature also supports fine-grained
claim-to-source checks instead of treating the presence of a citation marker as
proof. [S07][S08]

Concurrent dialogue/reasoning systems and predictive prefetching both support
starting work from partial intent, but the official brief favors simple and
cheap orchestration. [S09][S10] The design borrows the shared revision state and
early trigger, not a second autonomous agent. This keeps cancellation and reuse
easy to test.

PydanticAI can derive tool schemas from typed Python functions, inject runtime
dependencies and validate tool arguments, which is useful for the broader
Talent Atlas copilot. [S11] The Theme 4 streaming path deliberately uses a
smaller explicit registry and deterministic controller first. The model can be
added as an ablation without becoming the database policy.

Langfuse's trace/observation model maps cleanly to a live RAG turn: session,
transcript trace, controller span, retrieval/tool spans, fusion span and answer
generation. [S12] The public cockpit mirrors that nesting so the visualization
is also an honest view of what should be captured in telemetry.

## Sources

- **S01** Samsung PRISM Y2026 GenAI Hackathon 3rd Edition brief, local reference PDF.
- **S02** Samsung PRISM Theme 4 Guide: Streaming Live RAG, local reference PDF.
- **S03** Ammann et al., [Question Decomposition for Retrieval-Augmented Generation](https://arxiv.org/abs/2507.00355), 2025.
- **S04** Medrano et al., [Scaling RAG with RAG Fusion: Lessons from Industry Deployment](https://arxiv.org/abs/2603.02153), 2026.
- **S05** Es et al., [RAGAS: Automated Evaluation of Retrieval Augmented Generation](https://aclanthology.org/2024.eacl-demo.16/), EACL 2024.
- **S06** [GroUSE: A Benchmark to Evaluate Evaluators in Grounded Question Answering](https://aclanthology.org/2025.coling-main.304/), COLING 2025.
- **S07** [A Fine-Grained Evaluation Framework for RAG Citations](https://aclanthology.org/2024.inlg-main.35/), INLG 2024.
- **S08** [ReClaim: Grounded Text Generation with Sentence-Level Citations](https://aclanthology.org/2025.findings-naacl.55/), NAACL Findings 2025.
- **S09** [Ping-Ponder: Concurrent Dialogue and Reasoning](https://aclanthology.org/volumes/2026.sigdial-1/), SIGDIAL 2026.
- **S10** [Predictive Prefetching for Retrieval-Augmented Generation](https://arxiv.org/abs/2605.17989), 2026.
- **S11** PydanticAI, [Function Tools and Toolsets](https://pydantic.dev/docs/ai/tools-toolsets/tools/).
- **S12** Langfuse, [Observability Data Model](https://langfuse.com/docs/observability/data-model).
- **S13** NIST, [AI Risk Management Framework](https://www.nist.gov/itl/ai-risk-management-framework).

## Research limitations

The second official guide is image-only, so its six pages were visually
inspected after local rendering. Literature findings are used to justify design
choices, not as benchmark results for this implementation. SignalRAG's own
numbers must come from the repository's reproducible evaluation suite.
