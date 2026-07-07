"""Connection tuning: WAL, sqlite-vec loading, ANN table creation/top-up.

Regression suite for the silent-degradation trio found in the 2026-07-06
storage audit: journal_mode=delete on the live node, no sqlite-vec extension
loaded anywhere, and vector_storage_v4 only rebuilding the ANN table on dims
divergence (never creating it when missing).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.signal.vector_codec import encode_f32
from topos.storage.db.connection_tuning import (
    load_sqlite_vec,
    runtime_status,
    tune_connection,
)
from topos.storage.db.migrations import ensure_migrations_applied
from topos.storage.db.migrations.vector_storage_v4 import (
    apply_vector_storage_v4_up,
    declared_vec_dims,
    top_up_vec_rows,
)

DIMS = 384


def _unit_vector(hot: int) -> list[float]:
    vec = [0.0] * DIMS
    vec[hot % DIMS] = 1.0
    return vec


def _seed_embedding(conn: sqlite3.Connection, embedding_id: str, hot: int) -> None:
    conn.execute(
        """
        INSERT INTO signal_embeddings (
            embedding_id, record_id, source_id, signal_dimension, model,
            provider, dims, text_preview, provenance_json, vector_blob,
            vector_format
        ) VALUES (?, ?, ?, 'memory', 'sentence-transformers/all-MiniLM-L6-v2',
                  'hf', ?, ?, ?, ?, 'f32')
        """,
        (
            embedding_id,
            f"rec_{embedding_id}",
            "chatgpt_file_ingestion",
            DIMS,
            f"preview {embedding_id}",
            json.dumps({"embedding_id": embedding_id, "record_id": f"rec_{embedding_id}"}),
            encode_f32(_unit_vector(hot)),
        ),
    )


@pytest.fixture()
def file_conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "tuning.db"))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def test_tune_connection_sets_wal_on_file_db(file_conn):
    status = tune_connection(file_conn)
    assert status["journal_mode"] == "wal"
    assert status["busy_timeout_ms"] == 5000


def test_tune_connection_loads_sqlite_vec(file_conn):
    status = tune_connection(file_conn)
    assert status["sqlite_vec"] is True
    version = file_conn.execute("SELECT vec_version()").fetchone()[0]
    assert str(version).startswith("v")


def test_tune_connection_memory_db_skips_wal():
    conn = sqlite3.connect(":memory:")
    status = tune_connection(conn)
    # :memory: databases cannot use WAL; must not error, must still load vec.
    assert status["journal_mode"] == "memory"
    assert status["sqlite_vec"] is True
    conn.close()


def test_load_sqlite_vec_idempotent(file_conn):
    assert load_sqlite_vec(file_conn) is True
    assert load_sqlite_vec(file_conn) is True


def test_v4_creates_ann_table_when_missing(file_conn):
    tune_connection(file_conn)
    ensure_migrations_applied(file_conn)
    # Simulate the live-node history: embeddings written while ANN was absent.
    file_conn.execute("DROP TABLE IF EXISTS signal_embeddings_vec")
    for i in range(5):
        _seed_embedding(file_conn, f"emb_{i}", hot=i)
    file_conn.commit()
    assert declared_vec_dims(file_conn) == 0

    apply_vector_storage_v4_up(file_conn)

    assert declared_vec_dims(file_conn) == DIMS
    rows = file_conn.execute("SELECT COUNT(*) FROM signal_embeddings_vec").fetchone()[0]
    assert rows == 5


def test_v4_tops_up_missing_rows(file_conn):
    tune_connection(file_conn)
    ensure_migrations_applied(file_conn)
    for i in range(3):
        _seed_embedding(file_conn, f"emb_{i}", hot=i)
    file_conn.commit()
    apply_vector_storage_v4_up(file_conn)
    # New embedding written while the extension was unavailable on some
    # other connection: present in signal_embeddings, absent from ANN.
    _seed_embedding(file_conn, "emb_late", hot=7)
    file_conn.commit()

    topped = top_up_vec_rows(file_conn, DIMS)

    assert topped == 1
    rows = file_conn.execute("SELECT COUNT(*) FROM signal_embeddings_vec").fetchone()[0]
    assert rows == 4
    # In-sync call is a no-op.
    assert top_up_vec_rows(file_conn, DIMS) == 0


def test_search_similar_uses_ann_and_falls_back(file_conn, monkeypatch):
    from topos.storage.adapters.sqlite import vector_search

    tune_connection(file_conn)
    ensure_migrations_applied(file_conn)
    for i in range(10):
        _seed_embedding(file_conn, f"emb_{i}", hot=i)
    file_conn.commit()
    apply_vector_storage_v4_up(file_conn)

    query = _unit_vector(3)
    results, _total = vector_search.search_similar(file_conn, query, limit=3)
    assert vector_search.last_search_backend() == "sqlite_vec"
    assert results
    assert results[0]["embedding_id"] == "emb_3"
    assert results[0]["similarity"] == pytest.approx(1.0, abs=1e-4)

    # Same query without the ANN table degrades to brute force — and the
    # degradation must be recorded, not silent.
    file_conn.execute("DROP TABLE signal_embeddings_vec")
    file_conn.commit()
    results_bf, _ = vector_search.search_similar(file_conn, query, limit=3)
    assert vector_search.last_search_backend() == "brute_force"
    assert results_bf[0]["embedding_id"] == "emb_3"

    # Similarity VALUES (not just ordering) must match brute-force cosine:
    # the vec0 table must be declared distance_metric=cosine, else 1-distance
    # is an L2 quantity and the service min-similarity filter drops real hits
    # (regression caught by the retrieval eval).
    by_id_bf = {r["embedding_id"]: r["similarity"] for r in results_bf}
    for r in results:
        assert r["similarity"] == pytest.approx(by_id_bf[r["embedding_id"]], abs=1e-3)


def test_runtime_status_reports_counts(file_conn):
    tune_connection(file_conn)
    ensure_migrations_applied(file_conn)
    for i in range(4):
        _seed_embedding(file_conn, f"emb_{i}", hot=i)
    file_conn.commit()
    apply_vector_storage_v4_up(file_conn)

    status = runtime_status(file_conn)
    assert status["journal_mode"] == "wal"
    assert status["sqlite_vec"] is True
    assert status["embeddings"] == 4
    assert status["ann_rows"] == 4
