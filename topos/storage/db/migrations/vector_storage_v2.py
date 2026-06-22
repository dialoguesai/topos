"""Vector storage v2: sqlite-vec ANN mirror table (local)."""

from __future__ import annotations

import logging
import sqlite3

from ....features.signal.vector_codec import decode_vector, encode_f32

logger = logging.getLogger(__name__)

MIGRATION_ID = "vector_storage_v2"
_VEC_TABLE = "signal_embeddings_vec"


def _sqlite_vec_available(conn: sqlite3.Connection) -> bool:
    try:
        conn.enable_load_extension(True)
        try:
            import sqlite_vec  # type: ignore

            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
            return True
        except Exception:
            conn.enable_load_extension(False)
            return False
    except Exception:
        return False


def apply_vector_storage_v2_up(conn: sqlite3.Connection) -> None:
    if "signal_embeddings" not in {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }:
        conn.execute(
            "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
            (MIGRATION_ID,),
        )
        conn.commit()
        return

    if not _sqlite_vec_available(conn):
        logger.debug("sqlite-vec unavailable; skipping %s virtual table", _VEC_TABLE)
        conn.execute(
            "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
            (MIGRATION_ID,),
        )
        conn.commit()
        return

    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {_VEC_TABLE} USING vec0(
            embedding_id TEXT PRIMARY KEY,
            embedding float[384]
        )
        """
    )

    existing = {
        row[0]
        for row in conn.execute(f"SELECT embedding_id FROM {_VEC_TABLE}").fetchall()
    }
    rows = conn.execute(
        """
        SELECT embedding_id, vector_blob, vector_format, dims
        FROM signal_embeddings
        WHERE vector_blob IS NOT NULL
        """
    ).fetchall()
    for embedding_id, blob, vector_format, dims in rows:
        if embedding_id in existing:
            continue
        try:
            vector = decode_vector(blob, vector_format or "json")
        except Exception:
            continue
        if dims and len(vector) != int(dims):
            continue
        if len(vector) != 384:
            continue
        conn.execute(
            f"INSERT OR REPLACE INTO {_VEC_TABLE}(embedding_id, embedding) VALUES (?, ?)",
            (embedding_id, encode_f32(vector)),
        )

    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_vector_storage_v2_down(conn: sqlite3.Connection) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {_VEC_TABLE}")
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
