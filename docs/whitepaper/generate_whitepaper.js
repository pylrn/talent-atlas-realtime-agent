const fs = require("fs");
const path = require("path");
const {
  AlignmentType,
  BorderStyle,
  Document,
  ExternalHyperlink,
  Footer,
  Header,
  HeadingLevel,
  ImageRun,
  LevelFormat,
  PageNumber,
  PageBreak,
  Packer,
  Paragraph,
  SectionType,
  ShadingType,
  Table,
  TableCell,
  TableRow,
  TextRun,
  VerticalAlign,
  WidthType,
} = require("docx");

const ROOT = path.resolve(__dirname, "../..");
const ASSETS = path.join(ROOT, "api/static/project-story/assets");
const OUTPUT = path.join(__dirname, "Straatix_Hybrid_Talent_Search_Whitepaper_Final.docx");

const C = {
  navy: "17324D",
  teal: "008C83",
  orange: "E99132",
  blue: "3E83A8",
  ink: "243442",
  gray: "596A78",
  pale: "E9F4F2",
  paleBlue: "EDF3F7",
  paleOrange: "FFF2E3",
  line: "C8D4DC",
  white: "FFFFFF",
};

const PAGE_WIDTH = 12240;
const PAGE_HEIGHT = 15840;
const MARGIN_X = 864;
const MARGIN_Y = 760;
const CONTENT_WIDTH = PAGE_WIDTH - MARGIN_X * 2;

const noBorder = {
  top: { style: BorderStyle.NONE, size: 0, color: C.white },
  bottom: { style: BorderStyle.NONE, size: 0, color: C.white },
  left: { style: BorderStyle.NONE, size: 0, color: C.white },
  right: { style: BorderStyle.NONE, size: 0, color: C.white },
  insideHorizontal: { style: BorderStyle.NONE, size: 0, color: C.white },
  insideVertical: { style: BorderStyle.NONE, size: 0, color: C.white },
};

const thinBorder = {
  top: { style: BorderStyle.SINGLE, size: 5, color: C.line },
  bottom: { style: BorderStyle.SINGLE, size: 5, color: C.line },
  left: { style: BorderStyle.SINGLE, size: 5, color: C.line },
  right: { style: BorderStyle.SINGLE, size: 5, color: C.line },
  insideHorizontal: { style: BorderStyle.SINGLE, size: 5, color: C.line },
  insideVertical: { style: BorderStyle.SINGLE, size: 5, color: C.line },
};

function tr(text, options = {}) {
  return new TextRun({ text, font: options.font || "Arial", ...options });
}

function p(text, options = {}) {
  const children = Array.isArray(text) ? text : [tr(text, options.run || {})];
  return new Paragraph({
    children,
    alignment: options.alignment,
    spacing: options.spacing || { after: 110, line: 250 },
    indent: options.indent,
    border: options.border,
    keepNext: options.keepNext,
    keepLines: options.keepLines,
    numbering: options.numbering,
  });
}

function eyebrow(text, color = C.teal) {
  return p([tr(text.toUpperCase(), { bold: true, color, size: 18 })], {
    spacing: { before: 40, after: 70 },
    keepNext: true,
  });
}

function h1(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_1,
    children: [tr(text, { font: "Georgia", bold: true, color: C.navy, size: 42 })],
    spacing: { before: 30, after: 150 },
    keepNext: true,
  });
}

function h2(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    children: [tr(text, { font: "Georgia", bold: true, color: C.navy, size: 28 })],
    spacing: { before: 130, after: 75 },
    keepNext: true,
  });
}

function h3(text, color = C.navy) {
  return p([tr(text, { font: "Arial", bold: true, color, size: 21 })], {
    spacing: { before: 50, after: 45 },
    keepNext: true,
  });
}

function link(label, url, options = {}) {
  return new ExternalHyperlink({
    link: url,
    children: [tr(label, { color: options.color || C.teal, underline: {}, bold: options.bold ?? true, size: options.size || 19 })],
  });
}

function cell(children, width, options = {}) {
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    borders: options.borders || noBorder,
    shading: options.fill ? { fill: options.fill, type: ShadingType.CLEAR } : undefined,
    verticalAlign: options.verticalAlign || VerticalAlign.TOP,
    margins: options.margins || { top: 120, bottom: 120, left: 150, right: 150 },
    children,
  });
}

