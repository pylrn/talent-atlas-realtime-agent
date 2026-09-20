"""Shared text-matching helpers for ranking checks and explanations.

Two precision fixes live here:

- `phrase_in_text` matches on WORD BOUNDARIES, not substrings — so a role like
  "cto" matches "CTO" / "a CTO role" but NOT the name "Victor" (vi-CTO-r).
- `norm_skill` normalizes hyphens/underscores to spaces so "mechanical-engineering"
  and "mechanical engineering" compare equal.
"""

from __future__ import annotations

import re


def norm_skill(s: str) -> str:
    """Lowercase + collapse hyphens/underscores to spaces for skill comparison."""
    return s.strip().lower().replace("-", " ").replace("_", " ")


def phrase_in_text(phrase: str, text: str) -> bool:
    """True if `phrase` occurs in `text` on word boundaries (not as a substring).

    "cto" matches "CTO" and "a cto role" but NOT "Victor". Multi-word phrases
    ("mechanical engineering") are matched as a whole. `text` is assumed lowercased.
    """
    phrase = phrase.strip()
    if not phrase:
        return False
    return re.search(r"\b" + re.escape(phrase) + r"\b", text) is not None
