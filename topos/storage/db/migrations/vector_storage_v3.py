"""Vector storage v3: search_text + FTS5 + topic cluster centroid vectors."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "vector_storage_v3"
_FTS_TABLE = "signal_embeddings_fts"


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def apply_vector_storage_v3_up(conn: sqlite3.Connection) -> None:
    if "signal_embeddings" in {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }:
        if "search_text" not in _column_names(conn, "signal_embeddings"):
            conn.execute("ALTER TABLE signal_embeddings ADD COLUMN search_text TEXT")
        conn.execute(
            """
            UPDATE signal_embeddings
            SET search_text = COALESCE(search_text, text_preview)
            WHERE search_text IS NULL AND text_preview IS NOT NULL
            """
        )

        conn.execute(
            f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} USING fts5(
                search_text,
                content='signal_embeddings',
                content_rowid='rowid',
                tokenize='porter unicode61'
            )
            """
        )
        conn.executescript(
            f"""
            CREATE TRIGGER IF NOT EXISTS signal_embeddings_ai AFTER INSERT ON signal_embeddings BEGIN
              INSERT INTO {_FTS_TABLE}(rowid, search_text) VALUES (new.rowid, new.search_text);
            END;
            CREATE TRIGGER IF NOT EXISTS signal_embeddings_ad AFTER DELETE ON signal_embeddings BEGIN
              INSERT INTO {_FTS_TABLE}({_FTS_TABLE}, rowid, search_text) VALUES('delete', old.rowid, old.search_text);
            END;
            CREATE TRIGGER IF NOT EXISTS signal_embeddings_au AFTER UPDATE ON signal_embeddings BEGIN
              INSERT INTO {_FTS_TABLE}({_FTS_TABLE}, rowid, search_text) VALUES('delete', old.rowid, old.search_text);
              INSERT INTO {_FTS_TABLE}(rowid, search_text) VALUES (new.rowid, new.search_text);
            END;
            """
        )
        conn.execute(
            f"""
            INSERT INTO {_FTS_TABLE}(rowid, search_text)
            SELECT rowid, search_text FROM signal_embeddings
            WHERE search_text IS NOT NULL
              AND rowid NOT IN (SELECT rowid FROM {_FTS_TABLE})
            """
        )

    if "topic_clusters" in {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }:
        if "centroid_vector" not in _column_names(conn, "topic_clusters"):
            conn.execute("ALTER TABLE topic_clusters ADD COLUMN centroid_vector BLOB")

    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_vector_storage_v3_down(conn: sqlite3.Connection) -> None:
    conn.executescript(
        f"""
        DROP TRIGGER IF EXISTS signal_embeddings_ai;
        DROP TRIGGER IF EXISTS signal_embeddings_ad;
        DROP TRIGGER IF EXISTS signal_embeddings_au;
        DROP TABLE IF EXISTS {_FTS_TABLE};
        """
    )
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
