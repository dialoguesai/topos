"""
Gap: CanonicalStore — direct SQL → adapter upsert calls
Sprint: EN-P1-S2
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import sqlite3

import pytest

from topos.storage.canonical.ai_chat.model import CanonicalAIChatMessage
from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
from topos.storage.canonical.canonical_store import InMemoryCanonicalStore, SQLiteCanonicalStore
from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.gap


def test_canonical_tables_manager_delegates_to_store(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    fake = InMemoryCanonicalStore()
    monkeypatch.setattr(
        "topos.storage.canonical.canonical_store.SQLiteCanonicalStore",
        lambda *_args, **_kwargs: fake,
    )
    manager = CanonicalTablesManager(conn)
    msg = CanonicalAIChatMessage(
        message_id="delegated-1",
        conversation_id="conv-1",
        sender_type="human",
        sender_id=None,
        ts="2026-01-01T00:00:00Z",
        content="delegated write",
        content_rendered=None,
        metadata_json=None,
        seq=0,
        source_id="chatgpt",
    )
    written = manager.write_messages_batch([msg], sync_batch_id="batch-delegate", mapping_source_id="chatgpt_file_ingestion")
    assert written == 1
    assert len(fake.upsert_calls) == 1
    assert fake.upsert_calls[0][0] == "ai_chat_messages"


def test_sqlite_canonical_store_routes_mvp_tables() -> None:
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    store = SQLiteCanonicalStore(conn)
    ref = store.upsert(
        "activity_events",
        {
            "event_id": "evt-1",
            "activity_type": "visit",
            "url": "https://example.com",
            "source_id": "browser_visits",
        },
        sync_batch_id="batch-activity",
    )
    assert ref.created is True
    row = conn.execute("SELECT sync_batch_id FROM activity_events WHERE event_id='evt-1'").fetchone()
    assert row[0] == "batch-activity"
