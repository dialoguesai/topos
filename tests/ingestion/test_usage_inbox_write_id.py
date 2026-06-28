"""Engine tests for Usage Inbox write_id ack and dedupe."""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock, patch

import pytest

from topos.ingestion.usage_inbox_dedupe import get_prior_delivery, record_delivery
from topos.core import handlers


pytestmark = pytest.mark.asyncio


@pytest.fixture
def sqlite_conn(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    monkeypatch.setattr(
        "topos.ingestion.usage_inbox_dedupe.get_db_connection",
        lambda: conn,
    )
    monkeypatch.setattr(
        "topos.core.handlers.get_db_connection",
        lambda: conn,
    )
    yield conn
    conn.close()


async def test_write_id_in_success_response(sqlite_conn, monkeypatch) -> None:
    monkeypatch.setattr(
        "topos.ingestion.ingest_helpers.ingest_ui_payload",
        AsyncMock(return_value={"status": "ok"}),
    )
    monkeypatch.setattr(handlers, "record_uma_request", lambda *args, **kwargs: None)

    message = {
        "id": "req-1",
        "type": "app_ingest",
        "payload": {
            "write_id": "write-abc",
            "user_id": "owner-1",
            "dataset_id": "owner-1:default",
            "source_id": "chatgpt_ui_conversation",
            "schema_id": "chatgpt.conversation.v1",
            "records": [{"content": "hello"}],
            "resource_id": "dataset:owner-1:owner-1:default:dev",
        },
    }
    result = await handlers.handle_control_plane_request(message)
    assert result["status"] == "ok"
    assert result["payload"]["write_id"] == "write-abc"
    assert result["payload"]["records_processed"] == 1


async def test_write_id_dedupe_skips_reingest(sqlite_conn, monkeypatch) -> None:
    record_delivery("write-dup", records_processed=2, records_total=2)
    assert get_prior_delivery("write-dup") == {"records_processed": 2, "records_total": 2}

    ingest_mock = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr("topos.ingestion.ingest_helpers.ingest_ui_payload", ingest_mock)

    message = {
        "id": "req-2",
        "type": "app_ingest",
        "payload": {
            "write_id": "write-dup",
            "user_id": "owner-1",
            "dataset_id": "owner-1:default",
            "source_id": "chatgpt_ui_conversation",
            "schema_id": "chatgpt.conversation.v1",
            "records": [{"content": "hello"}],
            "resource_id": "dataset:owner-1:owner-1:default:dev",
        },
    }
    result = await handlers.handle_control_plane_request(message)
    assert result["status"] == "ok"
    assert result["payload"]["deduplicated"] is True
    ingest_mock.assert_not_awaited()
