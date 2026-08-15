"""Re-stamp commit-derived journal rows from wellbeing onto work.

``signal_dimension`` was mapped from record kind alone, and the GitHub
connector writes one journal row per authored COMMIT — so the whole commit
stream was stamped ``wellbeing``. On one live node that was 123 embeddings,
and with them 19 of 163 topic clusters named "Wellbeing Tracker (…)" over
terms like "merge branch", "gitignore", "build". The labeler was not at
fault: asked to "name the state, rhythm or condition" for a cluster of
commits, the dimension's own noun is the only answer left.

``embed_context`` now decides those rows by origin at write time
(``_DIMENSION_BY_RECORD_KIND_AND_SOURCE``); this moves the ones already on
disk. Unlike ``signal_dimension_backfill_v1``, which only filled rows still
sitting at the ``memory`` default, this one RE-STAMPS a wrong value, so it
matches on the old value explicitly rather than on emptiness.

Labels do not move on their own afterwards: the facet a record belongs to
changes, so the clusters have to be recomputed (not merely relabeled) for
the split to reach a surface.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

MIGRATION_ID = "journal_origin_dimension_v1"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(r[1] == column for r in conn.execute(f"PRAGMA table_info({table})"))


def restamp_journal_origin_dimensions(conn: sqlite3.Connection) -> dict:
    """Move (journal kind, source) pairs the kind-only map got wrong.

    Driven by the same table the write path uses, so the two cannot drift.
    Only rows still carrying the kind-only answer are touched — a dimension
    someone set deliberately to anything else is left alone.
    """
    from ....features.signal.embed_context import (
        _DIMENSION_BY_RECORD_KIND,
        _DIMENSION_BY_RECORD_KIND_AND_SOURCE,
    )

    counts = {"signal_embeddings": 0, "timeline": 0}
    for (kind, source), dimension in _DIMENSION_BY_RECORD_KIND_AND_SOURCE.items():
        stale = _DIMENSION_BY_RECORD_KIND.get(kind)
        if not stale or stale == dimension:
            continue
        if _table_exists(conn, "signal_embeddings"):
            cursor = conn.execute(
                """
                UPDATE signal_embeddings SET signal_dimension = ?
                 WHERE LOWER(COALESCE(record_type, '')) = ?
                   AND LOWER(COALESCE(source_id, '')) = ?
                   AND LOWER(COALESCE(signal_dimension, '')) = ?
                """,
                (dimension, kind, source, stale),
            )
            counts["signal_embeddings"] += int(cursor.rowcount or 0)
        if _table_exists(conn, "timeline") and _has_column(conn, "timeline", "source_id"):
            cursor = conn.execute(
                """
                UPDATE timeline SET signal_dimension = ?
                 WHERE (LOWER(COALESCE(canonical_table, '')) = ?
                        OR LOWER(COALESCE(record_type, '')) = ?)
                   AND LOWER(COALESCE(source_id, '')) = ?
                   AND LOWER(COALESCE(signal_dimension, '')) = ?
                """,
                (dimension, kind, kind, source, stale),
            )
            counts["timeline"] += int(cursor.rowcount or 0)
    return counts


def apply_journal_origin_dimension_v1_up(conn: sqlite3.Connection) -> None:
    counts = restamp_journal_origin_dimensions(conn)
    if any(counts.values()):
        logger.info(
            "journal origin dimension: re-stamped %d embeddings, %d timeline rows",
            counts["signal_embeddings"],
            counts["timeline"],
        )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_journal_origin_dimension_v1_down(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
