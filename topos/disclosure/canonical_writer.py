"""Persist ingest-time disclosure columns on canonical tables."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, Optional

from .field_registry import CANONICAL_ID_COLUMN

logger = logging.getLogger("topos.disclosure.canonical_writer")

DISCLOSURE_MODEL_SETTING = "openai/privacy-filter"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


def upsert_disclosure_fields(
    conn: sqlite3.Connection,
    table: str,
    record_id: str,
    patches: Dict[str, Any],
    *,
    model_id: Optional[str] = None,
) -> bool:
    if not patches or not record_id:
        return False
    if not _table_exists(conn, table):
        logger.warning("disclosure write skipped: table %s missing", table)
        return False
    id_col = CANONICAL_ID_COLUMN.get(table)
    if not id_col:
        logger.warning("disclosure write skipped: unknown id column for %s", table)
        return False

    safe_patches: Dict[str, Any] = {}
    for key, value in patches.items():
        if not _column_exists(conn, table, key):
            continue
        safe_patches[key] = value
    if model_id and _column_exists(conn, table, "content_disclosure_model"):
        safe_patches["content_disclosure_model"] = model_id
    if not safe_patches:
        return False

    sets = ", ".join(f"{col}=?" for col in safe_patches)
    params = list(safe_patches.values()) + [record_id]
    conn.execute(f"UPDATE {table} SET {sets} WHERE {id_col}=?", params)
    return True


def upsert_nsfw_fields(
    conn: sqlite3.Connection,
    table: str,
    record_id: str,
    *,
    is_nsfw: bool,
    score: float,
    model_id: Optional[str] = None,
) -> bool:
    patches: Dict[str, Any] = {
        "content_nsfw": 1 if is_nsfw else 0,
        "content_nsfw_score": float(score),
    }
    if model_id:
        patches["content_nsfw_model"] = model_id
    return upsert_disclosure_fields(conn, table, record_id, patches, model_id=None)
