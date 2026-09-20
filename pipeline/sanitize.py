"""Prompt-injection sanitisation.

Wraps user-supplied text in hard delimiters and strips characters
that could confuse the LLM into treating user content as instructions.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_MAX_INPUT_CHARS = 8_000
_CONTROL_RE      = re.compile(r"[\x00-\x1f\x7f]")
_BRACE_RE        = re.compile(r"(?<![\"'])[{}](?![\"'])")   # bare { } outside quotes


def sanitize_input(text: str) -> str:
    """Return sanitised, delimiter-wrapped text safe for LLM prompt injection.

    Steps:
      1. Truncate to _MAX_INPUT_CHARS
      2. Strip C0/C1 control characters (null bytes, bell, etc.)
      3. Replace bare { } that could break JSON schema instructions
      4. Wrap in <<INPUT_START>> / <<INPUT_END>> delimiters
    """
    if not text:
        return "<<INPUT_START>>\n<<INPUT_END>>"

    if len(text) > _MAX_INPUT_CHARS:
        text = text[:_MAX_INPUT_CHARS]
        logger.warning("Input truncated to %d chars", _MAX_INPUT_CHARS)

    text = _CONTROL_RE.sub(" ", text)
    text = _BRACE_RE.sub(lambda m: "｛" if m.group() == "{" else "｝", text)

    return f"<<INPUT_START>>\n{text}\n<<INPUT_END>>"


def unwrap_input(sanitized: str) -> str:
    """Strip delimiters — useful for logging the clean text."""
    return (
        sanitized
        .removeprefix("<<INPUT_START>>\n")
        .removesuffix("\n<<INPUT_END>>")
    )
