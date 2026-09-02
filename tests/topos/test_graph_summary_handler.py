"""graph_summary must read columns that exist.

It reported `materialized_at: null` on every node because the column is
`last_run_at`, and the handler's per-field try/except -- there so a missing
TABLE on an older node costs one number, not the reply -- also swallowed a
missing COLUMN into a permanent null. Shipped in 1.3.40; found on the live
reply, not by a test, which is what this is for.
"""

from __future__ import annotations

import inspect
import sqlite3

from topos.core.handlers import graph_summary


def test_the_timestamp_column_exists_in_the_table_the_refresh_writes():
    src = inspect.getsource(graph_summary._counts)
    assert "MAX(last_run_at)" in src
    assert "materialized_at" not in src.split("MAX(")[1].split(")")[0]


def test_counts_read_a_real_materialization_row():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE graph_nodes (id INTEGER PRIMARY KEY);
        CREATE TABLE graph_edges (id INTEGER PRIMARY KEY);
        CREATE TABLE entities (id INTEGER PRIMARY KEY);
        CREATE TABLE graph_materialization_state (
            id INTEGER PRIMARY KEY, dirty_generation INTEGER, materialized_generation INTEGER,
            last_run_at TEXT, last_error TEXT
        );
        INSERT INTO graph_nodes VALUES (1),(2);
        INSERT INTO graph_edges VALUES (1);
        INSERT INTO entities VALUES (1),(2),(3);
        INSERT INTO graph_materialization_state VALUES (1, 5, 5, '2026-09-02T01:11:48+00:00', NULL);
        """
    )
    import topos.core.state as state
    original = state.get_db_connection
    state.get_db_connection = lambda: conn  # type: ignore[assignment]
    try:
        out = graph_summary._counts()
    finally:
        state.get_db_connection = original  # type: ignore[assignment]
    assert out["available"] is True
    assert (out["nodes"], out["edges"], out["entities"]) == (2, 1, 3)
    assert out["materialized_at"] == "2026-09-02T01:11:48+00:00"
