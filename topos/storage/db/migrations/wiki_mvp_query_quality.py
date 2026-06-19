"""Query quality sprint: align derived message_* tables with enrichment writers."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "wiki_mvp_query_quality_derived_v1"

_DERIVED_EMOTIONS_DDL = """
CREATE TABLE IF NOT EXISTS message_emotions_derived (
    message_id TEXT NOT NULL,
    source_id TEXT,
    emotion_label TEXT,
    confidence REAL,
    model_name TEXT,
    all_emotions_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (message_id, model_name)
);
"""


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row[1]) for row in rows}
    except sqlite3.OperationalError:
        return set()


def apply_wiki_mvp_query_quality_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    if conn.execute(
        "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone():
        return

    entity_cols = _table_columns(conn, "message_entities")
    if entity_cols and "message_id" not in entity_cols and "record_id" in entity_cols:
        try:
            conn.execute("ALTER TABLE message_entities ADD COLUMN message_id TEXT")
            conn.execute(
                "UPDATE message_entities SET message_id=record_id WHERE message_id IS NULL"
            )
        except sqlite3.OperationalError:
            pass

    topic_cols = _table_columns(conn, "message_topics")
    if topic_cols and "message_id" not in topic_cols and "record_id" in topic_cols:
        try:
            conn.execute("ALTER TABLE message_topics ADD COLUMN message_id TEXT")
            conn.execute(
                "UPDATE message_topics SET message_id=record_id WHERE message_id IS NULL"
            )
        except sqlite3.OperationalError:
            pass

    emotion_cols = _table_columns(conn, "message_emotions")
    if emotion_cols and "message_id" not in emotion_cols:
        if "emotion_id" in emotion_cols and "record_id" in emotion_cols:
            conn.executescript(_DERIVED_EMOTIONS_DDL)
        else:
            try:
                conn.execute("ALTER TABLE message_emotions ADD COLUMN message_id TEXT")
                conn.execute(
                    """
                    UPDATE message_emotions
                    SET message_id=COALESCE(message_id, record_id)
                    WHERE message_id IS NULL
                    """
                )
            except sqlite3.OperationalError:
                pass

    conn.execute(
        "INSERT INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_wiki_mvp_query_quality_down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS message_emotions_derived")
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id=?", (MIGRATION_ID,))
    conn.commit()
