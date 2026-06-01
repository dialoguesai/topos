"""Normalize sender / identifier strings for matching contact_identifiers rows."""

from __future__ import annotations

from typing import Any


def normalize_contact_key(value: Any) -> str:
    """
    Match keys used when joining message sender_id to contact book identifiers.
    Aligned with historical logic in topos.core.handlers._normalize_contact_key.
    """
    s = str(value or "").strip()
    if not s:
        return ""
    low = s.lower()
    if low == "self":
        return "self"
    if "@" in low:
        return low
    digits = "".join(ch for ch in s if ch.isdigit())
    if digits:
        return f"+{digits}" if s.startswith("+") else digits
    return low
