# DESIGN.md — Talent Atlas submission deck

Style direction chosen: **C · Teal engineering**. Emphasis: **proof it works**.
Density: **presentation — one idea per page**. Universal design principle applies
(this is a hackathon pitch, not one of the three preset verticals), so this file
is the project's design constitution.

## 1. Canvas and master

Canvas **1280 × 720**. Page padding **上下 20px / 左右 72px**. All three zones sit
inside that padding.

| Zone | Vertical | Height | Rule |
| :--- | :--- | :--- | :--- |
| **A · title block** | 0–120px | 120px (incl. 20 top padding) | One page title, 34px bold, left-aligned, never centred. A 6px lime rule sits above it. |
| **B · content** | 120–660px | 540px usable | All body, diagrams, tables, charts. Nothing may bleed into A or C. |
| **C · footer** | 660–720px | 60px (incl. 20 bottom padding) | Left: `Talent Atlas · Theme 05` in 14px grey. Right: page number `NN / 12`, same size, same weight, same position on every page. |

Cover (01), the peak page (11) and close (12) may use a custom composition, but
the footer still appears on 02–11 so the deck reads as one document.

**Content budget check (every content page):** `20 + 120 + B + 20 ≤ 720`, so
`B ≤ 540`. Every page below is authored to a stated B height and must be checked
against it before it ships.

## 2. Colour

Four chromatic hexes. No fifth colour is permitted anywhere, including inside
diagrams, charts and tables.

| Role | Hex | Where |
| :--- | :--- | :--- |
| **Primary** deep teal | `#04342C` | Title blocks on peak pages, the cover field, dark panels, the audit-trail surface |
| **Secondary** mid teal | `#0F6E56` | Card surfaces, diagram nodes, table headers, the second chart series |
| **Accent** lime | `#97C459` | **Focal only** — the single number or word the eye must land on, the active branch in a diagram, the 6px rule above each title |
| **Pale** mint | `#E1F5EE` | Light card fills, diagram backdrops, table zebra, the `authoritative=False` surface |

Neutrals: `#FFFFFF` page, `#0B1F1A` body text, `#6B7280` footer and captions.

### 2.1 Area allocation

| Page class | Primary | Secondary | Accent | Neutral |
| :--- | :--- | :--- | :--- | :--- |
| Peak / hero (01, 03, 08, 11, 12) | 45–60% | ≤ 20% | **15–20%** | remainder |
| Supporting (02, 04, 05, 06, 07, 09, 10) | ≤ 35% | ≤ 25% | **≤ 5%** | remainder |

Two pages must be visibly different from the rest in colour weight: 03 (accent
burst on the results) and 12 (near-total primary field). No two adjacent pages
may share the same dominant colour class.

**Accent discipline.** Lime is spent once per page, on the one thing that page is
about. On 03 it is the `670`. On 06 it is the two cancelled branches. On 08 it is
`applied`. If a page has lime in two places, one of them is wrong.

### 2.2 Gradients and translucency

- Cover and close fields: `linear-gradient(140deg, #04342C 0%, #0F6E56 100%)`.
- Diagram backdrops: `linear-gradient(180deg, #E1F5EE 0%, #FFFFFF 100%)`.
- Overlay on the cover field: `rgba(4,52,44,0.55)` under the title so the
  riding text stays legible.
- Cancelled / withdrawn elements: `opacity: 0.35` plus `textDecoration:
  'line-through'` — a withdrawn branch must look withdrawn, not merely absent.
- Cards lift with `boxShadow: '0 4px 20px rgba(4,52,44,0.08)'`.

## 3. Type

Two families only.

| Family | Used for | Why |
| :--- | :--- | :--- |
| **Inter** | Titles, body, captions | Neutral, high legibility at 18–34px |
| **JetBrains Mono** | Every measurement, identifier, branch name, audit entry, slot name, tag name | The deck is full of numbers that are evidence. Monospace signals "this is a reading, not a slogan", and satisfies the rule that anchor numbers must not use the body face. |

| Level | Size | Weight | Line height | Face |
| :--- | :--- | :--- | :--- | :--- |
| Cover title | 76px | bold | 1.1 | Inter |
| Close statement | 56px | bold | 1.15 | Inter |
| **Anchor number** | **96–132px** | **bold** | **1.0** | **JetBrains Mono** |
| Page title (A zone) | 34px | bold | 1.25 | Inter |
| Card heading | 24px | 600 | 1.3 | Inter |
| Body | 19px | regular | 1.55 | Inter |
| Data / code / identifiers | 15–18px | regular | 1.5 | JetBrains Mono |
| Footer, captions | 14px | regular | 1.4 | Inter |

## 4. Density gates

Universal defaults apply (regular pages leave ≤ 35% white; cards fill ≥ 85% of
their height; sibling cards align on the three bands). Two project-specific
overrides, both declared here:

- **Cover (01) and close (12) are declared "breathing" pages** — 42–45% white is
  intended, and the white is grouped around the title rather than spread evenly.
- **Page 03 is a declared hero data page** — 36% white, all of it around the
  anchor numbers, none of it between the numbers and their captions.

