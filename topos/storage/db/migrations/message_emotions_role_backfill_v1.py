"""Wave B5: stamp ``role`` on legacy ``message_emotions`` rows.

Write-path (emo_27) already stamps role via ``record_role``; wellbeing filters
keep ``NULL OR authored/addressed``. Legacy NULLs therefore leak observed-role
emotions into owner wellbeing.

Live lesson (same as B6 / P3.1): the materialized ``actor_role`` column is
sparse on nodes where data landed after the one-shot P4.1 backfill. Joining
``actor_role`` alone no-ops (~0 updates / thousands still NULL). This backfill
computes role through :func:`record_role` from parent row fields — THE single
source of truth — and optionally refreshes ``actor_role`` when it is NULL.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Tuple

MIGRATION_ID = "message_emotions_role_backfill_v1"

_BATCH = 2000

# Parent tables: (table, pk, columns record_role needs).
_PARENT_SPECS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    (
        "conversation_messages",
        "message_id",
        ("sender_type", "sender_id", "is_from_self", "event_type", "message_type"),
    ),
    (
        "ai_chat_messages",
        "message_id",
        ("sender_type", "sender_id"),
    ),
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _migration_applied(conn: sqlite3.Connection, migration_id: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?",
            (migration_id,),
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False


def backfill_message_emotions_role(conn: sqlite3.Connection) -> dict[str, int]:
    """Stamp NULL emotion roles via ``record_role`` on parent rows.

    Idempotent (NULL-only). Returns ``updated``, ``still_null``, and
    ``actor_role_stamped`` (side-fill of sparse parent ``actor_role``).
    No-ops cleanly on wiki-shaped ``message_emotions`` (no ``message_id``).
    """
    from topos.features.provenance.roles import record_role

    report = {"updated": 0, "still_null": 0, "actor_role_stamped": 0}
    if not _table_exists(conn, "message_emotions"):
        return report
    cols = _columns(conn, "message_emotions")
    if "role" not in cols or "message_id" not in cols:
        return report

    for table, pk, role_cols in _PARENT_SPECS:
        if not _table_exists(conn, table):
            continue
        present = _columns(conn, table)
        if pk not in present:
            continue
        fields = [c for c in role_cols if c in present]
        has_actor_role = "actor_role" in present
        select_cols = ", ".join([f"p.{pk}"] + [f"p.{c}" for c in fields])
        if has_actor_role:
            select_cols += ", p.actor_role"

        while True:
            rows = conn.execute(
                f"""
                SELECT {select_cols}
                FROM message_emotions e
                JOIN {table} p ON p.{pk} = e.message_id
                WHERE e.role IS NULL
                LIMIT ?
                """,
                (_BATCH,),
            ).fetchall()
            if not rows:
                break

            emotion_updates: List[Tuple[str, str]] = []
            actor_updates: List[Tuple[str, str]] = []
            for row in rows:
                mid = row[0]
                record: Dict[str, Any] = {
                    col: row[i + 1] for i, col in enumerate(fields)
                }
                role = record_role(record, table=table)
                emotion_updates.append((role, mid))
                if has_actor_role:
                    existing = row[1 + len(fields)]
                    if existing is None:
                        actor_updates.append((role, mid))

            conn.executemany(
                "UPDATE message_emotions SET role = ? WHERE message_id = ? AND role IS NULL",
                emotion_updates,
            )
            report["updated"] += len(emotion_updates)
            if actor_updates:
                conn.executemany(
                    f"UPDATE {table} SET actor_role = ? WHERE {pk} = ? AND actor_role IS NULL",
                    actor_updates,
                )
                report["actor_role_stamped"] += len(actor_updates)
            if len(rows) < _BATCH:
                break

    still = conn.execute(
        "SELECT COUNT(*) FROM message_emotions WHERE role IS NULL"
    ).fetchone()
    report["still_null"] = int(still[0]) if still else 0
    return report


def apply_message_emotions_role_backfill_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    if not _migration_applied(conn, MIGRATION_ID):
        backfill_message_emotions_role(conn)
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
