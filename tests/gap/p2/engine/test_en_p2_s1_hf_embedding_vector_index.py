"""
Gap: Embeddings — stub → VectorIndex metadata + vector count > 0
Sprint: EN-P2-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import sqlite3

import pytest

from topos.enrichment.job_writer import write_signal_records
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import ensure_migrations_applied

pytestmark = pytest.mark.gap


def test_embedding_write_populates_vector_index(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "vec.db"))
    ensure_migrations_applied(conn)
    bundle = AdapterFactory.create("local_database", conn=conn)
    records = [
        {
            "message_id": "m1",
            "record_id": "m1",
            "source_id": "chatgpt",
            "vector": [0.1, 0.2, 0.3],
            "dims": 3,
            "model": "all-MiniLM-L6-v2",
            "provider": "huggingface",
            "signal_dimension": "memory",
        }
    ]
    count = write_signal_records(
        "embeddings",
        records,
        adapters=bundle,
        provenance={"provider": "huggingface", "model": "all-MiniLM-L6-v2"},
        conn=conn,
    )
    assert count >= 1
    page = bundle.vector.list_metadata(source_id="chatgpt")
    assert page.total >= 1
    assert "vector" not in page.items[0]
