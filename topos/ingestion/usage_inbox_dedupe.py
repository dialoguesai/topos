"""Local dedupe store for Usage Inbox write_id redeliveries."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from ..core.state import get_db_connection

logger = logging.getLogger("topos.ingestion.usage_inbox_dedupe")

DEDUPE_TABLE = "usage_inbox_delivered_writes"
_RETENTION_DAYS = 30


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {DEDUPE_TABLE} (
            write_id TEXT PRIMARY KEY,
            records_processed INTEGER NOT NULL,
            records_total INTEGER NOT NULL,
            delivered_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{DEDUPE_TABLE}_delivered_at
        ON {DEDUPE_TABLE}(delivered_at)
        """
    )


def is_derivation_complete(write_id: str) -> bool:
    wid = str(write_id or "").strip()
    if not wid:
        return False
    conn = get_db_connection()
    if not conn:
        return False
    try:
        from ..pipeline.job_store import is_derivation_complete as _is_complete

        return _is_complete(conn, wid)
    except Exception as exc:
        logger.debug("derivation completion lookup failed: %s", exc)
        return False


def ensure_derivation_enqueued(
    write_id: str,
    *,
    source_id: str,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    """Re-enqueue derivation when ingest was delivered but derivation did not finish."""
    wid = str(write_id or "").strip()
    if not wid or is_derivation_complete(wid):
        return
    conn = get_db_connection()
    if not conn:
        return
    try:
        from ..pipeline.job_store import enqueue_job

        job_payload = dict(payload or {})
        job_payload.setdefault("source_id", source_id)
        job_payload.setdefault("write_id", wid)
        job_payload.setdefault("recover", True)
        enqueue_job(
            conn,
            kind="inbox_deferred_enrichment",
            payload=job_payload,
            source_id=source_id,
            write_id=wid,
            idempotency_key=f"inbox_derivation:{wid}",
        )
    except Exception as exc:
        logger.debug("ensure_derivation_enqueued failed: %s", exc)


def _purge_old(conn: sqlite3.Connection) -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=_RETENTION_DAYS)).isoformat()
    conn.execute(f"DELETE FROM {DEDUPE_TABLE} WHERE delivered_at < ?", (cutoff,))


def get_prior_delivery(write_id: str) -> Optional[Dict[str, int]]:
    wid = str(write_id or "").strip()
    if not wid:
        return None
    conn = get_db_connection()
    if not conn:
        return None
    try:
        _ensure_schema(conn)
        row = conn.execute(
            f"SELECT records_processed, records_total FROM {DEDUPE_TABLE} WHERE write_id = ?",
            (wid,),
        ).fetchone()
        if not row:
            return None
        return {"records_processed": int(row[0]), "records_total": int(row[1])}
    except Exception as exc:
        logger.debug("usage_inbox dedupe lookup failed: %s", exc)
        return None


def record_delivery(write_id: str, *, records_processed: int, records_total: int) -> None:
    wid = str(write_id or "").strip()
    if not wid:
        return
    conn = get_db_connection()
    if not conn:
        return
    try:
        _ensure_schema(conn)
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            f"""
            INSERT INTO {DEDUPE_TABLE} (write_id, records_processed, records_total, delivered_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(write_id) DO UPDATE SET
                records_processed = excluded.records_processed,
                records_total = excluded.records_total,
                delivered_at = excluded.delivered_at
            """,
            (wid, int(records_processed), int(records_total), now),
        )
        _purge_old(conn)
        conn.commit()
    except Exception as exc:
        logger.debug("usage_inbox dedupe record failed: %s", exc)
