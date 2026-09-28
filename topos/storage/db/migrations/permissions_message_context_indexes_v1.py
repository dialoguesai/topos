"""Bounded neighbor lookup indexes for current message classifications.

These indexes preserve context_for's exact source/conversation/dataset and
(event_at,message_id) ordering. They contain no message text. SQLite maintains
them at ingest/update/delete, replacing per-request source scans and sorts.
Missing legacy tables/columns are skipped and revisited by always_run.

Registering this stamps schema 79. Deploy at a coordinated release cut with a
rollback runtime that also knows this migration; older runtimes reject the stamp.
"""
from __future__ import annotations

import sqlite3

MIGRATION_ID = "permissions_message_context_indexes_v1"
INDEXES = (
    ("conversation_messages", "idx_conversation_messages_permission_context",
     ("source_id", "conversation_id", "dataset_id", "event_at", "message_id")),
    ("ai_chat_messages", "idx_ai_chat_messages_permission_context",
     ("source_id", "conversation_id", "event_at", "message_id")),
)


def apply_permissions_message_context_indexes_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS wiki_schema_migrations "
                 "(migration_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))")
    for table, name, columns in INDEXES:
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if set(columns) <= present:
            conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({','.join(columns)})")
    conn.execute("INSERT OR IGNORE INTO wiki_schema_migrations(migration_id) VALUES (?)", (MIGRATION_ID,))
    conn.commit()
