"""
Stage 9 column renames: apply standardized names to existing DBs.
SQLite 3.25.1+ RENAME COLUMN. Idempotent: skips if new name already exists or old missing.
Source: docs/SCHEMA_CONVENTIONS.md §7.0.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, List, Optional, Tuple

logger = logging.getLogger("topos.storage.db.migrations.stage9")

# (table, old_column, new_column) in dependency order
RENAME_TARGETS: List[Tuple[str, str, str]] = [
    ("ai_chat_conversations", "source", "source_id"),
    ("conversation_messages", "ts", "event_at"),
    ("conversation_messages", "from_self", "is_from_self"),
    ("ai_chat_messages", "ts", "event_at"),
    ("ai_chat_messages", "seq", "sequence"),
    ("message_emotions", "model", "model_name"),
    ("message_emotions", "all_emotions", "all_emotions_json"),
    ("browser_url_classification", "source_table", "enriched_from_table"),
]


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    rows = cursor.fetchall()
    # PRAGMA table_info returns (cid, name, type, notnull, dflt_value, pk)
    names = [row[1] for row in rows]
    return column in names


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    return cursor.fetchone() is not None


def apply_stage9_renames(conn: sqlite3.Connection) -> dict[str, Any]:
    """
    Apply Stage 9 column renames. Idempotent.
    Returns dict with applied=[(table, old, new), ...], skipped=[...], errors=[...].
    """
    applied: List[Tuple[str, str, str]] = []
    skipped: List[Tuple[str, str, str]] = []
    errors: List[str] = []

    for table, old_col, new_col in RENAME_TARGETS:
        if not _table_exists(conn, table):
            skipped.append((table, old_col, new_col))
            continue
        if not _column_exists(conn, table, old_col):
            skipped.append((table, old_col, new_col))
            continue
        if _column_exists(conn, table, new_col):
            skipped.append((table, old_col, new_col))
            continue
        try:
            conn.execute(f'ALTER TABLE "{table}" RENAME COLUMN "{old_col}" TO "{new_col}"')
            conn.commit()
            applied.append((table, old_col, new_col))
            logger.info("Stage9 migration: %s.%s -> %s", table, old_col, new_col)
        except Exception as e:
            conn.rollback()
            errors.append(f"{table}.{old_col}->{new_col}: {e}")
            logger.warning("Stage9 migration failed: %s.%s -> %s: %s", table, old_col, new_col, e)

    return {"applied": applied, "skipped": skipped, "errors": errors}


def run_stage9_migrations(conn: sqlite3.Connection) -> dict[str, Any]:
    """Run Stage 9 migrations (currently only column renames)."""
    return apply_stage9_renames(conn)
