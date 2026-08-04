"""Unit tests for source scrub attribution purge and tier-B raw/flat removal."""

from __future__ import annotations

import sqlite3

import pytest

from topos.sources.scrub_attribution import (
    remove_raw_and_flat_tables,
    scrub_attributed_rows,
)
from topos.sources.definitions import CANONICAL_ADDRESS_BOOK_SOURCE_ID
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "scrub.db"))
    db.row_factory = sqlite3.Row
    apply_all_migrations(db)
    return db


def _ensure_vec_table(conn: sqlite3.Connection) -> None:
    # With sqlite-vec loaded (process-wide auto-extension since connection
    # tuning landed) the migrations already created the real vec0 table and
    # this is a no-op; the plain-table fallback keeps the test meaningful
    # in environments without the extension.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS signal_embeddings_vec (
            embedding_id TEXT PRIMARY KEY,
            embedding BLOB
        )
        """
    )


def _vec_blob() -> bytes:
    # Valid 384-dim unit vector — the real vec0 table enforces declared dims.
    import struct

    return struct.pack("<384f", 1.0, *([0.0] * 383))


def test_scrub_attributed_rows_removes_one_source_and_keeps_other(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO journal_entries (entry_id, source_id, content, entry_at)
        VALUES ('j1', 'grow_journal', 'alpha', '2026-01-01'),
               ('j2', 'other_source', 'beta', '2026-01-02')
        """
    )
    conn.execute(
        """
        INSERT INTO signal_embeddings (
            embedding_id, record_id, source_id, signal_dimension, model, provider,
            dims, text_preview, provenance_json
        ) VALUES
            ('emb-grow', 'j1', 'grow_journal', 'memory', 'test', 'test', 384, 'alpha', '{}'),
            ('emb-other', 'j2', 'other_source', 'memory', 'test', 'test', 384, 'beta', '{}')
        """
    )
    conn.commit()

    result = scrub_attributed_rows(conn, "grow_journal")

    assert conn.execute("SELECT COUNT(*) FROM journal_entries WHERE source_id='grow_journal'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM journal_entries WHERE source_id='other_source'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM signal_embeddings WHERE source_id='grow_journal'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM signal_embeddings WHERE source_id='other_source'").fetchone()[0] == 1
    assert result.rows_deleted >= 2
    assert any(item.table == "journal_entries" and item.action == "rows_deleted" for item in result.tables)
    assert any(item.table == "signal_embeddings" and item.action == "rows_deleted" for item in result.tables)


def test_scrub_attributed_rows_deletes_vec_sidecar_rows(conn: sqlite3.Connection) -> None:
    _ensure_vec_table(conn)
    conn.execute(
        """
        INSERT INTO signal_embeddings (
            embedding_id, record_id, source_id, signal_dimension, model, provider,
            dims, text_preview, provenance_json
        ) VALUES ('emb-1', 'r1', 'browser_visits', 'memory', 'test', 'test', 384, 'visit', '{}')
        """
    )
    conn.execute(
        "INSERT INTO signal_embeddings_vec (embedding_id, embedding) VALUES (?, ?)",
        ("emb-1", _vec_blob()),
    )
    conn.commit()

    result = scrub_attributed_rows(conn, "browser_visits")

    assert conn.execute("SELECT COUNT(*) FROM signal_embeddings WHERE source_id='browser_visits'").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM signal_embeddings_vec WHERE embedding_id='emb-1'").fetchone()[0] == 0
    vec_action = next((item for item in result.tables if item.table == "vector_index"), None)
    assert vec_action is not None
    assert vec_action.action == "vec_rows_deleted"
    assert vec_action.count == 1


def test_scrub_attributed_rows_drops_empty_raw_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE raw_chat_messages_growjournal (
            source_system TEXT,
            source_record_id TEXT,
            payload_json TEXT,
            created_at TEXT,
            PRIMARY KEY (source_system, source_record_id)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO raw_chat_messages_growjournal (source_system, source_record_id, payload_json, created_at)
        VALUES ('grow_journal', 'r1', '{}', '2026-01-01')
        """
    )
    conn.commit()

    result = scrub_attributed_rows(conn, "grow_journal")

    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert "raw_chat_messages_growjournal" not in tables
    assert any(item.action == "table_dropped" for item in result.tables)


def test_remove_raw_and_flat_tables_leaves_canonical_rows(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE grow_journal_sessions (
            record_id TEXT PRIMARY KEY,
            source_id TEXT,
            payload_json TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO grow_journal_sessions (record_id, source_id, payload_json)
        VALUES ('s1', 'grow_journal', '{}')
        """
    )
    conn.execute(
        """
        INSERT INTO journal_entries (entry_id, source_id, content, entry_at)
        VALUES ('j1', 'grow_journal', 'keep me', '2026-01-01')
        """
    )
    conn.commit()

    source_def = {
        "source_id": "grow_journal",
        "source_type": "ui_stream",
        "tables": [{"table_id": "grow_journal_sessions", "columns": [{"name": "record_id", "type": "text"}]}],
    }
    actions = remove_raw_and_flat_tables(conn, source_def, "grow_journal")

    assert conn.execute("SELECT COUNT(*) FROM grow_journal_sessions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM journal_entries WHERE source_id='grow_journal'").fetchone()[0] == 1
    assert any(item.table == "grow_journal_sessions" and item.count == 1 for item in actions)


def test_purge_legacy_summary_shape(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO journal_entries (entry_id, source_id, content, entry_at)
        VALUES ('j1', 'grow_journal', 'alpha', '2026-01-01')
        """
    )
    conn.commit()

    summary = scrub_attributed_rows(conn, "grow_journal").to_legacy_summary()

    assert "tables_dropped" in summary
    assert "rows_deleted" in summary
    assert summary["rows_deleted"] == 1
    assert isinstance(summary["table_actions"], list)


def test_source_scrub_preserves_shared_contacts_and_collects_orphans(
    conn: sqlite3.Connection,
) -> None:
    conn.executemany(
        """
        INSERT INTO contacts (
            contact_id, dataset_id, source_id, display_name, is_self, created_at, updated_at
        ) VALUES (?, 'dataset-1', ?, ?, 0, datetime('now'), datetime('now'))
        """,
        [
            ("shared", CANONICAL_ADDRESS_BOOK_SOURCE_ID, "Shared Person"),
            ("orphan", CANONICAL_ADDRESS_BOOK_SOURCE_ID, "Channel-only Person"),
        ],
    )
    conn.executemany(
        """
        INSERT INTO contact_identifiers (
            dataset_id, source_id, identifier, identifier_type, contact_id, created_at, updated_at
        ) VALUES ('dataset-1', ?, ?, 'phone', ?, datetime('now'), datetime('now'))
        """,
        [
            ("imessage", "+15550001", "shared"),
            ("signal", "+15550001", "shared"),
            ("imessage", "+15550002", "orphan"),
        ],
    )
    conn.commit()

    result = scrub_attributed_rows(conn, "imessage")

    assert conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE contact_id='shared'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM contacts WHERE contact_id='orphan'"
    ).fetchone()[0] == 0
    assert [
        row[0]
        for row in conn.execute(
            "SELECT source_id FROM contact_identifiers WHERE contact_id='shared'"
        ).fetchall()
    ] == ["signal"]
    assert any(
        item.table == "contacts" and item.action == "rows_deleted" and item.count == 1
        for item in result.tables
    )


def test_generic_scrub_rejects_canonical_address_book(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="canonical address book"):
        scrub_attributed_rows(conn, CANONICAL_ADDRESS_BOOK_SOURCE_ID)
