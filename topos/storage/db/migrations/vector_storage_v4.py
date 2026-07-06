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


def vec_table_uses_cosine(conn: sqlite3.Connection) -> bool:
    """True when the vec0 DDL declares cosine distance.

    Without it vec0 defaults to L2: ordering stays correct for normalized
    vectors, but `1 - distance` is then NOT a cosine similarity, and the
    service-level min-similarity threshold silently drops nearly every hit
    (caught by the retrieval eval the first time ANN actually ran in tests).
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (_VEC_TABLE,),
    ).fetchone()
    return bool(row and row[0] and "distance_metric=cosine" in str(row[0]))


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
            embedding float[{int(dims)}] distance_metric=cosine
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


def top_up_vec_rows(conn: sqlite3.Connection, dims: int) -> int:
    """Insert embeddings missing from the ANN table (cheap no-op when in sync)."""
    from ....features.signal.vector_codec import decode_vector, encode_f32

    counts = conn.execute(
        f"""
        SELECT
            (SELECT COUNT(*) FROM signal_embeddings WHERE vector_blob IS NOT NULL),
            (SELECT COUNT(*) FROM {_VEC_TABLE})
        """
    ).fetchone()
    if counts is None or int(counts[0] or 0) <= int(counts[1] or 0):
        return 0
    rows = conn.execute(
        f"""
        SELECT embedding_id, vector_blob, vector_format
        FROM signal_embeddings
        WHERE vector_blob IS NOT NULL
          AND embedding_id NOT IN (SELECT embedding_id FROM {_VEC_TABLE})
        """
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

    # Keep the ANN table aligned with the active embedding model. Runs on
    # every startup path (cheap count-compare no-op when in sync):
    #   * missing table (extension installed after embeddings were written,
    #     or a fresh install) -> create + full backfill
    #   * dims divergence -> rebuild at active dims
    #   * row gap (embeddings written while the extension was unavailable)
    #     -> top up only the missing rows
    if _sqlite_vec_available(conn) and "signal_embeddings" in _tables(conn):
        active_dims = _active_model_dims()
        declared = declared_vec_dims(conn)
        needs_rebuild = active_dims and (
            declared != active_dims or (declared and not vec_table_uses_cosine(conn))
        )
        if needs_rebuild:
            written = rebuild_vec_table(conn, active_dims)
            logger.info(
                "%s %s at %d dims, cosine (%d vectors)",
                "Created" if declared == 0 else "Rebuilt",
                _VEC_TABLE,
                active_dims,
                written,
            )
        elif active_dims and declared == active_dims:
            topped_up = top_up_vec_rows(conn, active_dims)
            if topped_up:
                logger.info("Topped up %s with %d missing vectors", _VEC_TABLE, topped_up)

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
