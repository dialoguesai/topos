"""Add explicit owner memory storage under the backed-up migration ledger."""
import sqlite3

MIGRATION_ID = "home_chat_memories_v1"


def apply_home_chat_memories_v1_up(conn: sqlite3.Connection) -> None:
    # SQLite permits the references before functional home_chat_sessions is created.
    conn.execute("""CREATE TABLE IF NOT EXISTS home_chat_memories (
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
    conn.execute("INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)", (MIGRATION_ID,))
    conn.commit()
