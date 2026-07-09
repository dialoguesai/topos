"""P4.1: actor_role as a real indexed column on canonical message tables.

PLAN_PROVENANCE_SPLIT P4 — the record role (authored > addressed >
participated > observed > ambient) becomes queryable storage instead of a
per-read computation: role-gated derivations and first-person retrieval can
filter on an indexed column, and the role each row held at backfill time is
auditable.

Adds ``actor_role TEXT NULL`` to ``conversation_messages`` and
``ai_chat_messages`` plus partial indexes, then backfills once (ledger-guarded)
via :func:`topos.features.provenance.roles.record_role` — THE single source of
truth for role; the migration deliberately does not replicate the rules in SQL
so the two can never drift.

Idempotent: column adds are PRAGMA-guarded and re-run cheaply after legacy
DDL (the canonical table creators use CREATE TABLE IF NOT EXISTS, so this must
re-run on every boot like the disclosure/period column migrations); the
backfill scan runs once, guarded by the migration ledger.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "actor_role_v1"

# Columns record_role reads per table (plus the primary key for the update).
_BACKFILL_SPEC = {
    "conversation_messages": (
        "message_id",
        ("sender_type", "sender_id", "is_from_self", "event_type", "message_type"),
    ),
    "ai_chat_messages": ("message_id", ("sender_type", "sender_id")),
}

_BATCH = 5000


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _columns(conn: sqlite3.Connection, table: str) -> set:
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


def _backfill_table(conn: sqlite3.Connection, table: str) -> int:
    from topos.features.provenance.roles import record_role

    pk, role_cols = _BACKFILL_SPEC[table]
    present = _columns(conn, table)
    cols = [c for c in role_cols if c in present]
    select_cols = ", ".join([pk] + cols)
    updated = 0
    while True:
        rows = conn.execute(
            f"SELECT {select_cols} FROM {table} WHERE actor_role IS NULL LIMIT {_BATCH}"
        ).fetchall()
        if not rows:
            break
        updates = []
        for row in rows:
            record = {col: row[i + 1] for i, col in enumerate(cols)}
            updates.append((record_role(record, table=table), row[0]))
        conn.executemany(
            f"UPDATE {table} SET actor_role = ? WHERE {pk} = ?", updates
        )
        updated += len(updates)
        if len(rows) < _BATCH:
            break
    return updated


def apply_actor_role_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    tables = [t for t in _BACKFILL_SPEC if _table_exists(conn, t)]
    for table in tables:
        if "actor_role" not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN actor_role TEXT")
        conn.execute(
            f"""CREATE INDEX IF NOT EXISTS idx_{table}_actor_role
                ON {table}(actor_role) WHERE actor_role IS NOT NULL"""
        )

    if not _migration_applied(conn, MIGRATION_ID):
        # One-time backfill through record_role (never replicated in SQL).
        # New rows are the producers' responsibility; NULL means "not yet
        # classified" and every reader treats it as the pre-P4.1 world.
        for table in tables:
            _backfill_table(conn, table)

    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
