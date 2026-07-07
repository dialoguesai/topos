"""Backfill real signal dimensions onto existing derived rows.

Every signal_embeddings row written before 2026-07-06 carries
signal_dimension='memory' (the embeddings job defaulted it), and every
timeline row has an empty signal_dimension. Both columns drive faceted
clustering, dimension-filtered vector search, and brief scoping — all
silent no-ops over a single-valued column.

Maps record kinds to dimensions via the same table the embeddings job now
uses at write time (embed_context._DIMENSION_BY_RECORD_KIND). One-time,
gated by MIGRATION_ID; new writes are stamped at ingest.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

MIGRATION_ID = "signal_dimension_backfill_v1"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def backfill_signal_dimensions(conn: sqlite3.Connection) -> dict:
    from ....features.signal.embed_context import _DIMENSION_BY_RECORD_KIND

    counts = {"signal_embeddings": 0, "timeline": 0}
    if _table_exists(conn, "signal_embeddings"):
        for kind, dimension in _DIMENSION_BY_RECORD_KIND.items():
            if dimension == "memory":
                continue
            cursor = conn.execute(
                """
                UPDATE signal_embeddings SET signal_dimension = ?
                WHERE LOWER(COALESCE(record_type, '')) = ?
                  AND (signal_dimension IS NULL OR signal_dimension = ''
                       OR signal_dimension = 'memory')
                """,
                (dimension, kind),
            )
            counts["signal_embeddings"] += int(cursor.rowcount or 0)
    if _table_exists(conn, "timeline"):
        for kind, dimension in _DIMENSION_BY_RECORD_KIND.items():
            if dimension == "memory":
                continue
            cursor = conn.execute(
                """
                UPDATE timeline SET signal_dimension = ?
                WHERE (LOWER(COALESCE(canonical_table, '')) = ?
                       OR LOWER(COALESCE(record_type, '')) = ?)
                  AND (signal_dimension IS NULL OR signal_dimension = ''
                       OR signal_dimension = 'memory')
                """,
                (dimension, kind, kind),
            )
            counts["timeline"] += int(cursor.rowcount or 0)
    return counts


def apply_signal_dimension_backfill_v1_up(conn: sqlite3.Connection) -> None:
    counts = backfill_signal_dimensions(conn)
    if any(counts.values()):
        logger.info(
            "signal dimension backfill: %d embeddings, %d timeline rows",
            counts["signal_embeddings"],
            counts["timeline"],
        )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_signal_dimension_backfill_v1_down(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
