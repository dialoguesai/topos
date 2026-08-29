"""Resolve a raw messaging handle to the person entity behind it.

Every lane that says something about a PERSON has to cross the same seam:
`conversation_messages.sender_id` and the L1 analytics' `a_key`/`b_key` both hold
a RAW handle ("+15555550105", an email), while names live in `contacts` and the
graph keys on `entities`. `contact_identifiers` is the only bridge, and
`entities.contact_id` carries it the last hop (57/57 of the owner's comms
partners are linked on the live node).

This module used to also COMPUTE per-partner interaction statistics. It no longer
does: `analytics/messenger_directed.py` computes them for the analytical rail, and
two independent derivations of the same signal is how they drift apart. The
closeness lens reads `messenger_dyad_stats` and this module only does the bridge —
the 2026-08-25 decision was that the two views "share evidence but are not merged",
and sharing means one of them stops recomputing.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict


def normalise_handle(raw: Any) -> str:
    """One key shape for both sides of the identifier join.

    Phones arrive as "+1 (555) 555-0105" in the address book and "+15555550105"
    on a message or a dyad key; digits-only/last-ten collapses both. Anything
    containing a letter (email, @handle) folds to lowercase instead.
    """
    s = str(raw or "").strip()
    if not s:
        return ""
    digits = "".join(ch for ch in s if ch.isdigit())
    if digits and not any(ch.isalpha() for ch in s):
        return digits[-10:] if len(digits) >= 10 else digits
    return s.lower()


def looks_like_a_person_name(name: str) -> bool:
    """A handle is an identifier, not a person.

    Unnamed contacts carry their handle as `entities.canonical_name`, and the
    owner's own entity is literally named "self". A fact reading
    `{person: "+15555550106", tier: "inner_circle"}` names nobody, and one reading
    `{person: "self"}` makes the owner their own inner circle — both were in the
    top three of the live edge ranking before these guards existed.
    """
    s = str(name or "").strip()
    if len(s) < 2:
        return False
    low = s.lower()
    if low.startswith(("self", "me:", "owner", "unknown", "user:", "system", "sys:", "rec:")):
        return False
    if "@" in s:
        # An email has letters and passes the alpha test, so it reached the writer
        # and was guard-rejected only after taking a tier slot and shifting
        # everyone else's percentile.
        return False
    if ":" in s:
        # `scheme:value` is an identifier shape, never a name. "unknown:0" has
        # letters and was written as a person.
        return False
    return any(ch.isalpha() for ch in s)


def handle_to_entity(conn: sqlite3.Connection) -> Dict[str, str]:
    """normalised handle -> entity_id, through contacts."""
    out: Dict[str, str] = {}
    try:
        rows = conn.execute(
            "SELECT e.entity_id, ci.identifier FROM entities e"
            " JOIN contact_identifiers ci ON ci.contact_id = e.contact_id"
            " WHERE e.contact_id IS NOT NULL AND e.contact_id != ''"
            "   AND ci.identifier IS NOT NULL AND ci.identifier != ''"
        ).fetchall()
    except sqlite3.Error:
        return out
    for entity_id, identifier in rows:
        key = normalise_handle(identifier)
        if key:
            out.setdefault(key, str(entity_id))
    return out
