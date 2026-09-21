"""Visual grounding: turning a role image into plan inputs the harness can enforce.

A recruiter holds up a job description and says "use this role". The image
arrives as an attachment, a vision step turns it into text, and the harness has
to answer three questions about it that have nothing to do with vision:

1. **Which of these requirements can the search actually enforce?** The candidate
   index has location, experience, skills and status. It has no degree column. A
   requirement that no plan slot can express must be reported as unenforceable
   rather than quietly dropped, because an agent that says "filtering on a
   Bachelor's degree" is claiming a filter that does not exist.

2. **Which are required and which are preferred?** A skill read off an image is a
   guess about the recruiter's intent, and a guess promoted to a hard filter
   produces zero results. Image-derived skills therefore default to *preferred*,
   and only explicit requirement language promotes them.

3. **What happens when the recruiter changes their mind?** "Use this role, but
   ignore the degree requirement" must remove exactly one requirement and be able
   to say which one it removed, while the rest of the image context stays
   available for the next turn.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from pipeline.aliases import KNOWN_SKILLS, normalize_location, normalize_skill

RequirementCategory = Literal["experience", "skill", "degree", "location", "role", "other"]

# Categories no plan slot can express. Reported, never silently dropped and
# never described to the recruiter as an active filter.
UNENFORCEABLE_CATEGORIES = frozenset({"degree"})

# Tokens may contain + and # ("c++", "c#") and internal dots ("node.js"), but a
# trailing dot is sentence punctuation and must not become part of a skill name.
_TOKEN = re.compile(r"[A-Za-z][A-Za-z+#]*(?:\.[A-Za-z+#]+)*")
_LOCATION_TOKEN = re.compile(r"[A-Za-z][A-Za-z'\-]*")

_YEARS = re.compile(r"(\d{1,2})\s*\+?\s*(?:to\s*(\d{1,2})\s*)?(?:years?|yrs?)\b", re.IGNORECASE)
_WORD_YEARS = re.compile(
    r"\b(three|four|five|six|seven|eight|nine|ten)\s+years?\b", re.IGNORECASE
)
_WORD_NUMBERS = {
    "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_DEGREE = re.compile(
    r"\b(bachelor'?s?|master'?s?|phd|doctorate|degree|graduat\w+|b\.?s\.?c?|m\.?s\.?c?|"
    r"b\.?tech|m\.?tech)\b",
    re.IGNORECASE,
)
_REQUIRED = re.compile(
    r"\b(required|requirement|must(?:\s+have)?|essential|mandatory|proficien\w+|"
    r"strong\s+experience|expertise|hands[- ]on)\b",
    re.IGNORECASE,
)
_PREFERRED = re.compile(
    r"\b(preferred|nice\s+to\s+have|plus|bonus|familiar\w*|exposure|advantage|desirable)\b",
    re.IGNORECASE,
)
_ROLE_TITLE = re.compile(
    r"\b(?:senior|staff|principal|lead|junior|mid[- ]level|head\s+of)?\s*"
    r"([A-Za-z][A-Za-z /+-]{2,40}?(?:engineer|developer|designer|manager|analyst|"
    r"scientist|architect|administrator|consultant|specialist|lead|director))\b",
    re.IGNORECASE,
)
_LOCATION_HINT = re.compile(r"\b(remote|hybrid|onsite|on-site)\b", re.IGNORECASE)
_CLAUSE_SPLIT = re.compile(r"[\n\r•·▪◦‣*]+|(?<=[.;])\s+")

# Locations the harness will accept from an image. Kept small on purpose: a
# location guessed from prose becomes a hard filter, and a wrong hard filter is
# a zero-result search.
_KNOWN_LOCATIONS = frozenset({
    "bangalore", "bengaluru", "pune", "mumbai", "delhi", "new delhi", "hyderabad",
    "chennai", "kolkata", "gurgaon", "gurugram", "noida", "ahmedabad", "jaipur",
    "kochi", "coimbatore", "indore", "chandigarh", "lucknow", "nagpur",
    "london", "manchester", "birmingham", "edinburgh", "dublin", "berlin",
    "munich", "hamburg", "paris", "amsterdam", "madrid", "barcelona", "rome",
    "zurich", "stockholm", "oslo", "copenhagen", "vienna", "warsaw", "lisbon",
    "new york", "san francisco", "seattle", "boston", "chicago", "austin",
    "denver", "atlanta", "los angeles", "toronto", "vancouver", "montreal",
    "singapore", "dubai", "tokyo", "sydney", "melbourne", "auckland",
    "india", "usa", "uk", "germany", "france", "netherlands", "spain",
    "canada", "australia", "ireland", "italy", "sweden",
})


class Requirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    category: RequirementCategory
    skills: list[str] = Field(default_factory=list)
    min_years_exp: int | None = None
    required: bool = False
    enforceable: bool = True


class RoleImage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_id: str
    source: str = "transport"
    received_at_ms: float = 0.0
    description: str = ""
    role_title: str | None = None
    requirements: list[Requirement] = Field(default_factory=list)
    slots: dict[str, Any] = Field(default_factory=dict)
    unenforceable: list[str] = Field(default_factory=list)


def _clauses(text: str) -> list[str]:
    parts = [part.strip(" \t-–—:") for part in _CLAUSE_SPLIT.split(text or "")]
    return [part for part in parts if len(part) >= 3]


def _windows(clause: str, pattern: re.Pattern[str], limit: int = 3) -> list[str]:
    """All 1..limit word windows of a clause, longest first."""
    words = pattern.findall(clause)
    windows: list[str] = []
    for size in range(limit, 0, -1):
        for start in range(max(0, len(words) - size + 1)):
            windows.append(" ".join(words[start:start + size]))
    return windows


def _skills_in(clause: str) -> list[str]:
    found: list[str] = []
    for window in _windows(clause, _TOKEN):
        canonical = normalize_skill(window)
        if canonical in KNOWN_SKILLS and canonical not in found:
            found.append(canonical)
    return found


def _locations_in(clause: str) -> list[str]:
    found: list[str] = []
    for window in _windows(clause, _LOCATION_TOKEN):
        canonical = normalize_location(window)
        if canonical in _KNOWN_LOCATIONS and canonical not in found:
            found.append(canonical)
    return found


def _years_in(clause: str) -> int | None:
    match = _YEARS.search(clause)
    if match:
        return int(match.group(1))
    word = _WORD_YEARS.search(clause)
    if word:
        return _WORD_NUMBERS.get(word.group(1).casefold())
    return None


def _role_title_in(clause: str) -> str | None:
    match = _ROLE_TITLE.search(clause)
    return " ".join(match.group(0).split()).casefold() if match else None


def classify_requirement(clause: str) -> Requirement:
    """Classify one requirement clause and extract what it can contribute."""
    skills = _skills_in(clause)
    years = _years_in(clause)
    # A clause that hedges is not a requirement, even if it also says "must".
    required = bool(_REQUIRED.search(clause)) and not _PREFERRED.search(clause)
    if _DEGREE.search(clause):
        category: RequirementCategory = "degree"
    elif years is not None:
        category = "experience"
    elif skills:
        category = "skill"
    elif _locations_in(clause) or _LOCATION_HINT.search(clause):
        category = "location"
    elif _role_title_in(clause):
        category = "role"
    else:
        category = "other"
    return Requirement(
        text=" ".join(clause.split()),
        category=category,
        skills=skills,
        min_years_exp=years,
        required=required,
        enforceable=category not in UNENFORCEABLE_CATEGORIES,
    )


def slots_from_requirements(requirements: list[Requirement]) -> dict[str, Any]:
    """Fold classified requirements into the plan fields the index can enforce."""
    must: list[str] = []
    should: list[str] = []
    years: list[int] = []
    locations: list[str] = []
    roles: list[str] = []
    for item in requirements:
        if not item.enforceable:
            continue
        if item.category == "skill":
            # Explicit requirement language promotes a skill to a hard filter.
            # Everything else stays a preference: a skill read off an image is a
            # guess about intent, and a guessed hard filter returns nothing.
            (must if item.required else should).extend(item.skills)
        elif item.category == "experience" and item.min_years_exp is not None:
            years.append(item.min_years_exp)
        elif item.category == "location":
            locations.extend(_locations_in(item.text))
        elif item.category == "role":
            title = _role_title_in(item.text)
            if title:
                roles.append(title)

    slots: dict[str, Any] = {}
    if must:
        slots["must_skills"] = sorted(set(must))
    if should:
        slots["should_skills"] = sorted(set(should))
    if years:
        slots["min_years_exp"] = min(years)
    if roles:
        slots["should_roles"] = sorted(set(roles))
    if locations:
        slots["should_locations"] = sorted(set(locations))
    return slots


def parse_role_description(
    description: str,
    *,
    image_id: str,
    received_at_ms: float = 0.0,
    source: str = "transport",
) -> RoleImage:
    """Turn an image's extracted text into requirements and plan slots."""
    requirements = [classify_requirement(clause) for clause in _clauses(description)]
    slots = slots_from_requirements(requirements)
    title = None
    for item in requirements:
        if item.category == "role":
            title = _role_title_in(item.text)
            break
    if title is None:
        title = _role_title_in(description or "")
    return RoleImage(
        image_id=image_id,
        source=source,
        received_at_ms=received_at_ms,
        description=" ".join((description or "").split()),
        role_title=title,
        requirements=requirements,
        slots=slots,
        unenforceable=[item.text for item in requirements if not item.enforceable],
    )


