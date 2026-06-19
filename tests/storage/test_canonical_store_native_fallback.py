"""Native canonical table fallback when wiki mirror is empty."""

import sqlite3

import pytest

from topos.storage.adapters.sqlite.stores import SQLiteCanonicalStore
from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture
def conn(tmp_path):
    db_path = tmp_path / "native_canonical.db"
    c = sqlite3.connect(str(db_path))
    apply_all_migrations(c)
    CanonicalTablesManager(c)
    c.execute(
        """
        INSERT INTO ai_chat_messages (
            message_id, conversation_id, sender_type, source_id, content, event_at
        ) VALUES ('m_git', 'conv1', 'user', 'chatgpt_file_ingestion', 'setup git and GitHub repo', '2026-01-01')
        """
    )
    c.commit()
    yield c
    c.close()


def test_list_ai_chat_messages_from_native_table(conn) -> None:
    store = SQLiteCanonicalStore(conn)
    page = store.list("ai_chat_messages", limit=10, offset=0)
    assert page.total == 1
    assert page.items[0]["record_id"] == "m_git"
    assert "git" in page.items[0]["content"].lower()
