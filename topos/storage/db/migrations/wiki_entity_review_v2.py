"""Entity review queue v2 (P12): merge candidates with both sides + kind."""

from __future__ import annotations

import sqlite3

MIGRATION_ID = "wiki_entity_review_v2"


def _columns(conn: sqlite3.Connection, table: str) -> set:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def apply_wiki_entity_review_v2_up(conn: sqlite3.Connection) -> None:
    cols = _columns(conn, "entity_review")
    if cols:
        if "kind" not in cols:
            conn.execute(
                "ALTER TABLE entity_review ADD COLUMN kind TEXT NOT NULL DEFAULT 'resolution'"
            )
        if "subject_entity_id" not in cols:
            conn.execute("ALTER TABLE entity_review ADD COLUMN subject_entity_id TEXT")
        if "reason" not in cols:
            conn.execute("ALTER TABLE entity_review ADD COLUMN reason TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_entity_review_status ON entity_review(status, kind)"
        )
    conn.execute(
        "INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)",
        (MIGRATION_ID,),
    )
    conn.commit()


def apply_wiki_entity_review_v2_down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_entity_review_status")
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id = ?", (MIGRATION_ID,))
    conn.commit()
