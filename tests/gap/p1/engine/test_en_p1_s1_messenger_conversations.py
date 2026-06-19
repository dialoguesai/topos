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
