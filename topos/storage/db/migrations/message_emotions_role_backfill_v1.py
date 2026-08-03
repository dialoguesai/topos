"""Wave B5: stamp ``role`` on legacy ``message_emotions`` rows.

Write-path (emo_27) already stamps role; wellbeing filters keep
``NULL OR authored/addressed``. Legacy NULLs therefore leak observed-role
emotions into owner wellbeing. This one-shot backfill copies the
materialized ``actor_role`` from the parent message tables (P4.1), which
is already ledger-guarded and indexed.

Posture-cap drift vs emo_27 write path (ambient → observed) is a documented
fast-follow; joining ``actor_role`` is the Wave-B5-sized path.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "message_emotions_role_backfill_v1"

_BATCH = 5000

# Parent message tables that carry actor_role (migration order 36).
_PARENT_TABLES = ("conversation_messages", "ai_chat_messages")


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
    """Stamp NULL roles from parent ``actor_role``. Idempotent (NULL-only).

    Returns counts: ``updated``, ``still_null`` (orphans / unmatched FKs).
    No-ops cleanly on wiki-shaped ``message_emotions`` (no ``message_id``).
    """
    report = {"updated": 0, "still_null": 0}
    if not _table_exists(conn, "message_emotions"):
        return report
    cols = _columns(conn, "message_emotions")
    if "role" not in cols or "message_id" not in cols:
        return report

    for parent in _PARENT_TABLES:
        if not _table_exists(conn, parent):
            continue
        parent_cols = _columns(conn, parent)
        if "actor_role" not in parent_cols or "message_id" not in parent_cols:
            continue
        while True:
            rows = conn.execute(
                f"""
                SELECT e.message_id, p.actor_role
                FROM message_emotions e
                JOIN {parent} p ON p.message_id = e.message_id
                WHERE e.role IS NULL
                  AND p.actor_role IS NOT NULL
                LIMIT ?
                """,
                (_BATCH,),
            ).fetchall()
            if not rows:
                break
            conn.executemany(
                "UPDATE message_emotions SET role = ? WHERE message_id = ? AND role IS NULL",
                [(role, mid) for mid, role in rows],
            )
            report["updated"] += len(rows)
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
