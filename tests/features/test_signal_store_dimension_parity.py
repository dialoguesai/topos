"""The dimension used to select a feature must be the dimension returned.

The historical restamp changed the indexed column without rewriting evidence
payloads. These tests keep that migration's result authoritative on reads and
ensure a subsequent full feature upsert cannot split column and payload again.
All fixtures are synthetic, in-memory databases.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.storage.adapters.sqlite.stores import SQLiteSignalFeatureStore
from topos.storage.db.migrations.fact_dimension_by_entity_type_v1 import (
    restamp_fact_dimensions,
)


@pytest.fixture
def feature_store():
    conn = sqlite3.connect(":memory:")
    for kind in ("fact", "score"):
        conn.execute(
            f"""CREATE TABLE signal_{kind}s (
                {kind}_id TEXT PRIMARY KEY,
                dimension TEXT,
                source_id TEXT,
                record_id TEXT,
                payload_json TEXT,
                created_at TEXT DEFAULT '2026-09-14T00:00:00Z'
            )"""
        )
    yield conn, SQLiteSignalFeatureStore(conn)
    conn.close()


@pytest.mark.parametrize("kind", ["fact", "score"])
def test_upsert_moves_feature_to_new_dimension(feature_store, kind):
    conn, store = feature_store
    put = getattr(store, f"put_{kind}")
    feature = {
        f"{kind}_id": "feature-1",
        "dimension": "relationships",
        "source_id": "synthetic",
        "record_id": "record-1",
    }
    put(feature)
    assert store.get_by_dimension("relationships").items[0]["dimension"] == "relationships"

    put({**feature, "dimension": "work"})

    assert store.get_by_dimension("relationships").items == []
    items = store.get_by_dimension("work").items
    assert len(items) == 1
    assert items[0]["dimension"] == "work"
    dimension, payload = conn.execute(
        f"SELECT dimension, payload_json FROM signal_{kind}s"
    ).fetchone()
    assert dimension == json.loads(payload)["dimension"] == "work"


@pytest.mark.parametrize("kind", ["fact", "score"])
@pytest.mark.parametrize("stored_dimension", ["work", None])
def test_read_uses_indexed_dimension_even_when_payload_is_stale(
    feature_store, kind, stored_dimension
):
    conn, store = feature_store
    conn.execute(
        f"INSERT INTO signal_{kind}s ({kind}_id, dimension, payload_json) VALUES (?, ?, ?)",
        ("legacy", stored_dimension, json.dumps({"dimension": "relationships"})),
    )

    if stored_dimension is not None:
        items = store.get_by_dimension(stored_dimension).items
    else:
        # Scores have no public unfiltered list; both families use this reader.
        items = store._list_rows(
            f"signal_{kind}s", dimension=None, limit=100, offset=0
        ).items

    assert len(items) == 1
    assert items[0]["dimension"] == stored_dimension
    assert items[0]["created_at"] == "2026-09-14T00:00:00Z"
    # Reading reconciles the public projection, without rewriting old evidence.
    payload = conn.execute(f"SELECT payload_json FROM signal_{kind}s").fetchone()[0]
    assert json.loads(payload)["dimension"] == "relationships"


def test_actual_restamp_and_store_reader_agree(feature_store):
    conn, store = feature_store
    store.put_fact(
        {
            "fact_id": "org-1",
            "dimension": "relationships",
            "entity_type": "ORG",
            "source_id": "synthetic",
            "record_id": "record-1",
        }
    )

    result = restamp_fact_dimensions(conn)

    assert result["changed"] == 1
    assert store.get_by_dimension("relationships").items == []
    assert store.get_by_dimension("work").items[0]["dimension"] == "work"
    assert store.list().items[0]["dimension"] == "work"
    assert json.loads(conn.execute("SELECT payload_json FROM signal_facts").fetchone()[0])[
        "dimension"
    ] == "relationships"
