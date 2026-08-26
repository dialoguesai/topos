"""Provenance on the review queue (W4.6): quarantined assertions must carry
what the writer knew — pack, source refs, quote, and the unresolved-subject
hint — or promotion mints evidence-less facts. Nullable columns: rows minted
before this migration genuinely lack them.
"""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "conflict_provenance_v1"

_COLS = (
    ("pack_id", "TEXT"),
    ("source_refs_json", "TEXT"),
    ("quote", "TEXT"),
    ("about_hint", "TEXT"),
    ("updated_at", "TEXT"),
)


def apply_conflict_provenance_v1_up(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='wiki_schema_migrations'"
    ).fetchone()
    if row:
        conn.execute(
            "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
            (MIGRATION_ID,),
        )
    have = {r[1] for r in conn.execute("PRAGMA table_info(fact_conflicts)")}
    for col, decl in _COLS:
        if col not in have:
            conn.execute(f"ALTER TABLE fact_conflicts ADD COLUMN {col} {decl}")
    conn.commit()