function table(rows, widths, options = {}) {
  return new Table({
    width: { size: widths.reduce((a, b) => a + b, 0), type: WidthType.DXA },
    columnWidths: widths,
    rows,
    borders: options.borders || noBorder,
    alignment: options.alignment || AlignmentType.CENTER,
  });
}

function rule(color = C.line) {
  return p([], {
    spacing: { before: 25, after: 80 },
    border: { bottom: { style: BorderStyle.SINGLE, size: 6, color, space: 1 } },
  });
}

function callout(label, body, fill = C.pale, accent = C.teal) {
  return table([
    new TableRow({
      children: [cell([
        p([tr(label + "  ", { bold: true, color: accent, size: 19 }), tr(body, { color: C.ink, size: 18 })], {
          spacing: { after: 0, line: 240 },
        }),
      ], CONTENT_WIDTH, {
        fill,
        borders: {
          ...noBorder,
          left: { style: BorderStyle.SINGLE, size: 22, color: accent },
        },
      })],
    }),
  ], [CONTENT_WIDTH]);
}

function image(file, width, height, alt) {
  return new ImageRun({
    type: "png",
    data: fs.readFileSync(file),
    transformation: { width, height },
    altText: { title: alt, description: alt, name: alt },
  });
}

function header() {
  return new Header({
    children: [new Paragraph({
      children: [
        tr("STRAATIX PARTNERS", { bold: true, color: C.navy, size: 15 }),
        tr("  |  TALENT INTELLIGENCE", { color: C.gray, size: 15 }),
      ],
      spacing: { after: 40 },
      border: { bottom: { style: BorderStyle.SINGLE, size: 4, color: C.line, space: 3 } },
    })],
  });
}

function footer() {
  return new Footer({
    children: [new Paragraph({
      children: [
        tr("Talent Atlas Hybrid RAG Whitepaper", { color: C.gray, size: 14 }),
        tr("\t"),
        tr("Page ", { color: C.gray, size: 14 }),
        new TextRun({ children: [PageNumber.CURRENT], color: C.gray, size: 14, font: "Arial" }),
        tr(" of ", { color: C.gray, size: 14 }),
        new TextRun({ children: [PageNumber.TOTAL_PAGES], color: C.gray, size: 14, font: "Arial" }),
      ],
      tabStops: [{ type: "right", position: 10200 }],
      spacing: { before: 40 },
    })],
  });
}

function section(children, isFirst = false) {
  return {
    properties: {
      type: isFirst ? undefined : SectionType.NEXT_PAGE,
      page: {
        size: { width: PAGE_WIDTH, height: PAGE_HEIGHT },
        margin: { top: MARGIN_Y, right: MARGIN_X, bottom: MARGIN_Y, left: MARGIN_X, header: 300, footer: 300 },
      },
    },
    // Define all header/footer variants on every one-page section so Word and
    // LibreOffice render section breaks consistently.
    headers: { default: header(), first: header(), even: header() },
    footers: { default: footer(), first: footer(), even: footer() },
    children,
  };
}

function featureBox(number, title, body, width, fill, accent) {
  return cell([
    p([tr(number, { bold: true, color: accent, size: 16 })], { spacing: { after: 55 } }),
    p([tr(title, { font: "Georgia", bold: true, color: C.navy, size: 22 })], { spacing: { after: 60 }, keepNext: true }),
    p([tr(body, { color: C.ink, size: 17 })], { spacing: { after: 0, line: 225 } }),
  ], width, { fill, borders: thinBorder, margins: { top: 130, bottom: 130, left: 150, right: 150 } });
}

function phaseCell(label, title, items, width, fill, accent) {
  const children = [
    p([tr(label, { bold: true, color: accent, size: 15 })], { spacing: { after: 45 } }),
    p([tr(title, { bold: true, color: C.navy, size: 18 })], { spacing: { after: 60 }, keepNext: true }),
  ];
  for (const item of items) {
    children.push(p([tr(item, { color: C.ink, size: 15 })], {
      numbering: { reference: "compact-bullets", level: 0 },
      spacing: { after: 34, line: 200 },
    }));
  }
  return cell(children, width, { fill, borders: thinBorder, margins: { top: 100, bottom: 100, left: 115, right: 95 } });
}

