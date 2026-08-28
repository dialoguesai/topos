"""Re-file entity facts by what they are about, not by which job wrote them.

The entities job stamped ``dimension='relationships'`` on every fact it wrote
while carrying the entity's own type in the same dict. ``get_by_dimension`` is a
live API filter, so the column is read at decision time and four-fifths of what
the relationships filter returned was not a relationship.

Measured on the owner's node 2026-08-27 over the 32,293 facts filed under
"relationships": 10,924 ORG, 3,932 PERSON, 2,704 DATE, 2,088 GPE. Re-deriving
from the stored ``entity_type`` moves 23,228 rows:

    relationships   32,293 ->  9,065     time        0 -> 4,610
    work             2,253 -> 14,835     places     13 -> 4,001
    interests        3,433 ->  5,388     resources   0 ->    93

Scoped by the presence of ``entity_type`` in the payload, which is what makes
this safe: it selects exactly the rows the entities job wrote and leaves the
``relationship_edges`` and dossier facts — which are genuinely about
relationships and carry no entity type — untouched.

Nothing is invented. The dimension is re-derived from a value already stored on
each row, so the migration is recomputable and the payload keeps the evidence.
Types the map deliberately does not cover (CARDINAL, ORDINAL, QUANTITY, PERCENT,
MISC, NORP, LAW) keep the dimension they have rather than being guessed at.
"""

from __future__ import annotations

import json
import logging
import sqlite3

logger = logging.getLogger(__name__)

MIGRATION_ID = "fact_dimension_by_entity_type_v1"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def restamp_fact_dimensions(conn: sqlite3.Connection, *, dry_run: bool = False) -> dict:
    """Re-derive ``signal_facts.dimension`` from each row's stored entity type."""
    from ....features.signal.dimension_registry import dimension_for_entity_type

    counts = {"scanned": 0, "changed": 0, "unmapped_kept": 0}
    if not _table_exists(conn, "signal_facts"):
        return counts

    updates = []
    for fact_id, dimension, payload in conn.execute(
        """
        SELECT fact_id, dimension, payload_json FROM signal_facts
        WHERE json_valid(payload_json)
          AND json_extract(payload_json, '$.entity_type') IS NOT NULL
        """
    ).fetchall():
        counts["scanned"] += 1
        try:
            entity_type = json.loads(payload).get("entity_type")
        except (ValueError, TypeError):
            continue
        # Falling back to the row's CURRENT dimension is what makes an unmapped
        # type a no-op rather than a guess.
        current = str(dimension or "")
        derived = dimension_for_entity_type(entity_type, fallback=current)
        if derived == current:
            if dimension_for_entity_type(entity_type, fallback="\0") == "\0":
                counts["unmapped_kept"] += 1
            continue
        counts["changed"] += 1
        updates.append((derived, fact_id))

    if dry_run or not updates:
        return counts

    conn.executemany("UPDATE signal_facts SET dimension=? WHERE fact_id=?", updates)
    return counts


def apply_fact_dimension_by_entity_type_v1_up(conn: sqlite3.Connection) -> None:
    counts = restamp_fact_dimensions(conn)
    if counts["changed"]:
        logger.info(
            "fact dimension restamp: %d of %d entity facts re-filed by type",
            counts["changed"], counts["scanned"],
        )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_fact_dimension_by_entity_type_v1_down(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
