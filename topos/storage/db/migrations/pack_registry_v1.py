"""Pack registry (W2.2 / F1.2): which ontology packs this node runs, at which
version, enabled or not, with the owner's disclosure default per pack.

A REAL TABLE rather than config-file state because enablement drives ingest-time
behaviour (the derivation enrichment job consults it per record batch) and
uninstall/re-run semantics need a place to record the pack version a node last
ran — the ``ontology_version`` stamped on facts answers "what wrote this", the
registry answers "what runs NOW".

Rows are seeded lazily by the engine from bundled pack defaults (D2 matrix:
everything ON owner_only except beliefs.* opt-in) the first time the derivation
job runs — the migration only creates the shape. Seeding here would duplicate
the defaults matrix into SQL and drift from the pack files, the exact
stored-but-never-applied trap this project keeps finding.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "pack_registry_v1"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def apply_pack_registry_v1_up(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "wiki_schema_migrations"):
        conn.execute(
            "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
            (MIGRATION_ID,),
        )
    if not _table_exists(conn, "pack_registry"):
        conn.execute(
            """
            CREATE TABLE pack_registry (
                pack_id TEXT PRIMARY KEY,
                version TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                disclosure_default TEXT NOT NULL DEFAULT 'owner_only',
                installed_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                last_run_at TEXT,
                last_run_version TEXT
            )
            """
        )
    conn.commit()
