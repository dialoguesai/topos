"""
Gap: Query session / vector / graph migrations — stubs → dedicated tables
Sprint: EN-P0-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import sqlite3

import pytest

from topos.storage.db.migrations.wiki_mvp_phase0 import apply_wiki_mvp_phase0_up

pytestmark = pytest.mark.gap

QUERY_VECTOR_GRAPH_TABLES = [
    "query_sessions",
    "query_artifacts",
    "signal_embeddings",
    "graph_nodes",
    "graph_edges",
]


def test_query_session_vector_graph_tables_exist() -> None:
    conn = sqlite3.connect(":memory:")
    apply_wiki_mvp_phase0_up(conn)
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    for table in QUERY_VECTOR_GRAPH_TABLES:
        assert table in existing, table
