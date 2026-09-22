# Talent Atlas — Samsung PRISM Y2026 Theme 05 Strategy

**Theme 05 — Interruptible Real-Time Agents**

## Thesis

An assistant that stays responsive while it thinks, and recovers cleanly when
the plan changes, is mostly a *bookkeeping* problem that nobody does the
bookkeeping for.

Anyone can make an agent that answers well when left alone. The hard part is
that a real recruiter interrupts mid-sentence, changes one constraint, keeps
four others, and then corrects a write it already made. Most systems handle that
in one of two ways: they go silent while they reason, or they throw the plan away
and start over. Both are visible to the user as the same thing — the assistant
stopped being useful the moment it got interesting.

Our answer is that every turn publishes what it currently believes, in a form
that is precise enough to act on. Interruptions then become cheap: you compare
two states and re-run only what the difference actually invalidated. That is the
whole design, and most of this document is about what it costs to be honest
about it.

## The problem in our own words

The brief asks for an agent that keeps the conversation alive without losing
logical depth, handles interruptions while still driving toward the goal, adapts
to goal changes without losing session context, and is wrapped in a harness safe
and reliable enough to trust. Read together, those are one requirement:

> At any instant, the system must be able to state what it is doing, what it
> believes, and which of its in-flight actions are still valid — and it must
> never say something it cannot yet back with evidence.

That framing is what made the work tractable. "Be interruptible" is not
testable. "Publish a state snapshot on every turn event, and prove that a
revision cancels only the work it invalidated" is.

Three consequences fell out of it, and they shaped the implementation:

**Speech and retrieval must overlap.** If the agent waits for retrieval before
speaking, it is silent exactly when the user is most likely to interrupt. So
retrieval runs in the background and the agent acknowledges the *instruction*
immediately. That creates a new hazard — an agent that talks during retrieval can
claim a result it does not have — so the acknowledgement is generated from the
instruction diff, never from the corpus, and it is audited by the same check that
judges any other mid-retrieval speech.

**Cancellation has to be selective.** Cancelling everything on every revision
destroys work that is still valid; preserving everything runs queries the user
already invalidated. The only way to choose is to know what each piece of work
depends on, so the dependency has to be explicit in the code rather than implied
by a call graph.

**A write must be provable.** Once an agent can change stored state, "it probably
only happened once" is not good enough, and neither is an audit log that cannot
demonstrate it.

## Architecture

```
                        ┌──────────────────────────────────────────┐
  audio / transcript ──▶│ Gemini Live  (full duplex, interruptible)│
  role image      ──▶   └───────────────┬──────────────────────────┘
                                        │ typed tool calls
                        ┌───────────────▼──────────────────────────┐
                        │ RealtimeToolDispatcher                   │
                        │  manifest: effect · blocking · scope ·   │
                        │            idempotent · aliases          │
                        │  validates → dedupes → supersedes        │
                        └───────┬──────────────────────┬───────────┘
                                │                      │
        ┌───────────────────────▼──────┐    ┌──────────▼─────────────┐
        │ SearchPlanRevision           │    │ add_to_shortlist       │
        │  filter_fingerprint          │    │  the only write        │
        │  branch_fingerprints × 4     │    │  idempotency + scope   │
        │  plan_fingerprint            │    │  supersession          │
        └───────┬──────────────────────┘    └──────────┬─────────────┘
                │                                      │
        ┌───────▼──────────────────────────────┐       │
        │ BranchExecutor                       │       │
        │  per branch: reuse | preserve |      │       │
        │              cancel | start          │       │
        └───────┬──────────────────────────────┘       │
                │                                      │
        ┌───────▼──────────────────────────────┐       │
        │ Hybrid retrieval (one shared SQL     │       │
        │ eligibility CTE, inlined per branch) │       │
        │  vector · bm25 · skills · sql        │       │
        └───────┬──────────────────────────────┘       │
                │                                      │
        ┌───────▼──────────────────────────────┐       │
        │ RRF fusion → rerank → diversity      │       │
        └───────┬──────────────────────────────┘       │
                │                                      │
        ┌───────▼──────────────────────────────────────▼─────────────┐
        │ StateSnapshot  {intent, slots, revision, changed_fields,   │
        │                 status, authoritative, branches}           │
        │ SnapshotJournal → every turn event, in order               │
        └───────┬────────────────────────────────────────────────────┘
                │
        ┌───────▼──────────────────────────────┐
        │ RealtimeProtocolAdapter              │
        │  in : transcript · interrupt · call  │
        │  out: call · cancellation · ack ·    │
        │       clarification · snapshot       │
        └──────────────────────────────────────┘
```

Two design decisions in that diagram carry most of the weight.

**The shared eligibility clause is modelled explicitly.** The hard filter is
built once and inlined as a CTE into *every* branch query. It is therefore a
cross-branch dependency, not a per-branch input, and the fingerprint model says
so: `filter_fingerprint` is folded into each branch fingerprint. A query rewrite
moves only `{vector, bm25}`; a change to the hard filter moves all four, because
rows fetched under a different eligibility clause are not the same rows.

**A snapshot is either authoritative or it is not.** Speculative work — retrieval
started from a partial transcript before the utterance ended — publishes
`authoritative=False`. A guess about what the user is about to say can never be
mistaken for a decision they made, and the turn cannot be closed on one.

