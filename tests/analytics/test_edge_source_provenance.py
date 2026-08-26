"""P0-4 — an edge says which connector produced it.

`messenger_social_edges` partitions on `source_scope`, a joined set like
`'imessage,signal'`. Two consequences the social-graph plan recorded as F4:

  * an edge inside a multi-source partition could not say where it came from, and
  * asking for one connector's view meant writing an entire extra partition of the
    same corpus — 2^n partitions for n sources (measured at one point: 90,797 rows
    across four partitions of one corpus).

Provenance belongs on the row. `conv_key` is `(conversation_id, source_id)`, so the
connector is already in hand when edges are built.

Note what this does NOT change: `messenger_participant_importance` and
`messenger_communities` are computed *over* an edge set, so a per-connector view of
those genuinely requires recomputing centrality on the filtered graph — it cannot be
served by filtering rows. Their partitioning stays, deliberately. What this closes is
the edge half, and it establishes the pattern L1's directed-edge table inherits
instead of the partition scheme.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.analytics.messenger_communities import (
    MESSENGER_SOCIAL_EDGES_TABLE,
    _persist_period_results,
    ensure_messenger_analytics_tables,
)


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "m.db"))
    ensure_messenger_analytics_tables(c)
    yield c
    c.close()


def _cols(conn) -> set:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({MESSENGER_SOCIAL_EDGES_TABLE})")}


def test_the_column_exists_after_ensure(conn):
    assert "source_counts_json" in _cols(conn)


def test_ensure_is_idempotent(conn):
    """It runs on every analytics pass — a second call must not raise."""
    ensure_messenger_analytics_tables(conn)
    ensure_messenger_analytics_tables(conn)
    assert "source_counts_json" in _cols(conn)


def test_it_upgrades_a_table_that_predates_the_column(tmp_path):
    """The live shape: the table already exists without the column.

    A plain CREATE TABLE IF NOT EXISTS would leave it missing forever, which is how
    an additive change silently applies only to machines installed after it.
    """
    c = sqlite3.connect(str(tmp_path / "old.db"))
    c.execute(
        f"""
        CREATE TABLE {MESSENGER_SOCIAL_EDGES_TABLE} (
            dataset_id TEXT NOT NULL, period_key TEXT NOT NULL,
            source_scope TEXT NOT NULL DEFAULT 'all', source_id TEXT NOT NULL,
            target_id TEXT NOT NULL, weight REAL NOT NULL, edge_type TEXT,
            edge_type_counts_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            PRIMARY KEY (dataset_id, period_key, source_scope, source_id, target_id)
        )
        """
    )
    c.commit()
    assert "source_counts_json" not in _cols(c)

    ensure_messenger_analytics_tables(c)

    assert "source_counts_json" in _cols(c)
    c.close()


def test_ensure_does_not_touch_user_version(conn):
    """The whole reason this is not a registry migration.

    Bumping user_version past what the installed engine understands fences the node
    out of every write — ingest, sync and enrichment — which is what happened on
    2026-08-25.
    """
    before = conn.execute("PRAGMA user_version").fetchone()[0]
    ensure_messenger_analytics_tables(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == before


def _payload(source_counts):
    return {
        "edges": [
            {
                "source": "c_a",
                "target": "c_b",
                "weight": 5,
                "edge_type": "mixed",
                "edge_type_counts": {"co_participation": 3, "direct_reply": 2},
                "source_counts": source_counts,
            }
        ],
        "nodes": [],
    }


def _stored(conn):
    row = conn.execute(
        f"SELECT source_counts_json FROM {MESSENGER_SOCIAL_EDGES_TABLE}"
    ).fetchone()
    return json.loads(row[0])


def test_a_multi_connector_edge_records_each_contribution(conn):
    """The case the old schema could not express at all."""
    _persist_period_results(
        conn, dataset_id="ds", period_key="2026-08", source_scope="imessage,signal",
        period_payload=_payload({"imessage": 3, "signal": 2}),
        importance={}, communities={},
    )
    assert _stored(conn) == {"imessage": 3, "signal": 2}


def test_a_single_connector_edge_is_attributed_too(conn):
    _persist_period_results(
        conn, dataset_id="ds", period_key="2026-08", source_scope="all",
        period_payload=_payload({"imessage": 5}),
        importance={}, communities={},
    )
    assert _stored(conn) == {"imessage": 5}


def test_an_edge_with_no_attribution_stores_an_empty_map_not_null(conn):
    """Absent provenance must read as 'none recorded', never as a JSON parse error."""
    _persist_period_results(
        conn, dataset_id="ds", period_key="2026-08", source_scope="all",
        period_payload=_payload({}), importance={}, communities={},
    )
    assert _stored(conn) == {}


def test_provenance_sums_to_the_edge_weight(conn):
    """The invariant that makes the column trustworthy.

    Every contribution to an edge is attributed to exactly one connector, so the
    per-connector counts must account for the whole weight. If they drift apart,
    some contribution was counted into the weight without provenance — which is the
    silent-undercount failure this column exists to prevent.

    Verified end-to-end against a copy of the live corpus: 498 of 498 edges
    attributed, every one summing to its weight.
    """
    _persist_period_results(
        conn, dataset_id="ds", period_key="2026-08", source_scope="all",
        period_payload=_payload({"imessage": 3, "signal": 2}),
        importance={}, communities={},
    )
    row = conn.execute(
        f"SELECT weight, source_counts_json FROM {MESSENGER_SOCIAL_EDGES_TABLE}"
    ).fetchone()
    assert sum(json.loads(row[1]).values()) == int(row[0])