function reference(number, title, url) {
  return p([
    tr(`[${number}] `, { bold: true, color: C.navy, size: 15 }),
    tr(title + " ", { color: C.ink, size: 15 }),
    link("Source", url, { size: 15, bold: false }),
  ], { spacing: { after: 42, line: 190 } });
}

const coverHero = table([
  new TableRow({
    children: [
      cell([
        eyebrow("Straatix engineering internship project | August 2026"),
        new Paragraph({
          children: [tr("Building Talent Atlas", { font: "Georgia", bold: true, color: C.navy, size: 64 })],
          spacing: { after: 110 },
        }),
        p([tr("An evidence-grounded hybrid RAG search system for precise talent matching", { font: "Georgia", color: C.navy, size: 28 })], {
          spacing: { after: 140, line: 310 },
        }),
        p([link("TRY THE LIVE DEMO", "https://talent-atlas.onrender.com/talent", { color: C.orange, size: 20 })], {
          spacing: { after: 50 },
        }),
        p([link("Read the engineering journal", "https://talent-atlas.onrender.com/journey", { size: 18, bold: false })], {
          spacing: { after: 0 },
        }),
      ], 5900, { margins: { top: 120, bottom: 120, left: 0, right: 240 } }),
      cell([
        p([image(path.join(ASSETS, "talent-home.png"), 272, 250, "Talent Atlas recruiter search interface")], {
          alignment: AlignmentType.CENTER,
          spacing: { after: 25 },
        }),
        p([tr("The public showcase uses transformed demonstration data; generated fields such as location and salary are synthetic.", { italic: true, color: C.gray, size: 14 })], {
          alignment: AlignmentType.CENTER,
          spacing: { after: 0, line: 190 },
        }),
      ], 4612, { fill: C.paleBlue, borders: thinBorder, verticalAlign: VerticalAlign.CENTER }),
    ],
  }),
], [5900, 4612]);

const page1 = [
  p([image(path.join(ASSETS, "straatix-logo.png"), 170, 48, "Straatix Talent Atlas logo")], { spacing: { after: 70 } }),
  coverHero,
  h2("Executive thesis"),
  p("Talent search fails when meaning is treated as a substitute for facts. Talent Atlas combines semantic retrieval over resume evidence with exact keywords, canonical skills and structured PostgreSQL filters. The system returns one candidate-level ranking, the passages that support it and the score components behind the order. A typed agent makes the same engine conversational, while Pydantic validation, deterministic ranking and Langfuse traces keep model behavior inspectable."),
  callout(
    "The design boundary",
    "The LLM interprets intent and chooses tools. It does not invent candidates, calculate similarity, bypass hard filters or silently rewrite evidence.",
    C.pale,
    C.teal,
  ),
  h2("What this prototype demonstrates"),
  table([
    new TableRow({
      children: [
        featureBox("01", "Hybrid retrieval", "Meaning, literal terms, exact skills and SQL constraints contribute independently.", 2628, C.paleBlue, C.blue),
        featureBox("02", "Evidence-first ranking", "Chunks are grouped into people, scored and returned with supporting passages.", 2628, C.pale, C.teal),
        featureBox("03", "Typed agent access", "Natural language becomes validated JSON before the shared search endpoint runs.", 2628, C.paleOrange, C.orange),
        featureBox("04", "Observable learning", "Traces, impressions and governed outcomes support measured iteration.", 2628, "F4F6F7", C.navy),
      ],
    }),
  ], [2628, 2628, 2628, 2628]),
];