Card fill: a card taller than 380px must carry ≥ 100 words of body; taller than
480px, ≥ 130. Tail elements inside a card (the enforcement chip on 04, the
limitation tag on 11) are pinned to the card floor with `marginTop: 'auto'`, never
left floating after the last paragraph.

## 5. Imagery

**Declaration: this deck contains no photography and no illustration.** Every L1
is a structural SVG — architecture, sequence, state shape, audit trail, timeline.
The subject is the control flow of a software system, which has no physical
referent to photograph, and the design principle permits SVG for exactly this
class (架构图 / 流程图 / 时序图 / 图表底图 / 巨型数字). Using generated imagery
here would add decoration that competes with the evidence.

| Level | Instances | Placement | Minimum size |
| :--- | :--- | :--- | :--- |
| **L1** | `cover_field.svg`, `architecture.svg`, `cancellation.svg`, `audit_trail.svg`, `fastpath_timeline.svg`, `innovation_limits.svg`, `close_field.svg` | Full-bleed field, or one column of the content zone | ≥ 40% of zone B, or full bleed |
| **L2** | `problem_collapse.svg`, `snapshot_shape.svg` | Beside body copy | ≥ 280 × 180 |
| **L3** | 6px lime rule above every page title + the footer wordmark | Identical position on all 12 pages | ≤ 64px tall |

All SVGs are inline in the page source, so they inherit the four hexes and cannot
drift from the palette. No file in `assets/` is referenced by the deck; the
directory is reserved in case a raster is added later.

## 6. Page map

| # | file | type | role | layout | L1 / visual | words | white | colour allocation | key constraint |
| :- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 01 | 01.slide | cover | hero | 全幅图+骑线文字 | L1 `cover_field.svg` full bleed | ~50 | 42% | primary 60% + accent 18% | Left-bleed field; title rides into it; theme badge is a real element, not a title-block sticker |
| 02 | 02.slide | content | supporting | 非对称双栏 60:40 | L2 `problem_collapse.svg` (right 40%) | ~240 | 20% | primary 30% + secondary 20% + accent 5% | 60:40, never 50:50; the right column must visually show four things becoming one |
| 03 | 03.slide | content | hero | 巨型数字+洞察 | anchor ×3 (≥96px) + Chart | ~150 | 36% | primary 45% + accent 20% | Three anchor numbers, each with a caption that says what it means; chart is secondary to the numbers |
| 04 | 04.slide | content | supporting | 左标题+右内容 | L3 + enforcement chips | ~280 | 16% | primary 25% + secondary 25% + accent 5% | Narrow left rail (32%); every row ends with the named check, pinned to the card floor |
| 05 | 05.slide | content | supporting | 上大图+下方卡片 | L1 `architecture.svg` (B 55%) | ~190 | 18% | primary 35% + secondary 20% + accent 5% | Diagram is the evidence; the two decision cards below it are equal height and aligned |
| 06 | 06.slide | content | supporting | 非对称双栏 65:35 | L1 `cancellation.svg` (left 65%) | ~210 | 20% | primary 30% + secondary 25% + accent 5% | Cancelled branches struck through at 0.35 opacity; preserved branches must be equally visible |
| 07 | 07.slide | content | supporting | 左标题+右内容 | L2 `snapshot_shape.svg` | ~230 | 18% | primary 25% + secondary 25% + accent 5% | `unset_slots` must be shown, not summarised — the whole point is that empty ≠ dropped |
| 08 | 08.slide | content | hero | 非对称双栏 60:40 | L1 `audit_trail.svg` (left 60%) | ~200 | 20% | primary 55% + accent 18% | Dark primary panel; both log lines shown, the applied one accented; the bug is stated plainly |
| 09 | 09.slide | content | supporting | 上大图+下方卡片 | L1 `fastpath_timeline.svg` (B 55%) | ~200 | 20% | primary 30% + secondary 25% + accent 5% | The 250 ms budget must appear on the same axis as the 2.6 ms reading, or the number is meaningless |
| 10 | 10.slide | content | supporting | 左标题+右内容 | Table + grouped list | ~260 | 16% | primary 25% + secondary 25% + accent 5% | Two groups only: read-only and state-modifying; the stack is grouped, never a flat dependency wall |
| 11 | 11.slide | content | supporting | 非对称双栏 65:35 | L1 `innovation_limits.svg` (left 65%) | ~250 | 24% | primary 45% + accent 15% | Innovations left, limitations right; each limitation is a fact about the artefact, not "future work" |
| 12 | 12.slide | ending | hero | 全屏视觉+大标题 | L1 `close_field.svg` full bleed | ~45 | 45% | primary 70% + accent 15% | One sentence, the tag name, and where the evidence lives. Nothing else |

**Budget audit.** Hero 3/12 = 25%. Asymmetric 11/12 = 92%. `非对称双栏` ×4 and
`左大图+右侧文字` ×0 = 4/12 = 33% (ceiling 40%). Symmetric ×1 (ceiling 2).
`N卡片横排` ×0. No adjacent pair shares a layout.
