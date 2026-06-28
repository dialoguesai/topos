"""Disclosure columns are ensured even when migration row was recorded early."""

from __future__ import annotations

import sqlite3

from topos.storage.db.migrations.canonical_disclosure_v1 import apply_canonical_disclosure_v1_up


def test_disclosure_columns_added_after_migration_row_and_late_table() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE wiki_schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "INSERT INTO wiki_schema_migrations (migration_id) VALUES ('canonical_disclosure_v1')"
    )
    conn.execute(
        """
        CREATE TABLE journal_entries (
            entry_id TEXT PRIMARY KEY,
            content TEXT,
            source_id TEXT
        )
        """
    )
    conn.commit()

    apply_canonical_disclosure_v1_up(conn)

    cols = {row[1] for row in conn.execute("PRAGMA table_info(journal_entries)").fetchall()}
    assert "content_disclosure" in cols
    assert "content_disclosure_hash" in cols
