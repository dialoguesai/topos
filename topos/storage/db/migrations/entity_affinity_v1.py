"""Affinity rebuild bookkeeping (PLAN_GRAPH_QUERY_AND_LATENT_EDGES §3.1/§3.4a).

Mirrors ``cluster_recompute_log``: one row per ``rebuild_affinity_edges`` run.

The column that earns this table is ``resolved_cosine`` — the absolute cosine
the percentile floor landed on for this run. The percentile P is fixed and
portable; the cosine it resolves to is node-specific and *drifts as data
accumulates*, so that series is the only way a node's distribution moving out
from under the threshold becomes observable rather than silent. It is also the
input to any future auto-retuning, which is why this log is not optional.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "entity_affinity_v1"


def apply_entity_affinity_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS affinity_recompute_log (
            recompute_id INTEGER PRIMARY KEY AUTOINCREMENT,
            entities_considered INTEGER NOT NULL DEFAULT 0,
            pairs_considered INTEGER NOT NULL DEFAULT 0,
            edges_written INTEGER NOT NULL DEFAULT 0,
            ceiling INTEGER NOT NULL DEFAULT 0,
            ceiling_hit INTEGER NOT NULL DEFAULT 0,
            percentile REAL,
            resolved_cosine REAL,
            floor_cosine REAL,
            spec_version INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_affinity_recompute_log_created
        ON affinity_recompute_log(created_at)
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_entity_affinity_v1_down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS affinity_recompute_log")
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