const page2 = [
  eyebrow("01 | Business problem, data and architecture"),
  h1("Why this matters to Straatix"),
  p("Straatix helps global companies build high-performing India teams where the first few hires disproportionately affect execution speed, culture and technical quality. A large candidate market does not automatically produce a precise shortlist: skills appear under different names, experience is distributed across resumes, and each additional combination of technology, seniority, location and operating context narrows the usable talent surface. India's scale therefore increases the value of better retrieval and verifiable evidence, not merely larger databases. [11]"),
  table([
    new TableRow({
      children: [
        featureBox("1", "India talent market", "Large, diverse and rapidly changing.", 2628, C.paleBlue, C.blue),
        featureBox("2", "Role intent", "Meaning, seniority and operating context.", 2628, C.pale, C.teal),
        featureBox("3", "Hard constraints", "Skills, location, experience and evidence.", 2628, C.paleOrange, C.orange),
        featureBox("4", "High-fit shortlist", "Small, explainable and actionable.", 2628, "F4F6F7", C.navy),
      ],
    }),
  ], [2628, 2628, 2628, 2628]),
  p([tr("Talent surface: availability is not the same as fit; every explicit requirement narrows the execution-ready shortlist.", { italic: true, color: C.gray, size: 14 })], {
    alignment: AlignmentType.CENTER,
    spacing: { before: 35, after: 55 },
  }),
  callout("Intern contribution", "The work progressed from vector search to a reusable hybrid engine with structured filters, rank fusion, optional reranking, typed agent tools, memory safeguards and end-to-end traces. It is a credible foundation for workflow integration, not a claim of completed production deployment.", C.pale, C.teal),
  h2("Meaning and exactness must coexist"),
  p("A recruiter may describe distributed-systems experience without using that exact phrase, while PostgreSQL, Kubernetes, five years of experience or Bengaluru remain literal requirements. Dense-only search can return a plausible candidate who violates a must-have; keyword-only search can miss equivalent experience. The system therefore separates four kinds of evidence."),
  table([
    new TableRow({
      children: [
        featureBox("MEANING", "Vector", "Related passages despite different wording.", 2628, C.paleBlue, C.blue),
        featureBox("WORDS", "Lexical", "Rare technologies, acronyms and phrases.", 2628, C.paleOrange, C.orange),
        featureBox("FACTS", "Structured", "Skills, location, experience and status.", 2628, C.pale, C.teal),
        featureBox("PROOF", "Evidence", "Candidate, document and source chunk.", 2628, "F4F6F7", C.navy),
      ],
    }),
  ], [2628, 2628, 2628, 2628]),
  h2("Getting the data into a searchable shape"),
  p("The starting corpus was a real public Kaggle recruitment dataset, not a fully synthetic set of resumes, but its rows did not match the application schema. Each row was transformed into a candidate record, a resume document and an evaluation case. Missing location and salary fields were generated deterministically for filter testing, emails were replaced with placeholders, and skill and experience fields were extracted with bounded rules. The source text remains real dataset content; generated attributes remain clearly marked demonstration data."),
  table([
    new TableRow({
      children: [
        phaseCell("1", "Candidate", ["identity + status", "skills + experience", "location + salary"], 3504, C.paleBlue, C.blue),
        phaseCell("2", "Document", ["resume or notes", "source metadata", "content hash for dedup"], 3504, C.pale, C.teal),
        phaseCell("3", "Chunk", ["token-aware passage", "384-value embedding", "candidate + document links"], 3504, C.paleOrange, C.orange),
      ],
    }),
  ], [3504, 3504, 3504]),
  h2("Why PostgreSQL and pgvector"),
  p("One database keeps candidate relationships, hard filters, full-text search and vectors close enough to coordinate in a single request. pgvector supports exact distance as well as HNSW approximate search; HNSW's ef_search parameter exposes the expected recall-versus-latency trade-off. This avoided a second vector database and its synchronization path while leaving clear scaling options through connection pooling, caching and background workers. [2]"),
];