# Recruiters name the thing they are removing, not the field name: "ignore the
# degree requirement", "drop the SQL skill", "no location filter". Stripping the
# framing words is what lets one ignore list handle all three phrasings.
_IGNORE_NOISE = re.compile(
    r"^(?:the|a|an)\s+|\s+(?:requirements?|skills?|filters?|hints?|criteria|constraints?)$",
    re.IGNORECASE,
)


def _ignore_terms(raw: list[str] | None) -> list[str]:
    """Expand each ignore entry into the phrasings it could be referring to."""
    terms: list[str] = []
    for entry in raw or []:
        base = " ".join(str(entry or "").split()).casefold()
        if not base:
            continue
        variants = {base}
        stripped = base
        while True:
            trimmed = _IGNORE_NOISE.sub("", stripped).strip()
            if not trimmed or trimmed == stripped:
                break
            variants.add(trimmed)
            stripped = trimmed
        # Longest first, so a specific phrase wins over a bare word it contains.
        terms.extend(sorted(variants, key=len, reverse=True))
    return terms


def _mentions(text: str, term: str) -> bool:
    """Whole-word containment.

    A plain substring test would let "sql" match inside "postgresql", so naming
    one skill would silently drop a requirement about a different one.
    """
    return bool(re.search(rf"(?<!\w){re.escape(term)}(?!\w)", text, re.IGNORECASE))


