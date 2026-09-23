"""Persist exact owner-only record selections without deleting their content."""
import sqlite3

MIGRATION_ID = "owner_only_records_v1"


def apply_owner_only_records_v1_up(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS owner_only_records (
        canonical_table TEXT NOT NULL,
        record_id TEXT NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (canonical_table, record_id)
    )""")
    conn.execute("INSERT OR IGNORE INTO wiki_schema_migrations (migration_id) VALUES (?)", (MIGRATION_ID,))
    conn.commit()