const phaseWidth = Math.floor(CONTENT_WIDTH / 5);
const page3 = [
  eyebrow("02 | The search engine"),
  h1("One endpoint, twenty deliberate steps"),
  p([tr("POST /search", { font: "Courier New", bold: true, color: C.orange, size: 20 }), tr(" is the stable spine used by the search UI, benchmark scripts and recruiting agent. A request may exit early for a name lookup or filter-only query; a full candidate search compiles the input into a safe specification, retrieves a broad evidence pool and spends more computation only on the strongest candidates.", { color: C.ink, size: 19 })]),
  table([
    new TableRow({
      children: [
        phaseCell("01-04", "Understand", ["route detection", "sanitize input", "cache lookup", "LLM or fallback plan"], phaseWidth, C.paleBlue, C.blue),
        phaseCell("05-08", "Constrain", ["validate fields", "normalize aliases", "confidence gate", "intent routing"], phaseWidth, C.pale, C.teal),
        phaseCell("09-12", "Retrieve", ["SQL eligible pool", "dense HNSW", "keyword / BM25", "exact-skill path"], phaseWidth, C.paleOrange, C.orange),
        phaseCell("13-16", "Rank", ["RRF fusion", "candidate grouping", "bounded relaxation", "cross-encoder"], phaseWidth, "F4F6F7", C.navy),
        phaseCell("17-20", "Explain", ["feature score", "MMR diversity", "evidence explanation", "trace + impressions"], CONTENT_WIDTH - phaseWidth * 4, "F7F4EE", C.orange),
      ],
    }),
  ], [phaseWidth, phaseWidth, phaseWidth, phaseWidth, CONTENT_WIDTH - phaseWidth * 4]),
  h2("Three retrieval paths protect different failures"),
  table([
    new TableRow({
      children: [
        featureBox("DENSE", "Semantic chunks", "Cosine similarity over pgvector HNSW catches paraphrases and related work.", 3504, C.paleBlue, C.blue),
        featureBox("LEXICAL", "Exact text", "PostgreSQL full-text search or optional BM25 protects uncommon tools and phrases.", 3504, C.paleOrange, C.orange),
        featureBox("SKILL", "Canonical facts", "Normalized candidate skills are scored directly rather than inferred from prose.", 3504, C.pale, C.teal),
      ],
    }),
  ], [3504, 3504, 3504]),
  h2("Fusion before judgment"),
  p([tr("RRF(d) = sum over r  [ w_r / (60 + rank_r(d)) ]", { font: "Courier New", bold: true, color: C.navy, size: 23 })], {
    alignment: AlignmentType.CENTER,
    spacing: { after: 80 },
  }),
  p("Reciprocal Rank Fusion compares positions instead of adding incompatible cosine, text and binary scores. A candidate supported by several branches accumulates evidence. Chunks are then collapsed to one person, retaining the best passage plus supporting passages from other documents. [5]"),
  callout("Failure handling", "Empty searches receive at most one recorded relaxation. Status is never silently relaxed, and every broadened condition is returned to the UI.", C.paleOrange, C.orange),
];