## Acceptance-gate mapping

| Theme 05 requirement | Product evidence | Automated check |
|---|---|---|
| Full duplex: begin retrieving before the utterance ends | retrieval starts from a settled transcript prefix, before the model's tool call | `benchmark`: `calls_before_tool_call == 1`, `calls_added_by_tool_call == 0`, `served_from_speculation` |
| Keep the conversation alive without losing depth | instruction-derived acknowledgement emitted before retrieval returns, audited for ungrounded claims | `evaluate_realtime_agent.py` `filler_budget` + `fast_path_acknowledgement` |
| Handle and recover from real-time interruptions | branch-granular cancellation; three barge-ins cause no cancellation and no restart | `selective_cancellation`, `burst_barge_in` |
| Adapt to goal changes without losing session context | refinement patches the plan; replacement starts clean, carries `previously_surfaced` and reports `dropped_constraints` | `slot_retention`, plus the goal-change checks in the evaluation script |
| Drive toward the end goal | tool manifest classifies retrieval as non-blocking and everything that answers a question as blocking | `tool_behavior` policy assertions |
| Solid, safe, reliable harness | typed bounded tools, dynamic manifest, idempotent write, scope supersession, audit log | `test_realtime_tools.py`, `duplicate_call_id`, `idempotent_write`, `superseded_write` |
| Optimising latency without compromising reasoning | fast path measured against a budget; virtual-clock cancellation latency | `fast_path_acknowledgement` (`< 250 ms`), `selective_cancellation` |
| Session-scoped memory only | no recruiter id or profile is passed to the engine; state is per-session and discarded on close | `dropped_transport`, session-close tests |
| Multimodal input and output | role image parsed into plan slots; audio output | `test_realtime_vision.py`, `vision.role_attached` |
| Fault tolerance | malformed calls rejected without touching state; failing tool surfaced then recovered | `malformed_arguments`, `tool_failure` |

Everything above is executed by `scripts/benchmark_realtime_interruptions.py`
(12 scenarios) and `scripts/evaluate_realtime_agent.py`, both of which exit
non-zero on failure, plus the `tests/` suite.

## Innovation highlights

**Selective branch cancellation.** The dependency that matters — the shared
eligibility clause — is usually invisible, implied by the order of statements in
a function. Making it an explicit fingerprint is what turns "cancel the stale
work" from a heuristic into a decision with a reason.

**An audit log that proves its own guarantee.** The log records `applied` when an
effect lands, not only when a retry replays it. An idempotency claim whose log
shows the retry but not the effect is not a proof, and this was a real bug we
found by trying to read the guarantee out of the structure meant to express it.

**Grounded filler with a fast path that cannot cheat.** The acknowledgement is
generated from the instruction diff and passes the same ungrounded-claim audit as
anything else the agent says mid-retrieval. Speed is not allowed to become a
loophole.

**Visual grounding with honest limits.** A role image is parsed into plan slots,
but requirements the database cannot enforce — a degree, say — are reported as
unenforceable and are never described as a filter that was applied.

**A deterministic benchmark.** Branch lifecycle runs on a virtual clock, so a
cancellation latency is a measurement rather than a coincidence. Only the two
wall-clock claims use real time.

## What we deliberately did not build

The brief puts speech synthesis quality, wake-word detection and UI polish out of
scope, and we took that seriously: the voice transport is Gemini Live's, the
input may be a transcript, and the panel is an evaluation cockpit rather than a
product surface. Effort went into the harness, because that is what the theme
grades.

## Honest scope and limitations

- **The corpus is a transformed public recruitment dataset**, not a production
  hiring corpus. Retrieval quality numbers describe this dataset only.
- **The benchmark is deterministic and simulated.** It exercises the real
  session, dispatcher, branch executor and protocol adapter, but with a
  scripted engine on a virtual clock. It proves the *control* behaviour
  (what was cancelled, preserved, deduplicated, reported), not database
  performance at scale.
- **The role-image parser is rule-based**, not a vision model. It handles
  requirement extraction and explicit exclusions well and will not generalise to
  arbitrary layouts; the interface is where a vision model would plug in.
- **Retrieval quality is inherited from the existing engine.** This work is about
  interruption and state, and it neither claims nor measures an improvement in
  ranking quality.
- **The 250 ms acknowledgement budget is met with room to spare** (measured
  ~1 ms) because the sentence is generated locally from the diff. A hosted model
  generating it would need its own budget.

## Reproducing the claims

The graded surface needs no database, no network and no API keys:

```bash
python -m pytest tests -q                              # 653 tests
python scripts/benchmark_realtime_interruptions.py     # 12 scenarios, exit 0
python scripts/evaluate_realtime_agent.py              # pass/fail gate, exit 0
```

The full voice demo additionally needs PostgreSQL (see the README) and
`GOOGLE_API_KEY` for the live audio transport. Typed search works without it.

The earlier Theme 04 research brief in `docs/hackathon/research/` predates this
document and is scoped to Streaming Live RAG; several of its acceptance gates
(early retrieval, state continuity) are still met, but this document supersedes
it as the statement of what is being submitted.
