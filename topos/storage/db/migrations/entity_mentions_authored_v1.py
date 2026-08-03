"""Wave B6 / P3.1: ``entity_mentions.authored_by_owner`` mention-level attribution.

PLAN_PROVENANCE_SPLIT P3.1 — mention rows need a denormalized owner-authorship
bit so IMB / misattribution expansion can filter without a per-read join through
parent message tables (P3.3's record_id lookup covers the message path for
retrieval meanwhile; this column is the spine-side expansion).

``authored_by_owner`` is INTEGER NULL:
  1 — parent record is owner-authored (``record_role`` == authored)
  0 — parent record is not owner-authored (observed/addressed/ambient/…)
  NULL — parent unresolved (orphan record_id / unknown table)

Column add is always_run (wiki_entities_v1 uses CREATE TABLE IF NOT EXISTS).
Backfill is ledger-guarded and computes via ``record_role`` from parent row
fields — not the sparsely-populated ``actor_role`` column alone.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, Optional

MIGRATION_ID = "entity_mentions_authored_v1"

_BATCH = 2000

# Parent tables: (table, pk_column, role_field_columns for record_role).
_PARENT_SPECS = (
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
    (
        "journal_entries",
        "entry_id",
        (),  # authored by construction when the row exists
    ),
    (
        "activity_events",
        "event_id",
        (),  # ambient
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


def authored_flag_for_row(
    row: Optional[Dict[str, Any]],
    *,
    table: str,
) -> Optional[int]:
    """Map a parent row to ``authored_by_owner`` (1/0). None if row missing."""
    if row is None:
        return None
    table_n = (table or "").strip().lower()
    if table_n == "journal_entries":
        return 1
    if table_n in ("activity_events", "browser_visits", "browser_events"):
        return 0
    from topos.features.provenance.roles import ROLE_AUTHORED, record_role

    return 1 if record_role(row, table=table_n) == ROLE_AUTHORED else 0


def lookup_authored_by_owner(
    conn: sqlite3.Connection,
    record_id: str,
    *,
    canonical_table: Optional[str] = None,
) -> Optional[int]:
    """Resolve authorship for a mention's parent record. Pure lookup, no writes."""
    if not record_id:
        return None
    preferred = (canonical_table or "").strip().lower()
    specs = list(_PARENT_SPECS)
    if preferred:
        specs = [s for s in specs if s[0] == preferred] + [
            s for s in specs if s[0] != preferred
        ]
    for table, pk, role_cols in specs:
        if not _table_exists(conn, table):
            continue
        present = _columns(conn, table)
        if pk not in present:
            continue
        cols = [c for c in role_cols if c in present]
        select_cols = ", ".join([pk] + cols) if cols else pk
        row = conn.execute(
            f"SELECT {select_cols} FROM {table} WHERE {pk} = ? LIMIT 1",
            (record_id,),
        ).fetchone()
        if row is None:
            continue
        if not cols:
            return authored_flag_for_row({pk: row[0]}, table=table)
        payload = {col: row[i + 1] for i, col in enumerate(cols)}
        return authored_flag_for_row(payload, table=table)
    return None


def backfill_entity_mentions_authored(conn: sqlite3.Connection) -> dict[str, int]:
    """Stamp NULL ``authored_by_owner`` from parent records. Idempotent (NULL-only).

    Join-driven per parent table so orphan ``record_id``s never spin the batcher.
    """
    report = {"updated": 0, "still_null": 0, "authored": 0, "not_authored": 0}
    if not _table_exists(conn, "entity_mentions"):
        return report
    cols = _columns(conn, "entity_mentions")
    if "authored_by_owner" not in cols:
        return report

    for table, pk, role_cols in _PARENT_SPECS:
        if not _table_exists(conn, table):
            continue
        present = _columns(conn, table)
        if pk not in present:
            continue
        parent_cols = [c for c in role_cols if c in present]
        select_parent = ", ".join(f"p.{c}" for c in parent_cols)
        select_list = "m.mention_id" + (f", {select_parent}" if select_parent else "")
        while True:
            rows = conn.execute(
                f"""
                SELECT {select_list}
                FROM entity_mentions m
                JOIN {table} p ON p.{pk} = m.record_id
                WHERE m.authored_by_owner IS NULL
                LIMIT ?
                """,
                (_BATCH,),
            ).fetchall()
            if not rows:
                break
            updates = []
            for row in rows:
                mention_id = row[0]
                if parent_cols:
                    payload = {
                        col: row[i + 1] for i, col in enumerate(parent_cols)
                    }
                else:
                    payload = {pk: mention_id}
                flag = authored_flag_for_row(payload, table=table)
                if flag is None:
                    continue
                updates.append((flag, mention_id))
                if flag == 1:
                    report["authored"] += 1
                else:
                    report["not_authored"] += 1
            if not updates:
                break
            conn.executemany(
                """
                UPDATE entity_mentions
                SET authored_by_owner = ?
                WHERE mention_id = ? AND authored_by_owner IS NULL
                """,
                updates,
            )
            report["updated"] += len(updates)
            if len(rows) < _BATCH:
                break

    still = conn.execute(
        "SELECT COUNT(*) FROM entity_mentions WHERE authored_by_owner IS NULL"
    ).fetchone()
    report["still_null"] = int(still[0]) if still else 0
    return report


def apply_entity_mentions_authored_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    if _table_exists(conn, "entity_mentions"):
        cols = _columns(conn, "entity_mentions")
        if "authored_by_owner" not in cols:
            conn.execute(
                "ALTER TABLE entity_mentions ADD COLUMN authored_by_owner INTEGER"
            )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_entity_mentions_authored
            ON entity_mentions(authored_by_owner)
            WHERE authored_by_owner IS NOT NULL
            """
        )
    if not _migration_applied(conn, MIGRATION_ID):
        backfill_entity_mentions_authored(conn)
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
