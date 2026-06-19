"""PRAGMA table_info assertions for Phase 0 key tables."""

from __future__ import annotations

import sqlite3

from topos.storage.db.migrations.wiki_mvp_phase0 import apply_wiki_mvp_phase0_up

EXPECTED_COLUMNS = {
    "signal_facts": {
        "fact_id",
        "dimension",
        "source_id",
        "record_id",
        "model",
        "provider",
        "payload_json",
        "created_at",
    },
    "signal_embeddings": {
        "embedding_id",
        "record_id",
        "source_id",
        "signal_dimension",
        "model",
        "provider",
        "dims",
        "text_preview",
        "provenance_json",
        "vector_blob",
        "created_at",
    },
    "query_sessions": {
        "session_id",
        "requester_id",
        "intent_hash",
        "envelope_json",
        "ttl_expires_at",
        "created_at",
        "updated_at",
    },
    "query_artifacts": {
        "artifact_id",
        "session_id",
        "cache_key",
        "public_result_json",
        "retrieval_fingerprint",
        "game_layer_strategy",
        "created_at",
    },
    "graph_nodes": {"node_id", "node_type", "label", "metadata_json", "source_id", "created_at"},
    "graph_edges": {
        "edge_id",
        "src_node_id",
        "dst_node_id",
        "edge_type",
        "weight",
        "metadata_json",
        "source_id",
        "created_at",
    },
}


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def test_migrations_phase0_columns() -> None:
    conn = sqlite3.connect(":memory:")
    apply_wiki_mvp_phase0_up(conn)
    for table, expected in EXPECTED_COLUMNS.items():
        assert expected.issubset(_column_names(conn, table)), table