const page4 = [
  eyebrow("03 | Models, ranking and evaluation"),
  h1("Small models where their training objective fits"),
  table([
    new TableRow({
      children: [
        cell([
          h3("all-MiniLM-L6-v2 for retrieval", C.blue),
          p("The local bi-encoder maps sentences and short paragraphs into 384-dimensional vectors. Its model card reports 22.7M parameters and contrastive training over more than one billion sentence pairs. That objective suits fast semantic recall, and the compact vectors reduce storage and memory compared with 1,536-dimensional defaults. The 256-word-piece truncation limit also reinforced token-aware chunking instead of embedding entire resumes. [3]"),
        ], 5256, { fill: C.paleBlue, borders: thinBorder }),
        cell([
          h3("MS MARCO MiniLM cross-encoder for reranking", C.orange),
          p("A cross-encoder reads the recruiter request and one retrieved passage together, making it a more careful relevance judge than independent embeddings. The selected L6 model was trained for passage ranking and offers a practical speed-quality point. It is applied only to a top-N prefix because joint encoding is too expensive for the full corpus. [4]"),
        ], 5256, { fill: C.paleOrange, borders: thinBorder }),
      ],
    }),
  ], [5256, 5256]),
  h2("The final score uses candidate evidence, not model confidence alone"),
  p("With the cross-encoder enabled, the feature score weights cross-encoder relevance at 45%, fused retrieval at 20%, exact skills at 12%, soft preferences at 10%, experience fit at 5%, skill recency at 3%, completeness at 2% and confirmed personalization at 3%. Without a cross-encoder, the missing weight moves mainly to retrieval, exact skills and experience. Scores are normalized to 0-100, then MMR selects the final list with lambda = 0.70: 70% relevance and 30% duplicate penalty."),
  table([
    new TableRow({
      children: [
        cell([p([tr("Recorded strategy benchmark", { bold: true, color: C.white, size: 18 })], { spacing: { after: 0 } })], 3300, { fill: C.navy, borders: thinBorder }),
        cell([p([tr("Top-1", { bold: true, color: C.white, size: 18 })], { alignment: AlignmentType.CENTER, spacing: { after: 0 } })], 1700, { fill: C.navy, borders: thinBorder }),
        cell([p([tr("P50", { bold: true, color: C.white, size: 18 })], { alignment: AlignmentType.CENTER, spacing: { after: 0 } })], 1800, { fill: C.navy, borders: thinBorder }),
        cell([p([tr("P95", { bold: true, color: C.white, size: 18 })], { alignment: AlignmentType.CENTER, spacing: { after: 0 } })], 1800, { fill: C.navy, borders: thinBorder }),
        cell([p([tr("Interpretation", { bold: true, color: C.white, size: 18 })], { spacing: { after: 0 } })], 1912, { fill: C.navy, borders: thinBorder }),
      ],
    }),
    ...[
      ["Semantic only", "35.0%", "53 ms", "172 ms", "fast baseline"],
      ["RRF hybrid", "35.0%", "60 ms", "85 ms", "stable hybrid"],
      ["RRF + rerank", "28.75%", "244 ms", "450 ms", "optional"],
    ].map((row, i) => new TableRow({
      children: row.map((value, j) => cell([
        p([tr(value, { bold: j === 0, color: C.ink, size: 17 })], { alignment: j > 0 && j < 4 ? AlignmentType.CENTER : AlignmentType.LEFT, spacing: { after: 0 } }),
      ], [3300, 1700, 1800, 1800, 1912][j], { fill: i % 2 ? "F8FAFB" : C.white, borders: thinBorder })),
    })),
  ], [3300, 1700, 1800, 1800, 1912], { borders: thinBorder }),
  p([tr("Benchmark context. ", { bold: true, color: C.navy, size: 16 }), tr("80 queries (30 manually designed and 50 imported), top_k=10, rerank pool=30, recorded 15 May 2026. The planner fell back on all 80 runs, labels were limited and results should be read as project evidence rather than a universal model comparison.", { italic: true, color: C.gray, size: 16 })], { spacing: { before: 70, after: 90, line: 220 } }),
  callout("Decision", "Reranking stayed configurable. A stronger theoretical model was not allowed to override a negative project result without a better labeled evaluation set.", C.pale, C.teal),
];

const page5 = [
  eyebrow("04 | Agent, interface and observability"),
  h1("The chatbot calls the search engine; it does not replace it"),
  p("JSON has two related jobs. A direct POST /search body configures predefined pipeline stages; it cannot execute arbitrary code. In the chatbot, PydanticAI exposes a fixed registry of typed Python functions. The model chooses one function and emits JSON arguments for its generated schema; Pydantic validates them before the corresponding do_* helper runs. Search tools call the same engine, inspection tools read candidate evidence, and workflow tools update explicit session state. [6]"),
  table([
    new TableRow({
      children: [
        featureBox("1", "Recruiter message", "Plain language plus current session context.", 2102, C.paleBlue, C.blue),
        featureBox("2", "Fixed tool registry", "Choose search, inspect, refine or workflow function.", 2102, C.pale, C.teal),
        featureBox("3", "Validated JSON args", "Match the selected function's generated schema.", 2102, C.paleOrange, C.orange),
        featureBox("4", "Deterministic helper", "Run shared search, database or session logic.", 2102, "F4F6F7", C.navy),
        featureBox("5", "JSON to the UI", "Translate tool results into SSE status, cards and prose.", 2104, "F7F4EE", C.orange),
      ],
    }),
  ], [2102, 2102, 2102, 2102, 2104]),
  h2("Prompting as an operating policy"),
  p("The prompt teaches intent-to-function routing as well as argument formatting: a new brief uses run_search; a small refinement uses modify_and_search; exact vocabulary can use list_skills or keyword_search; comparisons fetch candidate details; and shortlist actions use explicit workflow tools. Search results return JSON containing ordered candidates, evidence, scores and diagnostics. The SSE translator observes each tool start, arguments and result, renders status and candidate cards, then lets the model summarize only that returned evidence. Validation still happens in code after generation."),
  table([
    new TableRow({
      children: [
        cell([
          p([image(path.join(ASSETS, "talent-agent.png"), 276, 204, "Talent Atlas agent interface with streamed tool activity")], { alignment: AlignmentType.CENTER, spacing: { after: 35 } }),
          p([tr("Figure 1. The recruiter-facing UI streams tool activity and renders candidate cards separately from the model's prose.", { italic: true, color: C.gray, size: 14 })], { alignment: AlignmentType.CENTER, spacing: { after: 0, line: 190 } }),
        ], 5256, { fill: C.paleBlue, borders: thinBorder }),
        cell([
          p([image(path.join(ASSETS, "langfuse-search-spans.png"), 276, 113, "Langfuse trace showing nested planner retrieval and ranking spans")], { alignment: AlignmentType.CENTER, spacing: { after: 35 } }),
          h3("From 'slow' to a traceable cause", C.teal),
          p("Langfuse nests the request, model generation, tool calls, SQL, retrieval branches, fusion and reranking. That separates model latency from database waits and ranking cost, supports replay and allows production content to be sampled and redacted. [7]", { spacing: { after: 45, line: 220 } }),
          p([tr("Figure 2. Search spans from a recorded trace.", { italic: true, color: C.gray, size: 14 })], { alignment: AlignmentType.CENTER, spacing: { after: 0 } }),
        ], 5256, { fill: C.pale, borders: thinBorder }),
      ],
    }),
  ], [5256, 5256]),
  h2("Learning without silent bias"),
  p("Search impressions and explicit outcomes create a future learning dataset, but unconfirmed observations cannot affect ranking. Confirmed preferences are stored separately, personalization can be disabled, and every score remains decomposable into named signals."),
];

