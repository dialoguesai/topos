"""Which entity is the owner — one answer, arrived at the same way everywhere.

`is_self` is not unique. Measured live 2026-08-26 the flag sat on THREE entities:
`ent_e73ff33ae330422b` ("Owner", 178 pack facts, 1,203 edges) plus two rows created that
morning holding nothing. Eleven production call sites read it with an unordered
`fetchone()`/`LIMIT 1`, so which owner they got was rowid luck — and a VACUUM, a rewrite of
that row, or an ordinary merge could silently move the answer from the fact-bearing entity to
an empty shell. Nothing would have failed; the node would just have started answering
"nothing is known about you".

The selector is deterministic and prefers the self-entity that actually owns facts, with
`entity_id` as the tiebreak so the choice is stable across rewrites. It was written once for
the facts-direct query lane and is lifted here so the other ten sites cannot drift from it.

`owner_entity_ids` is the plural form, for the questions that genuinely have several answers —
ego removal has to drop EVERY self node from a graph, not the best one.
"""

from __future__ import annotations

import sqlite3
from typing import Optional


def owner_entity_id(conn: sqlite3.Connection) -> Optional[str]:
    """The owner, chosen deterministically. None when the node has no self entity."""
    try:
        row = conn.execute(
            """SELECT e.entity_id
                 FROM entities e
                WHERE e.is_self=1
             ORDER BY (SELECT COUNT(*) FROM signal_objects o
                        WHERE o.object_type='fact'
                          AND o.object_key LIKE 'fact:' || e.entity_id || ':%') DESC,
                      e.entity_id ASC
                LIMIT 1"""
        ).fetchone()
    except sqlite3.Error:
        # Fixtures and early-boot databases lack signal_objects; the ordering is a
        # preference, not a requirement, so fall back to the stable tiebreak alone.
        try:
            row = conn.execute(
                "SELECT entity_id FROM entities WHERE is_self=1 ORDER BY entity_id ASC LIMIT 1"
            ).fetchone()
        except sqlite3.Error:
            return None
    return str(row[0]) if row and row[0] else None


def owner_entity_ids(conn: sqlite3.Connection) -> set:
    """EVERY self entity.

    Plural for the questions that have several right answers: ego removal must drop all of
    them from a graph, and a projection guard must treat all of them as the owner. Using the
    singular there would leave the ego in the graph under its other identity — which looks
    exactly like the fix working.
    """
    try:
        return {str(r[0]) for r in
                conn.execute("SELECT entity_id FROM entities WHERE is_self=1").fetchall()
                if r and r[0]}
    except sqlite3.Error:
        return set()
