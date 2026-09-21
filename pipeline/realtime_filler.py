"""Grounded filler: what the agent may say while a retrieval is still running.

Theme 5 asks the agent to keep the conversation alive without losing logical
depth. Those two goals pull against each other. Waiting for retrieval keeps the
answer grounded but produces dead air; talking over retrieval keeps the
conversation alive but invites the model to fill the gap with a guess.

The resolution is a *grounded* filler: the agent may speak while a tool is in
flight, but only about the criteria it just sent, which it authored and which
cannot be wrong. Any statement about the *outcome* of a retrieval is a claim the
agent cannot support until the tool response arrives, so this module classifies
it as a violation.

Grounding is enforced strictly. Length is reported but not enforced: a slightly
long sentence that asserts nothing false is a style problem, not a correctness
one, and treating it as a violation would push the agent back toward silence.
"""

from __future__ import annotations

import re
from typing import Any

# A phrase that asserts an outcome. Any of these before the tool response is a
# claim the agent has no evidence for yet.
OUTCOME_CLAIM = re.compile(
    r"\b(?:"
    r"i(?:'ve|\s+have)?\s+(?:found|got|pulled|located|identified|shortlisted)"
    r"|there\s+(?:are|is|were)"
    r"|here\s+(?:are|is)"
    r"|we(?:'ve|\s+have)?\s+(?:got|found)"
    r"|showing"
    r"|matched"
    r"|returned"
    r"|came\s+back"
    r")\b",
    re.IGNORECASE,
)

# A number attached to a result noun is a count claim even without a verb:
# "three candidates" asserts an outcome as plainly as "I found three candidates".
COUNT_CLAIM = re.compile(
    r"\b\d+\s+(?:candidates?|matches?|profiles?|people|engineers?|developers?|"
    r"designers?|managers?|results?|hits?)\b",
    re.IGNORECASE,
)

MAX_FILLER_WORDS = 30

# Words that are both ordinary English and common given names. A capitalized
# occurrence of one of these is not evidence that a candidate was named, so the
# single-token name check skips them. This is a deliberately small list: it only
# needs to cover words that plausibly appear in a recruiting sentence.
AMBIGUOUS_NAME_WORDS = frozenset({
    "able", "art", "best", "bill", "bright", "can", "chase", "clark", "cook",
    "dawn", "day", "dean", "drew", "even", "faith", "fast", "first", "free",
    "grace", "grant", "hard", "hope", "hunter", "joy", "june", "key", "king",
    "knight", "light", "long", "main", "mark", "mason", "may", "new", "noble",
    "page", "parker", "prince", "quick", "rain", "rich", "rose", "sharp",
    "short", "sky", "small", "smart", "star", "still", "strong", "summer",
    "sun", "swift", "tall", "then", "true", "well", "west", "will", "winter",
    "young",
})


def classify_filler(
    text: str,
    *,
    candidate_names: tuple[str, ...] | list[str] = (),
) -> dict[str, Any]:
    """Classify one chunk of agent speech emitted while a tool is in flight.

    Returns a report rather than a verdict, because the caller decides what to
    do with an over-budget sentence. ``grounded`` is the safety property: a
    report with ``grounded=False`` means the agent asserted an outcome it could
    not yet know.
    """
    spoken = " ".join(str(text or "").split())
    word_count = len(spoken.split())
    if not spoken:
        return {
            "kind": "empty",
            "grounded": True,
            "within_budget": True,
            "word_count": 0,
            "reason": None,
        }

    leaked = _leaked_name(spoken, candidate_names)
    if leaked is not None:
        return _report("violation", word_count, "names_a_candidate_before_evidence", leaked)
    # A count is the least ambiguous outcome signal, so it is reported as such
    # even when the sentence also contains a claim verb like "matched".
    if COUNT_CLAIM.search(spoken):
        return _report("violation", word_count, "states_a_result_count_before_evidence")
    if OUTCOME_CLAIM.search(spoken):
        return _report("violation", word_count, "claims_a_result_before_evidence")

    if word_count > MAX_FILLER_WORDS:
        return _report("over_budget", word_count, "longer_than_filler_budget")
    return _report("acknowledgement", word_count, None)


def _report(kind: str, word_count: int, reason: str | None, matched: str | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "kind": kind,
        "grounded": kind != "violation",
        "within_budget": word_count <= MAX_FILLER_WORDS,
        "word_count": word_count,
        "reason": reason,
    }
    if matched is not None:
        report["matched_name"] = matched
    return report


def _leaked_name(spoken: str, candidate_names: tuple[str, ...] | list[str]) -> str | None:
    """Return a candidate name mentioned before any evidence arrived, if any.

    Two checks, because the agent will naturally use a first name:

    * the full name, matched case-insensitively;
    * a single name token, matched only where the transcript capitalised it and
      it is not a word that is also ordinary English.

    The capitalisation requirement is what keeps this precise. Transcription
    preserves proper-noun casing, so "I think Ada would fit" is caught while
    "I will look for python engineers" is not. The cost is a fail-open: if a
    provider lowercases the transcript, a first-name leak is missed rather than
    every "will" and "may" being reported as one. That is the right trade for an
    advisory audit — the prompt, not this check, is what stops the agent
    speaking an ungrounded name in the first place.
    """
    for name in candidate_names:
        clean = " ".join(str(name or "").split())
        if len(clean) < 3:
            continue
        if re.search(rf"(?<!\w){re.escape(clean)}(?!\w)", spoken, re.IGNORECASE):
            return clean
    for name in candidate_names:
        clean = " ".join(str(name or "").split())
        for token in clean.split():
            if len(token) < 3 or token.casefold() in AMBIGUOUS_NAME_WORDS:
                continue
            if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", spoken):
                return clean
    return None