const page6 = [
  eyebrow("05 | Value, limits and next steps"),
  h1("A credible foundation, with clear claims still to earn"),
  table([
    new TableRow({
      children: [
        featureBox("SPEED", "One reusable engine", "The UI, benchmarks and agent share ranking logic instead of maintaining divergent search paths.", 3504, C.paleBlue, C.blue),
        featureBox("TRUST", "Auditable evidence", "Each result carries matching passages, normalized intent, component scores and trace metadata.", 3504, C.pale, C.teal),
        featureBox("LEARNING", "Measured iteration", "Outcomes and traces can support tuning by role family without treating the LLM as an oracle.", 3504, C.paleOrange, C.orange),
      ],
    }),
  ], [3504, 3504, 3504]),
  h2("What is proven - and what is not"),
  table([
    new TableRow({
      children: [
        cell([
          h3("Demonstrated in the prototype", C.teal),
          p("A working hybrid retrieval architecture; transformed real Kaggle recruitment data; stable typed search and agent-tool contracts; candidate evidence and explanations; cache benchmarks; and concurrent HTTP load tests from 1 to 500 simultaneous requests, with queueing limits captured in traces."),
        ], 5256, { fill: C.pale, borders: thinBorder }),
        cell([
          h3("Still requires production evidence", C.orange),
          p("Validation against an actual Straatix or client production dataset and real recruiter traffic; a large recruiter-adjudicated benchmark; measured business uplift; tenant isolation; PII governance; fairness analysis; and workflow integration."),
        ], 5256, { fill: C.paleOrange, borders: thinBorder }),
      ],
    }),
  ], [5256, 5256]),
  h2("A pragmatic 90-day path"),
  table([
    new TableRow({
      children: [
        phaseCell("0-30 DAYS", "Production-data validation", ["sample real role + profile data", "100-200 recruiter judgments", "Recall@50 and nDCG@10"], 3504, C.paleBlue, C.blue),
        phaseCell("31-60 DAYS", "Harden", ["privacy + tenant controls", "migration rehearsals", "cancellation + queue safeguards"], 3504, C.pale, C.teal),
        phaseCell("61-90 DAYS", "Integrate + learn", ["controlled recruiter pilot", "explicit rejection reasons", "role-family drift review"], 3504, C.paleOrange, C.orange),
      ],
    }),
  ], [3504, 3504, 3504]),
  h2("References and project evidence"),
  reference(1, "Lewis et al., Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks, NeurIPS 2020.", "https://arxiv.org/abs/2005.11401"),
  reference(2, "pgvector documentation: vector search, HNSW, filtering and hybrid retrieval.", "https://github.com/pgvector/pgvector"),
  reference(3, "Sentence Transformers model card: all-MiniLM-L6-v2.", "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2"),
  reference(4, "Cross-Encoder model card: ms-marco-MiniLM-L6-v2.", "https://huggingface.co/cross-encoder/ms-marco-MiniLM-L6-v2"),
  reference(5, "Cormack, Clarke and Buettcher, Reciprocal Rank Fusion, SIGIR 2009.", "https://cormack.uwaterloo.ca/cormacksigir09-rrf.pdf"),
  reference(6, "PydanticAI model and provider abstraction.", "https://pydantic.dev/docs/ai/models/overview/"),
  reference(7, "Langfuse observability and application tracing.", "https://langfuse.com/docs/observability/overview"),
  reference(8, "Talent Atlas engineering journal.", "https://talent-atlas.onrender.com/journey"),
  reference(9, "Talent Atlas source repository and recorded benchmark artifacts.", "https://github.com/pylrn/hybrid-search"),
  reference(10, "Interactive Talent Atlas demonstration.", "https://talent-atlas.onrender.com/talent"),
  reference(11, "Government of India, Press Information Bureau: India Leads Globally in AI Talent Acquisition, 19 Dec 2025.", "https://www.pib.gov.in/PressReleasePage.aspx?PRID=2206767"),
  p([tr("Prepared as a concise engineering whitepaper for the Straatix Talent Atlas prototype. Forward-looking recommendations should be validated through labeled evaluation, privacy review and production testing.", { italic: true, color: C.gray, size: 14 })], { spacing: { before: 55, after: 0, line: 190 } }),
];

