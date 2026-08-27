"""Which dataset holds the owner's messaging, when the caller cannot say.

`/v1/ingestion/datasets` returns ZERO rows on a node whose messages arrived by sync rather
than upload — measured on the live node 2026-08-27. Every messaging screen resolves its
dataset from that list, so a database holding 7,668 messages, 180 dyads and 4,048 entities
rendered as an empty Social page and an empty Luck page at the same time. The engine is one
GROUP BY away from the answer.

Lives on its own so the luck read and the messenger-analytics handlers share ONE rule; two
copies would drift, and the two screens would then disagree about which dataset the node is.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Tuple


def has_messaging_substrate(conn: Any, dataset_id: str) -> bool:
    """Does THIS dataset carry the messages the messaging screens read?

    Cheap existence probes, not counts: the only job is to tell "nothing happened" apart
    from "this dataset cannot answer".
    """
    if not str(dataset_id or "").strip():
        return False
    for sql in (
        "SELECT 1 FROM messenger_dyad_stats WHERE dataset_id=? LIMIT 1",
        "SELECT 1 FROM conversation_messages WHERE dataset_id=? AND is_from_self=1 LIMIT 1",
    ):
        try:
            if conn.execute(sql, (dataset_id,)).fetchone():
                return True
        except sqlite3.Error:
            continue
    return False


def resolve_primary_dataset(conn: Any) -> str:
    """The dataset with the most conversation messages, or empty if there are none."""
    try:
        row = conn.execute(
            "SELECT dataset_id, COUNT(*) n FROM conversation_messages"
            " WHERE dataset_id IS NOT NULL GROUP BY dataset_id ORDER BY n DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return ""
    return str(row[0]) if row and row[0] else ""


def resolve_messaging_dataset(conn: Any, requested: str) -> Tuple[str, bool]:
    """(dataset_to_use, engine_resolved_it).

    Falls back when the caller names nothing, or names a dataset with no messages — the
    second case matters because answering it literally reports real work with "who has heard
    is unknown", which is true of the id and useless to the person.

    Callers MUST surface `engine_resolved_it`; a silent substitution would show one dataset's
    data under another's name.
    """
    requested = str(requested or "").strip()
    if requested and has_messaging_substrate(conn, requested):
        return requested, False
    primary = resolve_primary_dataset(conn)
    if primary and primary != requested:
        return primary, True
    return requested, False
