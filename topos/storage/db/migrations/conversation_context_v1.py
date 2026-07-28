"""Conversation context tag: work/personal as an owner-set column on conversations.

Second labeling axis alongside posture (PLAN_PROVENANCE_SPLIT §3.1): posture
says *whose words* the content is; context_tag says *which life domain* the
thread belongs to. Owner-set per conversation (never inferred here), values
``work`` | ``personal``; NULL = unset. Consumers refine heuristics with it —
first consumer is the relationships extractor's edge tier (work → professional,
personal → personal, unset → today's professional default).

Idempotent: the column add is PRAGMA-guarded and re-runs cheaply after legacy
DDL (conversations uses CREATE TABLE IF NOT EXISTS), so this is registered in
BOTH apply_all_migrations and ensure_migrations_applied and does not gate on
the migration ledger — same pattern as the disclosure/period column migrations.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "conversation_context_v1"

CONTEXT_TAG_VALUES = frozenset({"work", "personal"})


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(
        row[1] == column for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    )


def apply_conversation_context_v1_up(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "conversations"):
        return
    if not _has_column(conn, "conversations", "context_tag"):
        conn.execute("ALTER TABLE conversations ADD COLUMN context_tag TEXT")
    # Provenance of the tag: 'owner' (set/cleared via API, always wins) or
    # 'auto:<model>' (local-LLM classification, only ever fills untagged rows).
    if not _has_column(conn, "conversations", "context_tag_source"):
        conn.execute("ALTER TABLE conversations ADD COLUMN context_tag_source TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversations_context_tag "
        "ON conversations(context_tag) WHERE context_tag IS NOT NULL"
    )
    conn.commit()
