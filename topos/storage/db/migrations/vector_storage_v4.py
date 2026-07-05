"""Vector storage v4: dims-flexible ANN table, cluster_id column, entity join table.

- The vec0 virtual table historically hardcoded float[384]; v4 rebuilds it to the
  active embedding model's dims when they diverge (P1 embedding upgrade path).
- signal_embeddings.cluster_id lets vector hits be filtered by topic cluster.
- embedding_entities joins vectors to resolved entities (P3 entity spine).
"""

from __future__ import annotations

import logging
import re
import sqlite3

logger = logging.getLogger(__name__)

MIGRATION_ID = "vector_storage_v4"
_VEC_TABLE = "signal_embeddings_vec"


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _add_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if table in _tables(conn) and column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }


def declared_vec_dims(conn: sqlite3.Connection) -> int:
    """Dims declared in the vec0 DDL; 0 when the table doesn't exist."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (_VEC_TABLE,),
    ).fetchone()
    if not row or not row[0]:
        return 0
    match = re.search(r"float\[(\d+)\]", str(row[0]))
    return int(match.group(1)) if match else 0


def _active_model_dims() -> int:
    try:
        from ....engine.backends.huggingface import (
            active_embedding_model,
            embedding_model_profile,
        )

        return int(embedding_model_profile(active_embedding_model()).get("dims") or 0)
    except Exception:
        return 0


def _sqlite_vec_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT vec_version()")
        return True
    except sqlite3.Error:
        return False


def rebuild_vec_table(conn: sqlite3.Connection, dims: int) -> int:
    """Drop and repopulate the ANN table at the given dims from signal_embeddings."""
    from ....features.signal.vector_codec import decode_vector, encode_f32

    conn.execute(f"DROP TABLE IF EXISTS {_VEC_TABLE}")
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE {_VEC_TABLE} USING vec0(
            embedding_id TEXT PRIMARY KEY,
            embedding float[{int(dims)}]
        )
        """
    )
    rows = conn.execute(
        "SELECT embedding_id, vector_blob, vector_format FROM signal_embeddings"
        " WHERE vector_blob IS NOT NULL"
    ).fetchall()
    written = 0
    for embedding_id, blob, vector_format in rows:
        try:
            vector = decode_vector(blob, vector_format or "json")
        except Exception:
            continue
        if len(vector) != dims:
            continue
        conn.execute(
            f"INSERT OR REPLACE INTO {_VEC_TABLE}(embedding_id, embedding) VALUES (?, ?)",
            (embedding_id, encode_f32(vector)),
        )
        written += 1
    return written


def apply_vector_storage_v4_up(conn: sqlite3.Connection) -> None:
    tables = _tables(conn)
    if "signal_embeddings" in tables:
        _add_column(conn, "signal_embeddings", "cluster_id", "TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_embeddings_cluster"
            " ON signal_embeddings(cluster_id) WHERE cluster_id IS NOT NULL"
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS embedding_entities (
            embedding_id TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            PRIMARY KEY (embedding_id, entity_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_embedding_entities_entity ON embedding_entities(entity_id)"
    )

    # Rebuild the ANN table if the active embedding model's dims diverge from
    # the declared column. Runs on every startup path (cheap no-op when equal).
    if _sqlite_vec_available(conn):
        active_dims = _active_model_dims()
        declared = declared_vec_dims(conn)
        if active_dims and declared and declared != active_dims:
            written = rebuild_vec_table(conn, active_dims)
            logger.info(
                "Rebuilt %s at %d dims (%d vectors)", _VEC_TABLE, active_dims, written
            )

    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_vector_storage_v4_down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS embedding_entities")
    conn.execute("DROP INDEX IF EXISTS idx_signal_embeddings_cluster")
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
