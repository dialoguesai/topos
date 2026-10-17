"""SQLite DDL for home chat sessions (functional feature storage, not canonical AIChat)."""

from __future__ import annotations

import logging
import sqlite3

from ..storage.db.write_gate import commit_connection, with_db_write

logger = logging.getLogger("topos.home_chat.schema")

#: What ``ensure_home_chat_schema`` puts in place, as ``sqlite_master`` names it, beside the one column it adds.
SCHEMA_OBJECTS = frozenset({
    ("table", "home_chat_sessions"), ("index", "idx_home_chat_sessions_user_engine_updated"),
    ("table", "home_chat_memories"), ("index", "idx_home_chat_memories_owner"),
})


def schema_in_place(conn: sqlite3.Connection) -> bool:
    """Whether everything the DDL below creates is already in this database, read from the database itself.

    A read takes no SQLite write lock, so this needs no gate. It is read on every call, never remembered: a
    database file that was replaced or made again, or a table or an index that was dropped, reads as "not in
    place" at once, and the gated DDL runs as it always did. An error reads as "not in place" too."""
    try:
        names = sorted(name for _kind, name in SCHEMA_OBJECTS)
        rows = conn.execute(f"SELECT type, name FROM sqlite_master WHERE name IN ({', '.join('?' * len(names))})",
                            names).fetchall()
        if not SCHEMA_OBJECTS <= {(str(row[0]), str(row[1])) for row in rows}:
            return False
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(home_chat_sessions)").fetchall()}
        return "participants_json" in columns
    except Exception:  # noqa: BLE001 -- unknown is "not in place": the gated path decides
        return False


def ensure_home_chat_schema(conn: sqlite3.Connection) -> None:
    """Create home chat session table and indices if missing."""
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        # NR183 R10: the store calls this on every request, its reads included, and it took the write gate each
        # time to run DDL that changed nothing: on the owner's node a share-index rebuild held the gate 62.9 s and
        # every home-chat read and save waited behind it. Only a database that lacks something takes the gate now.
        if schema_in_place(conn):
            return
        # DDL takes SQLite's write lock at execute time — gate it with the
        # commit (write_gate lock-order inversion); store calls this per request.
        with with_db_write():
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS home_chat_sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    engine_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT 'New chat',
                    trace_session_id TEXT NOT NULL DEFAULT '',
                    history_json TEXT NOT NULL DEFAULT '{}',
                    participants_json TEXT NOT NULL DEFAULT '[]',
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at_ms INTEGER NOT NULL DEFAULT 0,
                    updated_at_ms INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_home_chat_sessions_user_engine_updated
                ON home_chat_sessions (user_id, engine_id, updated_at_ms DESC)
                """
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS home_chat_memories (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    engine_id TEXT NOT NULL,
                    origin_session_id TEXT NOT NULL REFERENCES home_chat_sessions(id) ON DELETE CASCADE,
                    source_session_id TEXT NOT NULL REFERENCES home_chat_sessions(id) ON DELETE CASCADE,
                    source_message_id TEXT NOT NULL,
                    source_content TEXT NOT NULL,
                    text TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1,
                    deleted INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_home_chat_memories_owner ON home_chat_memories(user_id, engine_id, deleted)")
            _ensure_participants_column(conn)
            commit_connection(conn)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ensure_home_chat_schema failed: %s", exc)
        raise


def _ensure_participants_column(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(home_chat_sessions)")
    columns = {str(row[1]) for row in cur.fetchall()}
    if "participants_json" not in columns:
        conn.execute(
            "ALTER TABLE home_chat_sessions ADD COLUMN participants_json TEXT NOT NULL DEFAULT '[]'"
        )
