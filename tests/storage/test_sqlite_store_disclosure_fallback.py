"""SQLite grantee reads fail CLOSED when a record's disclosure is still pending.

Constructing the store applies the canonical disclosure migration, which adds the
`content_disclosure` column. A record whose ingest privacy layer has not run yet has a
NULL disclosure value — a grantee (default_disclosure) read must surface a placeholder,
never the raw content.
"""

from __future__ import annotations

import sqlite3

from topos.storage.adapters.sqlite.stores import SQLiteCanonicalStore


def test_list_default_disclosure_withholds_raw_when_disclosure_pending() -> None:
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
        VALUES ('e1', 'met alice about the merger', 'grow_journal', '2026-01-01')
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
    content = str(page.items[0].get("content", ""))
    assert "merger" not in content
    assert content == "[disclosure pending]"


def test_list_owner_still_sees_raw_when_disclosure_pending() -> None:
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
        VALUES ('e1', 'met alice about the merger', 'grow_journal', '2026-01-01')
        """
    )
    conn.commit()

    store = SQLiteCanonicalStore(conn)
    page = store.list("journal_entries", disclosure_tier="owner_raw", limit=10)
    assert page.total == 1
    assert "merger" in str(page.items[0].get("content", ""))
