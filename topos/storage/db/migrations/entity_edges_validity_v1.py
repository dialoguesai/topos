"""B2.2: entity_edges validity — close-and-supersede semantics for edges.

The wiki_entities_v1 table carried ``UNIQUE(src_entity_id, dst_entity_id,
edge_type)``, which makes a closed edge revision structurally impossible (an
ended relationship cannot coexist with its history). SQLite cannot drop a
table-level UNIQUE constraint, so this is a table rebuild (the signal_objects
pattern):

  * new table WITHOUT the UNIQUE constraint, plus ``valid_from``/``valid_to``
  * copy rows with ``valid_from = created_at`` and ``valid_to = NULL``
  * swap, then a PARTIAL UNIQUE index on (src, dst, type) WHERE valid_to IS
    NULL — exactly one ACTIVE row per triple, closed history unconstrained
  * the weight-DESC lookup indexes are recreated (dropped with the old table)

Idempotent: keyed on the presence of the ``valid_to`` column, so it is safe to
re-run after legacy DDL and cheap when already applied.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "entity_edges_validity_v1"

_NEW_TABLE_COLUMNS_DDL = """
    edge_id TEXT PRIMARY KEY,
    src_entity_id TEXT NOT NULL,
    dst_entity_id TEXT NOT NULL,
    edge_type TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 0,
    evidence_count INTEGER NOT NULL DEFAULT 0,
    last_event_at TEXT,
    valid_from TEXT,
    valid_to TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
"""


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row)


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _ensure_indexes(conn: sqlite3.Connection) -> None:
    # Active-row uniqueness replaces the old table-level UNIQUE constraint.
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_edges_active
           ON entity_edges(src_entity_id, dst_entity_id, edge_type)
           WHERE valid_to IS NULL"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entity_edges_src ON entity_edges(src_entity_id, weight DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_entity_edges_dst ON entity_edges(dst_entity_id, weight DESC)"
    )


def _record(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )


def apply_entity_edges_validity_v1_up(conn: sqlite3.Connection) -> None:
    if not _table_exists(conn, "entity_edges"):
        # Fresh/partial schema: create directly in the new shape so a later
        # legacy CREATE IF NOT EXISTS (wiki_entities_v1) becomes a no-op.
        conn.execute(f"CREATE TABLE entity_edges ({_NEW_TABLE_COLUMNS_DDL})")
        _ensure_indexes(conn)
        _record(conn)
        conn.commit()
        return

    if "valid_to" in _columns(conn, "entity_edges"):
        # Already the new shape — just guarantee the indexes exist (a legacy
        # DDL re-run cannot remove them, but partial schemas might lack them).
        _ensure_indexes(conn)
        _record(conn)
        conn.commit()
        return

    # Rebuild: copy under a scratch name, swap, recreate indexes. The whole
    # copy+swap runs inside ONE explicit transaction so a crash mid-rebuild
    # can never leave the database without an entity_edges table (on an
    # autocommit connection each statement would otherwise land immediately,
    # and a re-run after a mid-swap crash would recreate an EMPTY table).
    conn.commit()  # close any implicit transaction before the explicit BEGIN
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DROP TABLE IF EXISTS entity_edges_validity_new")
        conn.execute(f"CREATE TABLE entity_edges_validity_new ({_NEW_TABLE_COLUMNS_DDL})")
        conn.execute(
            """
            INSERT INTO entity_edges_validity_new (
                edge_id, src_entity_id, dst_entity_id, edge_type,
                weight, evidence_count, last_event_at,
                valid_from, valid_to, metadata_json, created_at, updated_at
            )
            SELECT edge_id, src_entity_id, dst_entity_id, edge_type,
                   weight, evidence_count, last_event_at,
                   created_at, NULL, metadata_json, created_at, updated_at
            FROM entity_edges
            """
        )
        conn.execute("DROP TABLE entity_edges")
        conn.execute("ALTER TABLE entity_edges_validity_new RENAME TO entity_edges")
        _ensure_indexes(conn)
        _record(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
