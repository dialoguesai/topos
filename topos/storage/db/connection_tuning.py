"""Per-connection SQLite tuning: WAL, busy timeout, sqlite-vec extension.

Every long-lived engine connection should pass through ``tune_connection``.
Without it the node runs journal_mode=delete (writers block every reader
during enrichment) and the sqlite-vec extension is never loaded, so ANN
vector search silently degrades to a full-table brute-force scan.

All failures are non-fatal: a connection that can't be tuned still works,
it's just slower — but the degradation is *recorded* (``runtime_status``)
instead of silent.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_BUSY_TIMEOUT_MS = 30000

# Status of the most recent tune_connection call, for banner/doctor/telemetry.
_last_tuning_status: Dict[str, Any] = {}


def _is_file_backed(conn: sqlite3.Connection) -> bool:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return False
    return bool(row and str(row[2] or "").strip())


def load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Load the sqlite-vec extension into this connection (idempotent)."""
    try:
        conn.execute("SELECT vec_version()")
        return True  # already loaded
    except sqlite3.Error:
        pass
    try:
        import sqlite_vec  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)
        conn.execute("SELECT vec_version()")
        return True
    except (sqlite3.Error, AttributeError, OSError) as exc:
        logger.debug("sqlite-vec extension load failed: %s", exc)
        return False


def tune_connection(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Apply WAL + busy timeout + sqlite-vec to a connection. Never raises."""
    status: Dict[str, Any] = {
        "journal_mode": None,
        "sqlite_vec": False,
        "busy_timeout_ms": None,
    }
    file_backed = _is_file_backed(conn)
    try:
        if file_backed:
            row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
            status["journal_mode"] = str(row[0]) if row else None
            if status["journal_mode"] != "wal":
                logger.warning(
                    "journal_mode=WAL not applied (got %s); concurrent reads will block on writes",
                    status["journal_mode"],
                )
            # NORMAL is the standard WAL pairing: fsync on checkpoint, not per-commit.
            conn.execute("PRAGMA synchronous=NORMAL")
        else:
            row = conn.execute("PRAGMA journal_mode").fetchone()
            status["journal_mode"] = str(row[0]) if row else None
    except sqlite3.Error as exc:
        logger.debug("journal tuning skipped: %s", exc)
    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        status["busy_timeout_ms"] = _BUSY_TIMEOUT_MS
    except sqlite3.Error:
        pass
    status["sqlite_vec"] = load_sqlite_vec(conn)
    if not status["sqlite_vec"]:
        logger.info("sqlite-vec unavailable; vector search will use brute-force scan")
    global _last_tuning_status
    _last_tuning_status = dict(status)
    return status


def runtime_status(conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """Vector/journal runtime health, for doctor checks and startup logging."""
    status: Dict[str, Any] = dict(_last_tuning_status)
    if conn is None:
        return status
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
        status["journal_mode"] = str(row[0]) if row else None
    except sqlite3.Error:
        pass
    try:
        conn.execute("SELECT vec_version()")
        status["sqlite_vec"] = True
    except sqlite3.Error:
        status["sqlite_vec"] = False
    try:
        status["embeddings"] = int(
            conn.execute(
                "SELECT COUNT(*) FROM signal_embeddings WHERE vector_blob IS NOT NULL"
            ).fetchone()[0]
        )
    except sqlite3.Error:
        status["embeddings"] = None
    try:
        status["ann_rows"] = int(
            conn.execute("SELECT COUNT(*) FROM signal_embeddings_vec").fetchone()[0]
        )
    except sqlite3.Error:
        status["ann_rows"] = None
    return status
