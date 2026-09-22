# STORY.md — Talent Atlas, Samsung PRISM Y2026 Theme 05

## ① Intent

**Audience and occasion.** The jury of the Samsung PRISM Y2026 GenAI Hackathon,
final demo round (top 15 teams, 15 October). They have the repo open and a
5-minute video beside them. They are not reading a proposal; they are deciding
whether the prototype works.

**What they must believe when we finish.** That this is not a demo that works
when nothing goes wrong. Interruption, revision, goal change and write-retry are
the *normal* path here, and each one is pinned by a named, executable check. The
single sentence we want them to carry out of the room: *every turn publishes
what it believes, so an interruption is a diff rather than a restart.*

**Length.** 12 pages. Hero quota at 12 pages is 3 (25%), which is what we use.

**Visual temperament.** Teal engineering: deep teal authority, lime used only
where the eye must land, monospace for anything that is a measurement or an
identifier. No photography, no stock imagery, no illustration. The subject is
control flow, so the visuals are structural — architecture, sequence, state
shape, audit trail. Restrained, evidence-forward, unemotional. It should read
like an instrument panel, not a pitch.

**Content boundaries.**
- *Must cover*, because the brief mandates it verbatim: theme ID, project title
  and team details; problem statement in our own words; solution and
  architecture diagram; tools and tech stack; innovation highlights; results;
  limitations.
- *Must lead with results.* Working prototype is 30% of the score, the single
  heaviest band, and the brief says "does the prototype actually work". Page 3
  is therefore the measured evidence, before any mechanism is explained.
- *Will not show*: ranking-quality improvements (not claimed, not measured), UI
  polish (explicitly out of scope in the brief), speech-synthesis quality
  (out of scope), cross-session personalisation (explicitly excluded — the brief
  allows session-scoped memory only).
- *Will not do*: claim a number we cannot reproduce from the tagged commit. Every
  figure on page 3 comes from a script in the repo that exits non-zero when it
  fails.

**Scoring alignment.** The deck is ordered by the weights the brief publishes:
results first (30%), then the mechanisms that show technical depth (25%), then
what is genuinely new (20%), with theme relevance (15%) carried by page 4's
requirement mapping and documentation (10%) by the fact that every claim cites
the check that enforces it.

## ② Skeleton

**12 pages, no catalogue and no chapter dividers.** A catalogue would cost a page
and four dividers would cost four more; at 12 pages that is 40% of the deck spent
on navigation, and "one idea per page" was chosen deliberately. Because no
catalogue is declared, the catalogue↔divider contract has no members and is
vacuously satisfied. The arc carries the reader instead: *what the problem
really is → that it works → how → what is new → what we did not solve.*

| Chapter (implicit) | Pages | What it does |
| :--- | :--- | :--- |
| Evidence | 01–03 | Title, the problem restated as one testable requirement, the measured results |
| Mechanism | 04–09 | Requirement→mechanism map, architecture, cancellation, state, the write, the fast path |
| Substance | 10–12 | Tools and stack, innovation and honest limits, close |

**Hero pages.** 01 (cover), 03 (results), 12 (close) — 3 of 12 = 25%, inside the
20–30% band, and no two are adjacent (03 has 02 and 04 between it and the others).

**Rhythm curve.**

```
01 peak   02 valley   03 peak   04 valley   05 peak
06 valley 07 valley   08 peak   09 valley   10 valley
11 peak   12 peak
```

The longest run of valleys is two (06–07 and 09–10), so the "three consecutive
valleys" failure never triggers.

**Layout budget.** Symmetric layouts are capped at two pages, so only the closing
page uses one — 11 of 12 pages are asymmetric (92%, floor is 40%). The pair
`左大图+右侧文字` / `非对称双栏` is capped at 40% of 12 ≈ 4 pages and uses exactly
4, all of them `非对称双栏`. `N卡片横排` is never used. No two adjacent pages
share a layout.

## ③ Page outline

