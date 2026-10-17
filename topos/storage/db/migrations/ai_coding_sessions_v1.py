"""AI-coding-tool sessions (CONTRACTS.md §2.6): ``ai_chat_messages.share_excluded`` and ``ai_chat_message_facets``.

``share_excluded INTEGER NOT NULL DEFAULT 0`` is additive: every row already stored reads as
shareable, and only the AI-tool sync sets it (``ingestion/ai_tool_sync.py``), for assistant turns
after a session touched a Topos surface. Readers that must not carry such a row add
``share_excluded = 0`` through ``storage/share_excluded.py``.

``ai_chat_message_facets(message_id PRIMARY KEY, tool, project_key, updated_at)`` is owner-only
storage for the project facet (the repo folder's basename and nothing more). It is never joined
into a grantee path and never written into ``metadata_json``, which reaches grantees raw.

Always-run, like ``actor_role_v1``: the ai_chat tables are created lazily by the first import,
possibly after the migrations ran once, so the column add must re-run at every boot. Both DDL
steps are PRAGMA-guarded and cheap. Registering the step moves ``PRAGMA user_version`` past every
engine that predates it, as 75-82 did at their release cuts.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "ai_coding_sessions_v1"

MESSAGES_TABLE = "ai_chat_messages"
COLUMN = "share_excluded"
FACETS_TABLE = "ai_chat_message_facets"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return bool(row)


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


TRIGGER_ON_INSERT = f"{MESSAGES_TABLE}_{COLUMN}_on_insert"
TRIGGER_ON_METADATA = f"{MESSAGES_TABLE}_{COLUMN}_on_metadata"
#: The flag travels WITH the write (CONTRACTS §2.6): a reader marks a record ``_metadata.share_excluded``, the
#: mapper carries that into ``metadata_json``, and the two triggers set the column inside the same statement as
#: the INSERT (or a later rewrite of the metadata), so no window and no later UPDATE separates a stored tainted
#: row from its flag. The column only ever goes 0 → 1 here; nothing clears it. JSON1 is part of every SQLite the
#: node ships with (``json_extract`` is already read by signal_features).
_TRIGGER_WHEN = (
    "NEW.metadata_json IS NOT NULL AND json_valid(NEW.metadata_json) "
    "AND json_extract(NEW.metadata_json, '$.share_excluded') IN (1, 'true')"
)


def ensure_share_excluded_triggers(conn: sqlite3.Connection) -> bool:
    """Create the two triggers when the table and the column exist. True when they are in place."""
    if not _table_exists(conn, MESSAGES_TABLE) or COLUMN not in _columns(conn, MESSAGES_TABLE):
        return False
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {TRIGGER_ON_INSERT} AFTER INSERT ON {MESSAGES_TABLE}
        WHEN {_TRIGGER_WHEN}
        BEGIN
            UPDATE {MESSAGES_TABLE} SET {COLUMN} = 1 WHERE message_id = NEW.message_id AND {COLUMN} = 0;
        END
        """
    )
    conn.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {TRIGGER_ON_METADATA} AFTER UPDATE OF metadata_json ON {MESSAGES_TABLE}
        WHEN {_TRIGGER_WHEN}
        BEGIN
            UPDATE {MESSAGES_TABLE} SET {COLUMN} = 1 WHERE message_id = NEW.message_id AND {COLUMN} = 0;
        END
        """
    )
    return True


def ensure_share_excluded_column(conn: sqlite3.Connection) -> bool:
    """Add the column to ``ai_chat_messages`` if the table exists and lacks it, with its index and the two
    triggers that set it at write time. True when the column is there."""
    if not _table_exists(conn, MESSAGES_TABLE):
        return False
    if COLUMN not in _columns(conn, MESSAGES_TABLE):
        conn.execute(f"ALTER TABLE {MESSAGES_TABLE} ADD COLUMN {COLUMN} INTEGER NOT NULL DEFAULT 0")
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{MESSAGES_TABLE}_{COLUMN} ON {MESSAGES_TABLE}({COLUMN}) WHERE {COLUMN} = 1"
    )
    ensure_share_excluded_triggers(conn)
    return True


def ensure_facets_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {FACETS_TABLE} (
            message_id TEXT PRIMARY KEY,
            tool TEXT,
            project_key TEXT,
            updated_at TEXT
        )
        """
    )


def apply_ai_coding_sessions_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    ensure_share_excluded_column(conn)
    # The facets table only beside the ai_chat tables: a node that never made them (messenger only) keeps no
    # ai_chat% table at all (tests/core/test_routine_lane_row_tools.py); the sync runner creates it before its
    # first write (ai_tool_sync._ensure_storage), after the canonical tables exist.
    if _table_exists(conn, MESSAGES_TABLE):
        ensure_facets_table(conn)
    conn.execute("INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)", (MIGRATION_ID,))
    conn.commit()
