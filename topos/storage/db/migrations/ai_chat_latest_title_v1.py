"""1.5.10: an AI chat's latest title, kept beside the title its first record set.

`ai_chat_conversations.title` is written once, by the record that creates the chat, and every review of the chat's
messages pins it: it is a column of the parent row the message's revision folds in (`evidence._load`), and of the
conversation context the Off-limits boundary hashes (`entity_boundary._context`). A title written later would move
both, withhold every message of the chat until its next review pass, and stale every search index holding one until
it is rebuilt. ChatGPT names a chat after its first reply, so most chats the browser extension streams arrive
untitled and are named on a later batch.

`latest_title` takes those later titles. It is in `evidence.REVIEW_SURFACE_EXCLUSIONS["ai_chat_conversations"]`,
which the context revision also leaves out (`entity_boundary.context_bookkeeping`), so writing it moves no review,
no context and no index. A share that opted into chat titles releases it before `title`. The Off-limits veto still
reads it: the veto reads every column, so a title naming an Off-limits entry withholds the message.

One nullable column, PRAGMA-guarded. No row is read or changed: the upsert fills it from the next batch that brings
a title. `always_run` like 75 to 81: legacy DDL (`CanonicalTablesManager`) may create the table after this ran.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "ai_chat_latest_title_v1"

TABLE = "ai_chat_conversations"
COLUMN = "latest_title"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def apply_ai_chat_latest_title_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    if _table_exists(conn, TABLE):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE})").fetchall()}
        if COLUMN not in columns:
            conn.execute(f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} TEXT")
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
