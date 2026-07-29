"""Persist complexity snapshot results on the node.

Commits go through the node's process-wide SQLite write gate when available:
the handler connection is shared across threads and unserialized commits race
with ingest/derive writers ("database is locked").
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from topos.storage.db.write_gate import commit_connection


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_complexity_tables(conn: sqlite3.Connection) -> None:
    # Cheap existence probe: sqlite3.Connection is not weak-referenceable, so
    # this is the simplest way to avoid a DDL+commit per upsert call.
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='complexity_snapshots' LIMIT 1"
    ).fetchone()
    if row is not None:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS complexity_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            metric_set TEXT NOT NULL,
            week_start TEXT,
            metrics_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_complexity_snapshots_metric_week
        ON complexity_snapshots(metric_set, week_start)
        """
    )
    commit_connection(conn)


def upsert_snapshot(
    conn: sqlite3.Connection,
    *,
    snapshot_id: str,
    metric_set: str,
    metrics: Dict[str, Any],
    week_start: Optional[str] = None,
    commit: bool = True,
) -> None:
    """Upsert one snapshot row. Pass commit=False inside a batch and commit
    once at the end (engine writes ~16 rows per snapshot)."""
    ensure_complexity_tables(conn)
    now = _now_iso()
    conn.execute(
        """
        INSERT INTO complexity_snapshots (snapshot_id, metric_set, week_start, metrics_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(snapshot_id) DO UPDATE SET
            metrics_json=excluded.metrics_json,
            updated_at=excluded.updated_at
        """,
        (
            snapshot_id,
            metric_set,
            week_start,
            json.dumps(metrics),
            now,
            now,
        ),
    )
    if commit:
        commit_connection(conn)


def load_snapshots(
    conn: sqlite3.Connection,
    *,
    metric_set: Optional[str] = None,
    limit: int = 52,
) -> List[Dict[str, Any]]:
    ensure_complexity_tables(conn)
    if metric_set:
        rows = conn.execute(
            """
            SELECT snapshot_id, metric_set, week_start, metrics_json, created_at, updated_at
            FROM complexity_snapshots
            WHERE metric_set=?
            ORDER BY week_start DESC, updated_at DESC
            LIMIT ?
            """,
            (metric_set, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT snapshot_id, metric_set, week_start, metrics_json, created_at, updated_at
            FROM complexity_snapshots
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            metrics = json.loads(str(row["metrics_json"]))
        except json.JSONDecodeError:
            metrics = {}
        out.append(
            {
                "snapshot_id": row["snapshot_id"],
                "metric_set": row["metric_set"],
                "week_start": row["week_start"],
                "metrics": metrics,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    return out


def load_latest_summary(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    rows = load_snapshots(conn, metric_set="summary", limit=1)
    if not rows:
        return None
    return rows[0].get("metrics") if isinstance(rows[0].get("metrics"), dict) else None
