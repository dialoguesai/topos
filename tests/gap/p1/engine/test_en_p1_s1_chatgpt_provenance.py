"""
Gap: ChatGPT — direct SQL writes → CanonicalStore + MappingStore provenance
Sprint: EN-P1-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import sqlite3

import pytest

from topos.storage.canonical.ai_chat import CanonicalTablesManager, Canonicalizer
from topos.storage.canonical.mapping_store import MappingRecord, SQLiteMappingStore
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.gap


def _staging_record(message_id: str = "msg-1") -> dict:
    return {
        "message_id": message_id,
        "dataset_id": "user:chatgpt",
        "thread_id": "conv-1",
        "ts": "2026-01-01T00:00:00Z",
        "sender_type": "human",
        "content": "hello world",
        "source_id": "chatgpt_file_ingestion",
    }


def test_chatgpt_canonical_store_and_mapping_provenance() -> None:
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    manager = CanonicalTablesManager(conn)
    canonicalizer = Canonicalizer(manager)

    result = canonicalizer.canonicalize_staging_batch(
        [_staging_record()],
        source="chatgpt",
        sync_batch_id="batch-1",
        mapping_source_id="chatgpt_file_ingestion",
    )
    assert result["messages_created"] == 1

    row = conn.execute(
        """
        SELECT source_id, source_record_id, ingested_at, sync_batch_id, content_hash
        FROM ai_chat_messages WHERE message_id=?
        """,
        ("msg-1",),
    ).fetchone()
    assert row is not None
    assert row[0] == "chatgpt_file_ingestion"
    assert row[1] == "msg-1"
    assert row[2]
    assert row[3] == "batch-1"

    mapping = SQLiteMappingStore(conn).get_mapping("chatgpt_file_ingestion", "msg-1")
    assert mapping is not None
    assert mapping.canonical_id == "msg-1"
    assert mapping.canonical_table == "ai_chat_messages"

    second = canonicalizer.canonicalize_staging_batch(
        [_staging_record()],
        source="chatgpt",
        sync_batch_id="batch-2",
        mapping_source_id="chatgpt_file_ingestion",
    )
    assert second["messages_created"] == 0
    count = conn.execute("SELECT COUNT(*) FROM ai_chat_messages").fetchone()[0]
    assert count == 1

    mapping_store = SQLiteMappingStore(conn)
    mapping_store.save_mapping(
        MappingRecord(
            source_id="chatgpt_file_ingestion",
            source_record_id="msg-1",
            canonical_id="msg-1",
            canonical_table="ai_chat_messages",
        )
    )
    assert mapping_store.get_mapping("chatgpt_file_ingestion", "msg-1") is not None
