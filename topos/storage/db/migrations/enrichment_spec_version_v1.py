"""Add nullable ``spec_version`` to enrichment coverage tables (PLAN M3).

Per-row stamps make staleness queryable: anti-joins treat NULL as version 0,
so pre-existing rows are reprocessed when a job's catalog ``spec_version``
bumps. Additive only — no data rewrite.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "enrichment_spec_version_v1"

_COVERAGE_TABLES = (
    "message_emotions",
    "message_entities",
    "message_topics",
    "message_sentiment",
    "signal_embeddings",
    "message_embeddings",
    "browser_url_classification",
    "user_goals",
    "relationship_edges",
)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, col_type: str) -> None:
    if not _table_exists(conn, table):
        return
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def apply_enrichment_spec_version_v1_up(conn: sqlite3.Connection) -> None:
    for table in _COVERAGE_TABLES:
        _add_column_if_missing(conn, table, "spec_version", "INTEGER")
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()
