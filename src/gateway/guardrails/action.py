"""Guardrail action execution — applies block, redact, and log decisions to messages."""

import re
from typing import Any

from shared.logging import get_logger

logger = get_logger()

# ---------------------------------------------------------------------------
# Action dispatchers
# ---------------------------------------------------------------------------


def apply_redact(text: str, matches: list[str]) -> str:
    """Replace every matched substring in *text* with ``[REDACTED]``.

    Matches are sorted by length descending to avoid partial-string replacement
    issues (e.g. a shorter match prefixing a longer one).
    """
    if not matches or not text:
        return text

    # Remove duplicates and sort longest-first to avoid substring shadowing
    deduped = sorted(set(matches), key=len, reverse=True)
    result = text
    for match in deduped:
        result = result.replace(match, "[REDACTED]")
    return result


def apply_redact_to_messages(
    messages: list[Any],  # list of NormalizedRequest.Message / dict
    matches: list[str],
) -> list[Any]:
    """Apply redaction to all messages in a request/response.

    For Pydantic Message objects, updates ``.content`` in place.
    For plain dicts, updates ``["content"]``.
    """
    for msg in messages:
        if hasattr(msg, "content"):
            msg.content = apply_redact(getattr(msg, "content", "") or "", matches)
        elif isinstance(msg, dict):
            msg["content"] = apply_redact(msg.get("content", "") or "", matches)
    return messages


def format_block_reason(rule_id: str, matches: list[str], confidence: float) -> str:
    """Format a human-readable block reason."""
    preview = ", ".join(matches[:3])
    if len(matches) > 3:
        preview += f" (+{len(matches) - 3} more)"
    return f"[{rule_id}] confidence={confidence:.2f} — matched: {preview}"