| # | title | type | role | rhythm | layout | visual | visual_role | density | anti_pattern | description |
| :- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 01 | Talent Atlas | cover | hero | peak | 全幅图+骑线文字 | L1: cover_field.svg（满幅底） | atmosphere | 字数约50 / 图1 / 留白约42% | 禁止等宽卡片横排；禁止把 "THEME 05" 做成标题栏右侧的装饰贴片；禁止居中对称构图 | Theme 05 / Interruptible Real-Time Agents, the product name, the one-line thesis, and the team-details block. Left-bleed composition so the title rides into the field rather than sitting on top of it. |
| 02 | The problem, in our own words | content | supporting | valley | 非对称双栏 60:40 | L2: problem_collapse.svg（右 40%） | evidence | 字数约240 / 图1 / 留白约20% | 禁止左右 50:50 等分；禁止整段引用 brief 原文当作"自己的话" | The brief's four asks are not four features. Read together they are one requirement, and that reframing is what made the work testable. Four asks on the left, one sentence on the right. |
| 03 | It works, and here is the proof | content | hero | peak | 巨型数字+洞察 | 大数字（≥96px）×3 + Chart | anchor | 字数约150 / 图1 / 留白约36% | 禁止等宽卡片横排；禁止把核心数字塞进图表卡的角落；禁止 L3 角标顶替 L1 | 670 tests, 12 of 12 scenarios, 0.9 ms against a 250 ms budget — and the whole graded surface runs with no database, no network and no API key. The number is only half the page; the judgement is that reproducibility was designed in, not asserted. |
| 04 | What the brief asks, what answers it | content | supporting | valley | 左标题+右内容 | L3 | evidence | 字数约280 / 图0 / 留白约16% | 禁止等宽四卡；禁止把"需求"栏写成 brief 的逐字摘抄 | Ten Theme 05 requirements in a narrow left rail, the mechanism that satisfies each on the right, and the named check that enforces it. This page is where theme relevance (15%) is earned. |
| 05 | Architecture | content | supporting | peak | 上大图+下方卡片 | L1: architecture.svg（占 B 区 55%） | evidence | 字数约190 / 图1 / 留白约18% | 禁止把架构图缩成 200×70 装饰贴片；禁止 50:50 等分；禁止把图当背景而正文压在其上 | The pipeline end to end, then the two decisions that carry the weight: the shared eligibility clause is a cross-branch dependency and is fingerprinted as one; a snapshot is either authoritative or it is not. |
| 06 | Cancel only what the revision invalidated | content | supporting | valley | 非对称双栏 65:35 | L1: cancellation.svg（左 65%） | evidence | 字数约210 / 图1 / 留白约20% | 禁止把四个分支画成等宽四卡；禁止把"取消"写成一句话结论而不给出被保留的分支 | A query rewrite cancels exactly `{vector, bm25}` and leaves `sql` and `skills` running; changing the hard filter moves all four, because rows fetched under a different eligibility clause are not the same rows. 120 ms of work avoided, 0 ms cancellation latency. |
| 07 | What the system believes, on every event | content | supporting | valley | 左标题+右内容 | L2: snapshot_shape.svg | evidence | 字数约230 / 图1 / 留白约18% | 禁止把快照画成一张通用 JSON 卡片；禁止省略 unset_slots（"从未提及"与"被丢弃"必须可区分） | The snapshot shape, why slots are total rather than sparse, and why speculative work is published with `authoritative=False` — a guess about what the user is about to say can never be mistaken for a decision they made. |
| 08 | The write that can be proved | content | supporting | peak | 非对称双栏 60:40 | L1: audit_trail.svg（左 60%） | evidence | 字数约200 / 图1 / 留白约20% | 禁止把审计日志渲染成装饰性终端截图；禁止只展示重试而不同时展示落地的那一次写入 | One state-changing tool, idempotency keys and scope supersession — and the bug we found by trying to read the guarantee out of the log meant to express it: the log recorded the retry but not the effect. |
| 09 | Fast, and not allowed to cheat | content | supporting | valley | 上大图+下方卡片 | L1: fastpath_timeline.svg（占 B 区 55%） | evidence | 字数约200 / 图1 / 留白约20% | 禁止把延迟数字做成孤立大数字而不给预算参照；禁止暗示快速路径绕过了审计 | The acknowledgement is derived from the instruction diff, never from the corpus, so it is emitted in ~2.6 ms and still passes the same ungrounded-claim audit as anything else said mid-retrieval. Speed is not permitted to become a loophole. |
| 10 | Tools and tech stack | content | supporting | valley | 左标题+右内容 | Table + FAIcon列表 | evidence | 字数约260 / 图0 / 留白约16% | 禁止等宽四卡；禁止把依赖清单堆成无分组的纯文本墙 | The seven declared tools split into read-only and state-modifying, and the stack that carries them — FastAPI, PostgreSQL 17 with pgvector, Redis, Gemini Live, Docker Compose, pytest. A drift guard fails the build if a declared tool is missing from the prompt. |
| 11 | What is new, and what we did not solve | content | supporting | peak | 非对称双栏 65:35 | L1: innovation_limits.svg（左 65%） | anchor | 字数约250 / 图1 / 留白约24% | 禁止把创新点写成形容词；禁止把局限写成"未来工作"式的免责声明 | Five innovations with the reason each one is not obvious, then five limitations stated as facts about the artefact — the corpus is public, the benchmark is simulated, the image parser is rule-based, ranking quality is inherited, the 250 ms budget is met because the sentence is local. |
| 12 | Close | ending | hero | peak | 全屏视觉+大标题 | L1: close_field.svg（满幅底） | atmosphere | 字数约45 / 图1 / 留白约45% | 禁止四卡片预览；禁止铺满正文段落；禁止二维码/致谢堆砌 | One sentence, the tag name that judges will look for, and where to find the evidence. Nothing else. |

## Self-check against the STORY checklist

- Hero pages: 3 of 12 (25%) — inside 20–30%, none adjacent. ✅
- No run of three consecutive `supporting + valley`. ✅
- `N卡片横排` used 0 times (limit 2). ✅
- Asymmetric layouts: 11 of 12 (92%), floor 40%. ✅
- No two adjacent pages share a layout. ✅
- `左大图+右侧文字` + `非对称双栏` = 4 of 12 (33%), ceiling 40%. ✅
- Every page carries `role`, `rhythm`, `visual_role`, `anti_pattern`. ✅
- Every page with a number states what the number means, not just the number. ✅
- No catalogue is declared, so no divider pages are owed. ✅