def apply_role_image(
    image: RoleImage,
    *,
    ignore: list[str] | None = None,
) -> dict[str, Any]:
    """Resolve an image into plan slots, honouring the requirements to ignore.

    An ignore entry can name a category (``"degree"``), a requirement phrase
    (``"the degree requirement"``) or a single skill (``"docker"``). Precision
    matters in that order: a named skill removes just that skill, while a named
    requirement or category removes the clause it belongs to. Dropping the whole
    clause because one skill inside it was named would silently remove
    constraints the recruiter never mentioned.
    """
    terms = _ignore_terms(ignore)
    kept: list[Requirement] = []
    ignored: list[str] = []
    removed_skills: list[str] = []

    for item in image.requirements:
        drop_clause = False
        remaining = list(item.skills)
        for term in terms:
            if term == item.category:
                drop_clause = True
                break
            # Exact match on the canonical skill, so "sql" does not silently
            # match "postgresql".
            matched = [skill for skill in remaining if skill == term]
            if matched:
                for skill in matched:
                    remaining.remove(skill)
                    removed_skills.append(skill)
                continue
            if _mentions(item.text, term):
                drop_clause = True
                break
        if drop_clause:
            ignored.append(item.text)
            continue
        kept.append(item.model_copy(update={"skills": remaining}) if remaining != item.skills else item)

    return {
        "image_id": image.image_id,
        "role_title": image.role_title,
        # Slots are recomputed from what survived rather than subtracted from the
        # original values: subtraction would have to know which requirement
        # produced which slot, and getting that wrong silently keeps a filter the
        # recruiter just asked to remove.
        "slots": slots_from_requirements(kept),
        "applied": [item.text for item in kept],
        "ignored": ignored,
        "skills_removed": sorted(set(removed_skills)),
        "unenforceable": [item.text for item in kept if not item.enforceable],
        "policy": (
            "Requirements the index cannot enforce are listed as unenforceable and must "
            "never be described as active filters. Image-derived skills are preferences "
            "unless the description stated them as required."
        ),
    }
