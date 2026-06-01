"""Per-source settings: enabled, last_sync_at, last_error (for local_sync sources)."""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("topos.storage.source_settings")

TABLE = "user_ingestion_sources"


def ensure_table(conn) -> None:
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            dataset_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            last_sync_at TEXT,
            last_error TEXT,
            updated_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (dataset_id, source_id)
        )
    """)
    conn.commit()


def get_source_settings(conn, dataset_id: str, source_id: str) -> Optional[dict]:
    """Return { enabled, last_sync_at, last_error } or None (then defaults: enabled true, no last_*)."""
    if not conn or not dataset_id or not source_id:
        return None
    try:
        ensure_table(conn)
        row = conn.execute(
            f"SELECT enabled, last_sync_at, last_error FROM {TABLE} WHERE dataset_id = ? AND source_id = ?",
            (dataset_id, source_id),
        ).fetchone()
        if not row:
            return {"enabled": True, "last_sync_at": None, "last_error": None}
        return {"enabled": bool(row[0]), "last_sync_at": row[1], "last_error": row[2]}
    except Exception as e:
        logger.warning("get_source_settings failed: %s", e)
        return {"enabled": True, "last_sync_at": None, "last_error": None}


def put_source_settings(
    conn,
    dataset_id: str,
    source_id: str,
    *,
    enabled: Optional[bool] = None,
) -> None:
    """Update enabled; leave last_sync_at/last_error unchanged."""
    if not conn or not dataset_id or not source_id or enabled is None:
        return
    try:
        ensure_table(conn)
        cur = conn.execute(
            f"SELECT 1 FROM {TABLE} WHERE dataset_id = ? AND source_id = ?",
            (dataset_id, source_id),
        ).fetchone()
        if cur:
            conn.execute(
                f"UPDATE {TABLE} SET enabled = ?, updated_at = datetime('now') WHERE dataset_id = ? AND source_id = ?",
                (1 if enabled else 0, dataset_id, source_id),
            )
        else:
            conn.execute(
                f"INSERT INTO {TABLE} (dataset_id, source_id, enabled, updated_at) VALUES (?, ?, ?, datetime('now'))",
                (dataset_id, source_id, 1 if enabled else 0),
            )
        conn.commit()
    except Exception as e:
        logger.warning("put_source_settings failed: %s", e)


def update_sync_result(
    conn,
    dataset_id: str,
    source_id: str,
    *,
    success: bool,
    last_sync_at: Optional[str] = None,
    last_error: Optional[str] = None,
) -> None:
    """After sync: set last_sync_at (and clear last_error) on success, or set last_error on failure."""
    if not conn or not dataset_id or not source_id:
        return
    try:
        ensure_table(conn)
        if success:
            conn.execute(
                f"""
                INSERT INTO {TABLE} (dataset_id, source_id, enabled, last_sync_at, last_error, updated_at)
                VALUES (?, ?, 1, ?, NULL, datetime('now'))
                ON CONFLICT(dataset_id, source_id) DO UPDATE SET
                    last_sync_at = ?,
                    last_error = NULL,
                    updated_at = datetime('now')
                """,
                (dataset_id, source_id, last_sync_at or "", last_sync_at or ""),
            )
        else:
            conn.execute(
                f"""
                INSERT INTO {TABLE} (dataset_id, source_id, enabled, last_sync_at, last_error, updated_at)
                VALUES (?, ?, 1, NULL, ?, datetime('now'))
                ON CONFLICT(dataset_id, source_id) DO UPDATE SET
                    last_error = ?,
                    updated_at = datetime('now')
                """,
                (dataset_id, source_id, last_error or "", last_error or ""),
            )
        conn.commit()
    except Exception as e:
        logger.warning("update_sync_result failed: %s", e)
