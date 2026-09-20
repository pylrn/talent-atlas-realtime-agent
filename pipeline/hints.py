"""Helpers for assembling personalization-hint context for the planner.

Two responsibilities:
  - build_hints_block: format the ABOUT-THIS-USER block injected into the
    planner system prompt. Empty string when disabled or no hints.
  - hints_cache_tag:   produce a cache-key suffix so users with different
    hints don't share cached plans. Empty when disabled or no hints — so
    the no-personalization majority of traffic keeps 100% cache hit rate.
"""

from __future__ import annotations

import hashlib
import json
from typing import Iterable


_BLOCK_HEADER = "=== ABOUT THIS USER (soft hints, NOT requirements) ==="
_BLOCK_RULES = """\
RULES for these hints:
1. They may only influence should.skills, should.themes, should.locations,
   should.roles, lexical_terms, semantic_query, or hyde_profile.
2. They MUST NEVER be added to must.*, must_not.*, or any hard-filter field.
   A hint is a soft bias, not a requirement.
3. If the current query contradicts a hint, the current query wins —
   ignore the hint for this search.
4. Do not re-offer a hint that is already in this list."""


def build_hints_block(hints: Iterable[dict] | None, *, enabled: bool) -> str:
    """Return the prompt prefix block, or '' when there's nothing to inject."""
    if not enabled or not hints:
        return ""
    texts = [str(h.get("text", "")).strip() for h in hints if h.get("text")]
    texts = [t for t in texts if t]
    if not texts:
        return ""
    bullets = "\n".join(f"- {t}" for t in texts)
    return f"{_BLOCK_HEADER}\n{bullets}\n\n{_BLOCK_RULES}\n\n"


def hints_cache_tag(hints: Iterable[dict] | None, *, enabled: bool) -> str:
    """Return '|h:<12hex>' when hints affect the prompt, else ''."""
    if not enabled or not hints:
        return ""
    texts = sorted(str(h.get("text", "")).strip() for h in hints if h.get("text"))
    texts = [t for t in texts if t]
    if not texts:
        return ""
    payload = json.dumps(texts, sort_keys=True)
    digest  = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"|h:{digest}"
