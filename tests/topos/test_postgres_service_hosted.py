from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

import pytest
from fastapi import HTTPException

from topos.services import postgres as postgres_service
from topos.storage.db.postgres import ensure_postgres_schema


def _sqlite_connect_factory(db_path: str):
    @contextmanager
    def _connect():
        conn = sqlite3.connect(db_path)
        try:
            ensure_postgres_schema(conn)
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    return _connect


@pytest.mark.asyncio
async def test_hosted_store_and_get_messages_are_dataset_scoped(tmp_path, monkeypatch):
    db_path = str(tmp_path / "hosted.db")
    monkeypatch.setattr(postgres_service, "connect_postgres", _sqlite_connect_factory(db_path))
    monkeypatch.setattr(postgres_service.settings, "user_id", "tenant_a")
    svc = postgres_service.PostgresDbService()

    await svc.store_message(
        {
            "dataset_id": "tenant_a:default",
            "sender_type": "human",
            "content": "hello tenant a",
            "message_id": "msg-a1",
            "ts": "2026-04-22T10:00:00+00:00",
        }
    )
    await svc.store_message(
        {
            "dataset_id": "tenant_a:default",
            "sender_type": "assistant",
            "content": "reply tenant a",
            "message_id": "msg-a2",
            "ts": "2026-04-22T10:01:00+00:00",
        }
    )

    out = await svc.get_messages("tenant_a:default", limit=10, offset=0)
    assert out["status"] == "ok"
    assert out["dataset_id"] == "tenant_a:default"
    assert [row["message_id"] for row in out["messages"]] == ["msg-a2", "msg-a1"]
    assert all(row["dataset_id"] == "tenant_a:default" for row in out["messages"])


@pytest.mark.asyncio
async def test_hosted_cross_tenant_read_is_denied(tmp_path, monkeypatch):
    db_path = str(tmp_path / "hosted.db")
    monkeypatch.setattr(postgres_service, "connect_postgres", _sqlite_connect_factory(db_path))
    monkeypatch.setattr(postgres_service.settings, "user_id", "tenant_a")
    svc = postgres_service.PostgresDbService()

    with pytest.raises(HTTPException) as exc:
        await svc.get_messages("tenant_b:default", limit=10, offset=0)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "tenant_access_denied"


@pytest.mark.asyncio
async def test_hosted_backup_restore_respects_authenticated_tenant(tmp_path, monkeypatch):
    db_path = str(tmp_path / "hosted.db")
    monkeypatch.setattr(postgres_service, "connect_postgres", _sqlite_connect_factory(db_path))
    monkeypatch.setattr(postgres_service.settings, "user_id", "tenant_a")
    svc = postgres_service.PostgresDbService()

    await svc.store_message(
        {
            "dataset_id": "tenant_a:default",
            "sender_type": "human",
            "content": "visible",
            "message_id": "msg-a1",
            "ts": "2026-04-22T10:00:00+00:00",
        }
    )
    backup_payload = await svc.backup_database(encrypted=False)
    backup_json = json.loads(backup_payload.decode("utf-8"))
    backup_json["messages"].append(
        {
            "tenant_id": "tenant_b",
            "dataset_id": "tenant_b:default",
            "message_id": "msg-b1",
            "sender_type": "human",
            "content": "must-not-restore",
            "ts": "2026-04-22T11:00:00+00:00",
            "user_id": None,
        }
    )

    restore_result = await svc.restore_database(
        json.dumps(backup_json).encode("utf-8"),
        authenticated_user_id="tenant_a",
        encrypted=False,
    )
    assert restore_result["status"] == "ok"

    out = await svc.get_messages("tenant_a:default", limit=50, offset=0)
    assert [row["message_id"] for row in out["messages"]] == ["msg-a1"]


@pytest.mark.asyncio
async def test_hosted_replay_projection_reports_dataset_count(tmp_path, monkeypatch):
    db_path = str(tmp_path / "hosted.db")
    monkeypatch.setattr(postgres_service, "connect_postgres", _sqlite_connect_factory(db_path))
    monkeypatch.setattr(postgres_service.settings, "user_id", "tenant_a")
    svc = postgres_service.PostgresDbService()

    await svc.store_message(
        {
            "dataset_id": "tenant_a:default",
            "sender_type": "human",
            "content": "one",
            "message_id": "msg-1",
            "ts": "2026-04-22T10:00:00+00:00",
        }
    )
    await svc.store_message(
        {
            "dataset_id": "tenant_a:default",
            "sender_type": "human",
            "content": "two",
            "message_id": "msg-2",
            "ts": "2026-04-22T10:01:00+00:00",
        }
    )
    replay = await svc.replay_projection("tenant_a:default")
    assert replay["status"] == "ok"
    assert replay["replayed_messages"] == 2
