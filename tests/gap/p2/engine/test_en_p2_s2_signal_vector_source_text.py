"""Gap: vector source text lookup by record_id."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.gap


def test_get_vector_source_text_from_ai_chat_messages(tmp_path, monkeypatch) -> None:
    import sqlite3

    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE ai_chat_messages (
            message_id TEXT PRIMARY KEY,
            conversation_id TEXT,
            sender_type TEXT,
            content TEXT,
            content_rendered TEXT,
            source_id TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, content, source_id)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("msg-1", "conv-1", "assistant", "# Title\n\nHello **world**", "chatgpt_ingestion"),
    )
    conn.commit()

    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("TOPOS_DATABASE_MODE", "local")

    from topos.features.signal.service import get_signal_service

    service = get_signal_service(conn=conn)
    result = service.get_vector_source_text(record_id="msg-1")

    assert result["found"] is True
    assert "Hello **world**" in result["content"]
    assert result["source_id"] == "chatgpt_ingestion"

    missing = service.get_vector_source_text(record_id="missing")
    assert missing["found"] is False
