"""Multi-source message retrieval for messages:read."""

import sqlite3

import pytest

from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.canonical.conversations_tables import ConversationsTablesManager
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "messages_multi.db"
    c = sqlite3.connect(str(db_path))
    apply_all_migrations(c)
    mgr = ConversationsTablesManager(c)
    for source_id, message_id, content in (
        ("demo_messenger_file", "msg-demo-1", "Demo messenger hello"),
        ("imessage", "msg-im-1", "iMessage follow-up"),
    ):
        mgr.upsert_message_batch(
            [
                {
                    "message_id": message_id,
                    "thread_id": f"thread-{source_id}",
                    "content": content,
                    "ts": "2026-03-13T12:00:00Z",
                    "sender_type": "contact",
                }
            ],
            dataset_id="user:default:device",
            source_id=source_id,
            sync_batch_id=f"batch-{message_id}",
        )
    c.commit()
    yield c
    c.close()


def test_multi_source_messages_retrieval(conn) -> None:
    adapters = AdapterFactory.create("local_database", conn=conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    manifest = resolve_scope_manifest("messages:read")
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="raw",
            query_text="",
            installed_source_ids=["demo_messenger_file", "imessage"],
        )
    )
    rows = bundle.context_packet.get("rows") or []
    message_ids = {str(row.get("record_id") or "") for row in rows}
    assert "msg-demo-1" in message_ids
    assert "msg-im-1" in message_ids
