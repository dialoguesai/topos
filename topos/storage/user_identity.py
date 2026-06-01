"""Dataset-scoped owner identity storage for self-name resolution."""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("topos.storage.user_identity")

TABLE = "user_identity"


def ensure_table(conn) -> None:
    """Create ``user_identity`` table if it does not exist."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            dataset_id TEXT NOT NULL PRIMARY KEY,
            display_name TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        )
        """
    )
    conn.commit()


def get_user_identity(conn, dataset_id: str) -> Optional[dict]:
    """Return ``{display_name}`` for a dataset, or ``None`` when absent."""
    if not conn or not dataset_id:
        return None
    try:
        ensure_table(conn)
        row = conn.execute(
            f"SELECT display_name FROM {TABLE} WHERE dataset_id = ?",
            (dataset_id,),
        ).fetchone()
        if not row:
            return None
        return {"display_name": row[0]}
    except Exception as e:
        logger.warning("get_user_identity failed: %s", e)
        return None


def put_user_identity(
    conn,
    dataset_id: str,
    *,
    display_name: Optional[str] = None,
) -> None:
    """Set dataset-scoped owner identity. ``None`` leaves the field unchanged."""
    if not conn or not dataset_id:
        return
    try:
        ensure_table(conn)
        existing = get_user_identity(conn, dataset_id)
        next_display_name = (
            display_name if display_name is not None else (existing.get("display_name") if existing else None)
        )
        conn.execute(
            f"""
            INSERT OR REPLACE INTO {TABLE} (dataset_id, display_name, updated_at)
            VALUES (?, ?, datetime('now'))
            """,
            (dataset_id, next_display_name),
        )
        conn.commit()
    except Exception as e:
        logger.warning("put_user_identity failed: %s", e)
