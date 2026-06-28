"""SQLite store falls back when disclosure columns are missing."""

from __future__ import annotations

import sqlite3

from topos.storage.adapters.sqlite.stores import SQLiteCanonicalStore


def test_list_default_disclosure_falls_back_without_content_disclosure_column() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """
        CREATE TABLE journal_entries (
            entry_id TEXT PRIMARY KEY,
            entry_at TEXT,
            mood_tag TEXT,
            category TEXT,
            content TEXT,
            people TEXT,
            place_name TEXT,
            source_id TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO journal_entries (entry_id, content, source_id, entry_at)
        VALUES ('e1', 'work goal: ship feature', 'grow_journal', '2026-01-01')
        """
    )
    conn.commit()

    store = SQLiteCanonicalStore(conn)
    page = store.list(
        "journal_entries",
        disclosure_tier="default_disclosure",
        limit=10,
    )
    assert page.total == 1
    assert "work goal" in str(page.items[0].get("content", ""))
