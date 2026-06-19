"""GT-EN-QQ-S3-01/02: Raw mode reads native ai_chat_messages with query filter."""

import sqlite3

import pytest

from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
from topos.storage.db.migrations import apply_all_migrations

from qq_helpers import ai_conversations_manifest

pytestmark = pytest.mark.gap


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "raw_native.db"
    c = sqlite3.connect(str(db_path))
    apply_all_migrations(c)
    CanonicalTablesManager(c)
    c.executemany(
        """
        INSERT INTO ai_chat_messages (
            message_id, conversation_id, sender_type, source_id, content, event_at
        ) VALUES (?, 'conv1', 'user', 'chatgpt_file_ingestion', ?, '2026-01-01')
        """,
        [
            ("m1", "how to configure nginx reverse proxy"),
            ("m2", "git push to GitHub repository"),
        ],
    )
    c.commit()
    yield c
    c.close()


def test_raw_git_query_returns_git_row(conn) -> None:
    adapters = AdapterFactory.create("local_database", conn=conn)
    adapter = DefaultSignalRetrievalAdapter(adapters)
    bundle = adapter.retrieve(
        RetrievalRequest(
            manifest=ai_conversations_manifest(),
            access_mode="raw",
            query_text="git GitHub",
        )
    )
    rows = bundle.context_packet.get("rows") or []
    assert len(rows) >= 1
    assert any("git" in str(row.get("content", "")).lower() for row in rows)
    assert not any("nginx" in str(row.get("content", "")).lower() for row in rows)
