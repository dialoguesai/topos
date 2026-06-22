"""Gap tests for idempotent vector upsert (Phase A)."""

from __future__ import annotations

import sqlite3

import pytest

from topos.enrichment.job_writer import write_signal_records
from topos.features.signal.vector_codec import decode_f32
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import ensure_migrations_applied

pytestmark = pytest.mark.gap


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "vr.db"))
    ensure_migrations_applied(db)
    return db


def test_idempotent_upsert_same_record_model(conn: sqlite3.Connection) -> None:
    bundle = AdapterFactory.create("local_database", conn=conn)
    records = [
        {
            "message_id": "m1",
            "record_id": "m1",
            "source_id": "chatgpt_file_ingestion",
            "vector": [1.0, 0.0, 0.0],
            "dims": 3,
            "model": "test-model",
            "provider": "test",
            "signal_dimension": "memory",
            "chunk_index": 0,
        }
    ]
    write_signal_records("embeddings", records, adapters=bundle, conn=conn)
    write_signal_records("embeddings", records, adapters=bundle, conn=conn)
    count = conn.execute(
        "SELECT COUNT(*) FROM signal_embeddings WHERE record_id=? AND model=?",
        ("m1", "test-model"),
    ).fetchone()[0]
    assert count == 1


def test_f32_storage_format(conn: sqlite3.Connection) -> None:
    bundle = AdapterFactory.create("local_database", conn=conn)
    vector = [0.6, 0.8, 0.0]
    write_signal_records(
        "embeddings",
        [
            {
                "record_id": "m2",
                "source_id": "chatgpt_file_ingestion",
                "vector": vector,
                "dims": 3,
                "model": "test-model",
                "provider": "test",
            }
        ],
        adapters=bundle,
        conn=conn,
    )
    row = conn.execute(
        "SELECT vector_format, vector_blob FROM signal_embeddings WHERE record_id=?",
        ("m2",),
    ).fetchone()
    assert row[0] == "f32"
    decoded = decode_f32(row[1])
    assert len(decoded) == 3
