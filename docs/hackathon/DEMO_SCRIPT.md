# Five-Minute Demo Script

This conversation uses records known to exist in the demonstration database.

## 1. Start with a grounded search

Say:

> Find finance candidates with accounting, auditing, financial reporting and at
> least five years of experience.

Expected evidence: `Finance Candidate 4841` is a strong result with 11 years of
experience and the requested finance skills. Open **Activity** to show the
vector, keyword, skill and SQL branches joining at fusion and reranking.

## 2. Add a constraint while work is active

Interrupt the assistant while it is explaining:

> Actually, restrict that to India.

Expected behaviour: immediate acknowledgement, a new revision, preserved skill
and experience slots, and cancellation/restart only for branches invalidated by
the eligibility change.

## 3. Correct only one slot

Say:

> Wait, remove the entire location restriction, but keep every other
> requirement.

Expected behaviour: `city`, `country` and preferred locations clear together;
skills and minimum experience remain. The next authoritative snapshot and
result cards must agree on the updated candidate count.

## 4. Force slow-path evidence reasoning

Say:

> Compare the top two candidates. Use their actual retrieved evidence, list
> strengths and missing requirements, and recommend one.

Expected behaviour: `compare_candidates` receives the real candidate IDs from
the active result set. Tool activity appears before the model's conclusion.

## 5. Demonstrate an idempotent write

Say:

> Shortlist the stronger candidate.

Then repeat exactly once:

> Shortlist that same candidate again.

Expected behaviour: the shortlist contains one entry. The effect log distinguishes
the first `applied` call from the idempotent `replayed` retry.

## 6. Demonstrate multimodal grounding

Upload a PNG screenshot of a job description and say:

> Use this role image. Required skills are mandatory; treat everything else as
> preferred, and tell me which visible requirements the database cannot enforce.

Expected behaviour: the vision slow path extracts visible text, invokes
`use_role_image`, and reports unsupported constraints honestly. Uploading a WAV
also exercises the media protocol without requiring live microphone input.

## What judges should watch

- Speech can be interrupted without losing the conversation.
- A bare barge-in does not discard valid retrieval.
- A changed slot creates a continuation revision rather than a new session.
- Tool calls precede claims about tool results.
- Speculative state is marked non-authoritative.
- Candidate cards, spoken response and evidence graph agree.
- The Activity graph exposes inputs, outputs, timings, cancellations and raw
  events without obstructing the default conversation UI.
