(function () {
  "use strict";

  window.PROJECT_STORY = {
    sources: [
      {
        id: "chat-origin",
        number: "01",
        kind: "Project conversation",
        title: "Origin and PostgreSQL/pgvector rebuild",
        detail: "The initial goal, constraints, structured/unstructured data problem, and decision to rebuild the earlier smart-search prototype around PostgreSQL and pgvector.",
        artifact: "Redacted project-associated Codex sessions"
      },
      {
        id: "chat-first-ui",
        number: "02",
        kind: "Project conversation + code",
        title: "First runnable search system and admin bench",
        detail: "The first end-to-end API, database inspection views, candidate/document controls, filters, scores, and latency readouts.",
        artifact: "api/main.py; api/static/index.html; early project sessions"
      },
      {
        id: "chat-dataset-hunt",
        number: "03",
        kind: "Project artifacts",
        title: "Dataset search, rejection, transformation, and corpus growth",
        detail: "Evidence for the synthetic fixtures, 5,000-row transformation, resume corpus adapters, deduplication work, and measured local corpus.",
        artifact: "pipeline/recruitment_dataset.py; pipeline/resume_corpus.py; db/migrations/"
      },
      {
        id: "minilm-model",
        number: "04",
        kind: "Official model card",
        title: "sentence-transformers/all-MiniLM-L6-v2",
        detail: "The model maps sentences and paragraphs to 384-dimensional vectors and was trained on a large collection of sentence-pair datasets for tasks including semantic search.",
        url: "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2"
      },
      {
        id: "current-pipeline-code",
        number: "05",
        kind: "Current implementation",
        title: "Hybrid retrieval, fusion, grouping, ranking, and diversity",
        detail: "The live deterministic path and formulas described in the article.",
        artifact: "pipeline/search.py; retrieve_dense.py; retrieve_bm25.py; retrieve_skill.py; fusion.py; group.py; feature_ranker.py; diversity.py"
      },
      {
        id: "cross-encoder-model",
        number: "06",
        kind: "Official model card",
        title: "cross-encoder/ms-marco-MiniLM-L-6-v2",
        detail: "A MiniLM cross-encoder trained on MS MARCO passage ranking data to score query-passage pairs.",
        url: "https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2"
      },
      {
        id: "strategy-benchmark",
        number: "07",
        kind: "Project benchmark",
        title: "Search strategy benchmark",
        detail: "An 80-query recorded evaluation comparing relevance and latency across search strategies. Its labels and sample size limit the strength of conclusions.",
        artifact: "scripts/benchmark_realtime_interruptions.py; scripts/evaluate_realtime_agent.py"
      },
      {
        id: "pipeline-guide",
        number: "08",
        kind: "Project documentation + code",
        title: "Search request contract and pipeline modes",
        detail: "The SearchRequest schema, named modes, canonical search spec, provider adapters, and per-request configuration overrides.",
        artifact: "api/main.py::SearchRequest; pipeline/modes.py; pipeline/planner.py; pipeline/validator.py"
      },
      {
        id: "pydantic-docs",
        number: "09",
        kind: "Official documentation",
        title: "PydanticAI agents, tools, models, and providers",
        detail: "Framework support for typed dependencies, generated tool schemas, message history, streaming, provider abstraction, and runtime model selection.",
        url: "https://ai.pydantic.dev/"
      },
      {
        id: "langfuse-docs",
        number: "10",
        kind: "Official documentation + implementation",
        title: "Langfuse observability data model",
        detail: "The trace, observation, generation, event, and score model used to structure project instrumentation.",
        artifact: "pipeline/observability.py; project Langfuse implementation reports",
        url: "https://langfuse.com/docs/observability/data-model"
      },
      {
        id: "memory-series",
        number: "11",
        kind: "Project implementation",
        title: "Recruiter facts, observations, outcomes, and personalization",
        detail: "The distinction between confirmed memory and inactive observations, deterministic profiling, confirmation flow, and bounded personalization.",
        artifact: "pipeline/memory.py; pipeline/profiler.py; pipeline/personalization.py; docs/memory.md"
      },
      {
        id: "pgvector-docs",
        number: "12",
        kind: "Official documentation",
        title: "pgvector exact and approximate nearest-neighbor search",
        detail: "Cosine distance, HNSW/IVFFlat indexing, filtering behavior, and iterative index scans.",
        url: "https://github.com/pgvector/pgvector"
      },
      {
        id: "ubuntu-reference",
        number: "13",
        kind: "Editorial reference",
        title: "Hybrid search and reranking: a deeper look at RAG",
        detail: "Canonical's plain-language explanation of dense retrieval, keyword retrieval, RRF, and cross-encoder reranking used as an editorial clarity reference.",
        url: "https://ubuntu.com/blog/hybrid-search-and-reranking-a-deeper-look-at-rag"
      },
      {
        id: "agent-prompt-code",
        number: "14",
        kind: "Current implementation",
        title: "Recruiting agent system prompt and typed search tool",
        detail: "The live prompt contract: context injection, must-versus-should rules, intent routing, evidence-grounded explanations, recovery behavior, and confirmation before persistent memory.",
        artifact: "pipeline/agent.py::build_system_prompt; pipeline/agent.py::run_search"
      }
    ]
  };
})();