const doc = new Document({
  creator: "Straatix Talent Atlas",
  title: "Building Talent Atlas: An Evidence-Grounded Hybrid RAG Search System",
  description: "Engineering whitepaper covering the Talent Atlas hybrid retrieval, ranking, agent and observability architecture.",
  styles: {
    default: {
      document: { run: { font: "Arial", size: 19, color: C.ink } },
      paragraph: { spacing: { after: 110, line: 250 } },
    },
    paragraphStyles: [
      {
        id: "Heading1",
        name: "Heading 1",
        basedOn: "Normal",
        next: "Normal",
        quickFormat: true,
        run: { font: "Georgia", size: 42, bold: true, color: C.navy },
        paragraph: { spacing: { before: 30, after: 150 }, outlineLevel: 0 },
      },
      {
        id: "Heading2",
        name: "Heading 2",
        basedOn: "Normal",
        next: "Normal",
        quickFormat: true,
        run: { font: "Georgia", size: 28, bold: true, color: C.navy },
        paragraph: { spacing: { before: 130, after: 75 }, outlineLevel: 1 },
      },
    ],
  },
  numbering: {
    config: [{
      reference: "compact-bullets",
      levels: [{
        level: 0,
        format: LevelFormat.BULLET,
        text: "-",
        alignment: AlignmentType.LEFT,
        style: { paragraph: { indent: { left: 250, hanging: 160 } } },
      }],
    }],
  },
  sections: [{
    properties: {
      page: {
        size: { width: PAGE_WIDTH, height: PAGE_HEIGHT },
        margin: { top: MARGIN_Y, right: MARGIN_X, bottom: MARGIN_Y, left: MARGIN_X, header: 300, footer: 300 },
      },
    },
    headers: { default: header() },
    footers: { default: footer() },
    children: [
      ...page1,
      new Paragraph({ children: [new PageBreak()] }),
      ...page2,
      new Paragraph({ children: [new PageBreak()] }),
      ...page3,
      new Paragraph({ children: [new PageBreak()] }),
      ...page4,
      new Paragraph({ children: [new PageBreak()] }),
      ...page5,
      new Paragraph({ children: [new PageBreak()] }),
      ...page6,
    ],
  }],
});

Packer.toBuffer(doc)
  .then((buffer) => {
    fs.writeFileSync(OUTPUT, buffer);
    process.stdout.write(`${OUTPUT}\n`);
  })
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });
