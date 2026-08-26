"""Community name history (PLAN_COMMUNITY_NAMING S1).

A community's identity is its CORE — the top-k most central members, weighted —
not its per-rebuild Louvain index. This table lets a name outlive rebuilds:
match the new core against history (weighted Jaccard), reuse on match, derive
only for genuinely new sets. Owner renames land here with source='owner' and
outrank everything.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "community_names_v1"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def apply_community_names_v1_up(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "wiki_schema_migrations"):
        conn.execute(
            "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
            (MIGRATION_ID,),
        )
    if not _table_exists(conn, "community_names"):
        conn.execute(
            """
            CREATE TABLE community_names (
                name_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                fingerprint_json TEXT NOT NULL,     -- [[entity_id, weight], ...] top-k core
                source TEXT NOT NULL,               -- llm | deterministic | owner
                model TEXT,                         -- when source='llm'
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_matched_at TEXT,
                times_matched INTEGER NOT NULL DEFAULT 0,
                retired_at TEXT                     -- superseded by an owner rename
            )
            """
        )
        conn.execute("CREATE INDEX idx_community_names_active ON community_names(retired_at)")
    conn.commit()
