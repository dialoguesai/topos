"""Vector storage v1: f32 format columns, idempotent upsert key, filter indexes."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "vector_storage_v1"


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def apply_vector_storage_v1_up(conn: sqlite3.Connection) -> None:
    if "signal_embeddings" not in {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }:
        conn.execute(
            "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
            (MIGRATION_ID,),
        )
        conn.commit()
        return

    _add_column(conn, "signal_embeddings", "vector_format", "TEXT NOT NULL DEFAULT 'json'")
    _add_column(conn, "signal_embeddings", "content_hash", "TEXT")
    _add_column(conn, "signal_embeddings", "chunk_index", "INTEGER NOT NULL DEFAULT 0")
    _add_column(conn, "signal_embeddings", "event_at", "TEXT")
    _add_column(conn, "signal_embeddings", "conversation_id", "TEXT")
    _add_column(conn, "signal_embeddings", "record_type", "TEXT")

    conn.execute(
        """
        UPDATE signal_embeddings
        SET vector_format = COALESCE(vector_format, 'json'),
            chunk_index = COALESCE(chunk_index, 0)
        WHERE vector_format IS NULL OR chunk_index IS NULL
        """
    )

    # Dedupe: keep newest created_at per (record_id, model, chunk_index).
    conn.execute(
        """
        DELETE FROM signal_embeddings
        WHERE embedding_id IN (
            SELECT embedding_id FROM (
                SELECT embedding_id,
                       ROW_NUMBER() OVER (
                           PARTITION BY record_id, model, COALESCE(chunk_index, 0)
                           ORDER BY created_at DESC, embedding_id DESC
                       ) AS rn
                FROM signal_embeddings
                WHERE record_id IS NOT NULL AND model IS NOT NULL
            )
            WHERE rn > 1
        )
        """
    )

    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_embeddings_record_model_chunk
        ON signal_embeddings(record_id, model, chunk_index)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_signal_embeddings_ann_filter
        ON signal_embeddings(source_id, signal_dimension, model, event_at DESC)
        WHERE vector_blob IS NOT NULL
        """
    )

    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_vector_storage_v1_down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_signal_embeddings_ann_filter")
    conn.execute("DROP INDEX IF EXISTS idx_signal_embeddings_record_model_chunk")
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
