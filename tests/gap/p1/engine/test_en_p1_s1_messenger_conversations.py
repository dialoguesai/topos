"""
Gap: Messenger — partial rows → idempotent conversation_messages + identity
Sprint: EN-P1-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import sqlite3

import pytest

from topos.storage.canonical.conversations_tables import ConversationsTablesManager, ensure_all_tables
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.gap


def _message(message_id: str = "imsg-1") -> dict:
    return {
        "message_id": message_id,
        "thread_id": "thread-abc",
        "ts": "2026-01-02T12:00:00Z",
        "sender_type": "human",
        "sender_id": "+15551234567",
        "content": "ping",
        "from_self": 0,
    }


def test_messenger_idempotent_upsert_with_provenance() -> None:
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    ensure_all_tables(conn)
    manager = ConversationsTablesManager(conn)

    first = manager.upsert_message_batch(
        [_message()],
        dataset_id="user:imessage",
        source_id="imessage",
        sync_batch_id="sync-a",
    )
    assert first["messages_created"] == 1

    row = conn.execute(
        """
        SELECT source_record_id, ingested_at, sync_batch_id
        FROM conversation_messages WHERE message_id=?
        """,
        ("imsg-1",),
    ).fetchone()
    assert row is not None
    assert row[0] == "imsg-1"
    assert row[1]
    assert row[2] == "sync-a"

    second = manager.upsert_message_batch(
        [_message()],
        dataset_id="user:imessage",
        source_id="imessage",
        sync_batch_id="sync-b",
    )
    assert second["messages_created"] == 0
    count = conn.execute("SELECT COUNT(*) FROM conversation_messages").fetchone()[0]
    assert count == 1


def _migrated_conn() -> sqlite3.Connection:
    """A database the way a running node has it: tables first, then migrations."""
    conn = sqlite3.connect(":memory:")
    ensure_all_tables(conn)
    apply_all_migrations(conn)
    return conn


def test_every_parent_row_in_a_batch_survives_on_a_migrated_database() -> None:
    """Re-running the column helpers inside the batch must not roll the batch back.

    Each helper re-ran ALTER TABLE, hit "duplicate column", and answered with
    conn.rollback() — inside batched_writes, whose commit is deferred, so every
    pending conversation/contact/participant insert but the last was discarded
    while the returned counts still claimed them.
    """
    conn = _migrated_conn()
    records = [
        {**_message("imsg-a"), "thread_id": "thread-a", "sender_id": "+15550000001"},
        {**_message("imsg-b"), "thread_id": "thread-b", "sender_id": "+15550000002"},
        {**_message("imsg-c"), "thread_id": "thread-c", "sender_id": "+15550000003"},
    ]
    result = ConversationsTablesManager(conn).upsert_message_batch(
        records, dataset_id="user:imessage", source_id="imessage"
    )

    assert result["conversations_created"] == 3
    for table in ("conversations", "contacts", "contact_identifiers", "conversation_participants"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        assert count == 3, f"{table} kept {count} of 3 rows"


def test_resyncing_a_conversation_keeps_owner_and_provenance_columns() -> None:
    """INSERT OR REPLACE rewrote the parent row from 5 columns, nulling the rest."""
    conn = _migrated_conn()
    manager = ConversationsTablesManager(conn)
    manager.upsert_message_batch([_message()], dataset_id="user:imessage", source_id="imessage")
    conn.execute(
        """
        UPDATE conversations
        SET context_tag = 'personal', context_tag_source = 'owner',
            source_record_id = 'chat-guid-1', ingested_at = '2026-01-02T00:00:00Z',
            sync_batch_id = 'sync-a', created_at = '2026-01-02 00:00:00'
        WHERE conversation_id = 'thread-abc'
        """
    )
    conn.commit()

    manager.upsert_message_batch([_message("imsg-2")], dataset_id="user:imessage", source_id="imessage")

    row = conn.execute(
        """
        SELECT context_tag, context_tag_source, source_record_id, ingested_at,
               sync_batch_id, created_at, source_id
        FROM conversations WHERE conversation_id = 'thread-abc'
        """
    ).fetchone()
    assert row == (
        "personal",
        "owner",
        "chat-guid-1",
        "2026-01-02T00:00:00Z",
        "sync-a",
        "2026-01-02 00:00:00",
        "imessage",
    )
