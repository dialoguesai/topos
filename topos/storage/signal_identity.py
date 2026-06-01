"""Signal identity storage: my_phone_number (and optional my_signal_id) per dataset.

Used to set sender_type (self vs contact) and owner when ingesting Signal messages.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("topos.storage.signal_identity")

TABLE = "signal_identity"


def ensure_table(conn) -> None:
    """Create signal_identity table if not exists."""
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            dataset_id TEXT NOT NULL PRIMARY KEY,
            my_phone_number TEXT,
            my_signal_id TEXT,
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()


def get_signal_identity(conn, dataset_id: str) -> Optional[dict]:
    """Return { my_phone_number, my_signal_id } for dataset_id, or None."""
    if not conn or not dataset_id:
        return None
    try:
        ensure_table(conn)
        row = conn.execute(
            f"SELECT my_phone_number, my_signal_id FROM {TABLE} WHERE dataset_id = ?",
            (dataset_id,),
        ).fetchone()
        if not row:
            return None
        return {"my_phone_number": row[0], "my_signal_id": row[1]}
    except Exception as e:
        logger.warning("get_signal_identity failed: %s", e)
        return None


def put_signal_identity(
    conn,
    dataset_id: str,
    *,
    my_phone_number: Optional[str] = None,
    my_signal_id: Optional[str] = None,
) -> None:
    """Set Signal identity for dataset_id. Pass None to leave a field unchanged."""
    if not conn or not dataset_id:
        return
    try:
        ensure_table(conn)
        existing = get_signal_identity(conn, dataset_id)
        phone = my_phone_number if my_phone_number is not None else (existing.get("my_phone_number") if existing else None)
        sid = my_signal_id if my_signal_id is not None else (existing.get("my_signal_id") if existing else None)
        conn.execute(
            f"""
            INSERT OR REPLACE INTO {TABLE} (dataset_id, my_phone_number, my_signal_id, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            """,
            (dataset_id, phone, sid),
        )
        conn.commit()
    except Exception as e:
        logger.warning("put_signal_identity failed: %s", e)
