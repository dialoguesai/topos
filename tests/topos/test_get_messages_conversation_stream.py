"""get_messages with message_stream=conversation (messenger / iMessage lane)."""

import sqlite3

import pytest

from topos.core.handlers import handle_control_plane_request
from topos.storage.canonical.conversations_tables import ensure_all_tables


@pytest.mark.asyncio
async def test_get_messages_conversation_requires_dataset_id(monkeypatch):
    conn = sqlite3.connect(":memory:")
    ensure_all_tables(conn)

    monkeypatch.setattr("topos.core.handlers.get_db_connection", lambda: conn)
    out = await handle_control_plane_request(
        {
            "id": "r1",
            "type": "get_messages",
            "payload": {"message_stream": "conversation", "limit": 5},
        }
    )
    assert out["status"] == "error"
    assert "dataset_id" in (out.get("error") or "").lower()


@pytest.mark.asyncio
async def test_get_messages_conversation_returns_rows(monkeypatch):
    conn = sqlite3.connect(":memory:")
    ensure_all_tables(conn)
    conn.execute(
        """
        INSERT INTO conversation_messages (
            message_id, conversation_id, dataset_id, sender_type, sender_id,
            event_at, source_id, content, is_from_self, metadata_json
        ) VALUES (
            'm1', 'c1', 'ds1', 'user', '+15550001',
            '2025-06-01T12:00:00+00:00', 'imessage', 'hello from imessage', 1, NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO conversation_messages (
            message_id, conversation_id, dataset_id, sender_type, sender_id,
            event_at, source_id, content, is_from_self, metadata_json
        ) VALUES (
            'm2', 'c1', 'ds1', 'user', '+15550002',
            '2025-06-01T11:00:00+00:00', 'signal', 'hello signal', 0, NULL
        )
        """
    )
    conn.commit()

    monkeypatch.setattr("topos.core.handlers.get_db_connection", lambda: conn)
    out = await handle_control_plane_request(
        {
            "id": "r2",
            "type": "get_messages",
            "payload": {
                "dataset_id": "ds1",
                "message_stream": "conversation",
                "limit": 10,
                "offset": 0,
            },
        }
    )
    assert out["status"] == "ok"
    body = out["payload"]
    assert body.get("message_stream") == "conversation"
    assert len(body["messages"]) == 2
    # Newest first
    assert body["messages"][0]["message_id"] == "m1"
    assert body["messages"][0]["source_id"] == "imessage"


@pytest.mark.asyncio
async def test_get_messages_conversation_filters_source_and_self(monkeypatch):
    conn = sqlite3.connect(":memory:")
    ensure_all_tables(conn)
    conn.execute(
        """
        INSERT INTO conversation_messages (
            message_id, conversation_id, dataset_id, sender_type, sender_id,
            event_at, source_id, content, is_from_self, metadata_json
        ) VALUES (
            'm1', 'c1', 'ds1', 'user', 'a',
            '2025-06-01T12:00:00+00:00', 'imessage', 'a', 1, NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO conversation_messages (
            message_id, conversation_id, dataset_id, sender_type, sender_id,
            event_at, source_id, content, is_from_self, metadata_json
        ) VALUES (
            'm2', 'c1', 'ds1', 'user', 'b',
            '2025-06-01T11:00:00+00:00', 'signal', 'b', 0, NULL
        )
        """
    )
    conn.commit()

    monkeypatch.setattr("topos.core.handlers.get_db_connection", lambda: conn)
    out = await handle_control_plane_request(
        {
            "id": "r3",
            "type": "get_messages",
            "payload": {
                "dataset_id": "ds1",
                "message_stream": "conversation",
                "source_id": "imessage",
                "is_from_self": True,
                "limit": 10,
            },
        }
    )
    assert out["status"] == "ok"
    msgs = out["payload"]["messages"]
    assert len(msgs) == 1
    assert msgs[0]["message_id"] == "m1"
