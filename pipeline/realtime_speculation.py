"""Speculative pre-retrieval from a partial voice transcript.

Theme 5 asks for full-duplex behaviour: retrieval should begin before the user
finishes speaking. This module decides when an in-progress transcript is stable
enough to be worth searching and converts it into a provisional plan. The session
then warms its revision cache with that plan, so evidence already exists by the
time the conversation model issues its authoritative tool call.

The rules here are deliberately conservative. A speculative search on an
unstable fragment wastes database work and can surface evidence that does not
match the final intent, so this module would rather wait than guess.
"""

from __future__ import annotations

import re

from pipeline.aliases import normalize_location
from pipeline.realtime_plan import SearchPlanRevision

MIN_WORDS = 3
MAX_QUERY_WORDS = 60

# Words that reliably signal a corpus-seeking request rather than small talk.
RETRIEVAL_CUES = re.compile(
    r"\b(find|search|show|look(?:ing)?\s+for|need|want|get\s+me|give\s+me|list|"
    r"candidates?|engineers?|developers?|designers?|managers?|analysts?|scientists?|"
    r"profiles?|resumes?|skills?|experience)\b",
    re.IGNORECASE,
)

# A trailing function word means the speaker is mid-phrase. Searching now would
# index a fragment that cannot match the final intent.
DANGLING_TAIL = re.compile(
    r"\b(?:and|or|with|who|that|the|a|an|in|at|for|plus|also|but|of|to|on|from|"
    r"having|experienced|experience|years?|located|based|looking|needing)\s*$",
    re.IGNORECASE,
)

# Trailing filler that adds no retrieval signal once the prefix is settled.
TRAILING_FILLER = re.compile(r"\b(?:please|thanks|thank you|okay|ok|um|uh|er)\b\.?$", re.IGNORECASE)

_LOCATION_LONG = re.compile(
    r"\b(?:located|based|working|available)\s+in\s+([A-Za-z][A-Za-z .'-]{1,40}?)"
    r"(?=\s+(?:with|who|and|plus|having)\b|[,.;]|$)",
    re.IGNORECASE,
)
_LOCATION_CITY_COUNTRY = re.compile(
    r"\bin\s+([A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,2})\s*,\s*"
    r"([A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,2})"
    r"(?=\s+(?:with|who|and|plus|having|for)\b|[.;]|$)"
)
_LOCATION_SHORT = re.compile(
    r"\bin\s+([A-Z][A-Za-z.'-]*(?:\s+[A-Z][A-Za-z.'-]*){0,2})"
    r"(?=\s+(?:with|who|and|plus|having|for)\b|[,.;]|$)"
)

# This parser only recovers cities. Country filters remain the conversation
# model's responsibility, so a country-only phrase must never be mislabeled as
# a city merely because it follows "in".
_COUNTRY_TERMS = {
    "argentina", "australia", "brazil", "canada", "china", "france",
    "germany", "india", "ireland", "italy", "japan", "mexico",
    "netherlands", "new zealand", "singapore", "south africa", "spain",
    "sweden", "switzerland", "uae", "uk", "united arab emirates",
    "united kingdom", "united states", "us", "usa",
}


def is_speculatable(text: str) -> bool:
    """Return True when the current prefix is stable enough to search."""
    clean = " ".join(text.split())
    words = clean.split()
    if len(words) < MIN_WORDS or len(words) > MAX_QUERY_WORDS:
        return False
    if DANGLING_TAIL.search(clean):
        return False
    return bool(RETRIEVAL_CUES.search(clean))


def plan_from_partial(text: str) -> SearchPlanRevision | None:
    """Build a provisional plan from a partial transcript.

    Only the semantic query and an explicitly spoken location are inferred. Hard
    skill filters are deliberately not guessed: the conversation model resolves
    canonical skill names through `list_skills`, and a speculative guess would
    both risk a zero-result filter and reduce the chance that the speculative
    fingerprint matches the model's final plan.
    """
    clean = TRAILING_FILLER.sub("", " ".join(text.split())).strip(" .,!?")
    if not clean:
        return None

    city = None
    pair = _LOCATION_CITY_COUNTRY.search(clean)
    match = pair or _LOCATION_LONG.search(clean) or _LOCATION_SHORT.search(clean)
    if match:
        candidate = normalize_location(match.group(1).strip(" .,'\""))
        if candidate not in _COUNTRY_TERMS:
            city = candidate

    return SearchPlanRevision.create(query=clean, city=city)
