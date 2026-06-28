"""Push SLA tests: fast ack, dedupe before enrichment, check_inbox_write, write_id lock."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from unittest.mock import AsyncMock, patch

import pytest

from topos.core import handlers
from topos.ingestion.inbox_drain import InboxDrain, run_inbox_app_ingest
from topos.ingestion.usage_inbox_dedupe import get_prior_delivery, record_delivery


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


async def test_fast_ack_before_slow_enrichment(sqlite_conn, monkeypatch) -> None:
    enrichment_started = asyncio.Event()
    enrichment_release = asyncio.Event()

    async def _slow_enrichment(_ctx: dict) -> dict:
        enrichment_started.set()
        await enrichment_release.wait()
        return {"status": "ok", "enrichment_jobs_run": 1}

    async def _fast_durable(**_kwargs) -> dict:
        return {
            "status": "ok",
            "records_processed": 1,
            "_enrichment_ctx": {"source_id": "chatgpt_ui_conversation", "sync_batch_id": "b1", "canonical_records": []},
        }

    monkeypatch.setattr("topos.ingestion.ingest_helpers.ingest_ui_payload", AsyncMock(side_effect=_fast_durable))
    monkeypatch.setattr("topos.ingestion.ingest_helpers.run_ui_payload_enrichment", _slow_enrichment)
    monkeypatch.setattr(handlers, "record_uma_request", lambda *args, **kwargs: None)

    message = {
        "id": "req-fast",
        "type": "app_ingest",
        "payload": {
            "write_id": "write-fast",
            "user_id": "owner-1",
            "dataset_id": "owner-1:default",
            "source_id": "chatgpt_ui_conversation",
            "schema_id": "chatgpt.conversation.v1",
            "records": [{"content": "entry"}],
            "resource_id": "dataset:owner-1:owner-1:default:dev",
        },
    }

    started = time.monotonic()
    result = await handlers.handle_control_plane_request(message)
    elapsed = time.monotonic() - started

    assert result["status"] == "ok"
    assert elapsed < 2.0
    assert get_prior_delivery("write-fast") == {"records_processed": 1, "records_total": 1}
    await asyncio.wait_for(enrichment_started.wait(), timeout=2.0)
    enrichment_release.set()


async def test_dedupe_before_enrichment_completes(sqlite_conn, monkeypatch) -> None:
    enrichment_release = asyncio.Event()

    async def _slow_enrichment(_ctx: dict) -> dict:
        await enrichment_release.wait()
        return {"status": "ok"}

    ingest_mock = AsyncMock(
        return_value={
            "status": "ok",
            "_enrichment_ctx": {"source_id": "chatgpt_ui_conversation", "sync_batch_id": "b1", "canonical_records": []},
        }
    )
    monkeypatch.setattr("topos.ingestion.ingest_helpers.ingest_ui_payload", ingest_mock)
    monkeypatch.setattr("topos.ingestion.ingest_helpers.run_ui_payload_enrichment", _slow_enrichment)
    monkeypatch.setattr(handlers, "record_uma_request", lambda *args, **kwargs: None)

    base_message = {
        "id": "req-dedupe",
        "type": "app_ingest",
        "payload": {
            "write_id": "write-race",
            "user_id": "owner-1",
            "dataset_id": "owner-1:default",
            "source_id": "chatgpt_ui_conversation",
            "schema_id": "chatgpt.conversation.v1",
            "records": [{"content": "entry"}],
            "resource_id": "dataset:owner-1:owner-1:default:dev",
        },
    }

    first = await handlers.handle_control_plane_request(dict(base_message))
    assert first["status"] == "ok"
    assert ingest_mock.await_count == 1

    second = await handlers.handle_control_plane_request({**base_message, "id": "req-dedupe-2"})
    assert second["status"] == "ok"
    assert second["payload"]["deduplicated"] is True
    assert ingest_mock.await_count == 1

    enrichment_release.set()


async def test_check_inbox_write_rpc(sqlite_conn) -> None:
    record_delivery("write-check", records_processed=2, records_total=3)

    missing = await handlers.handle_control_plane_request(
        {
            "id": "check-1",
            "type": "check_inbox_write",
            "payload": {"write_id": "missing-write"},
        }
    )
    assert missing["status"] == "ok"
    assert missing["payload"]["delivered"] is False

    found = await handlers.handle_control_plane_request(
        {
            "id": "check-2",
            "type": "check_inbox_write",
            "payload": {"write_id": "write-check"},
        }
    )
    assert found["status"] == "ok"
    assert found["payload"]["delivered"] is True
    assert found["payload"]["records_processed"] == 2
    assert found["payload"]["records_total"] == 3


async def test_same_write_id_serialized_by_drain() -> None:
    drain = InboxDrain(max_concurrent=10)
    active = 0
    peak = 0
    lock = asyncio.Lock()
    ingest_count = 0

    async def _ingest(write_id: str) -> str:
        nonlocal active, peak, ingest_count
        async with lock:
            active += 1
            peak = max(peak, active)
            ingest_count += 1
        await asyncio.sleep(0.05)
        async with lock:
            active -= 1
        return write_id

    await asyncio.gather(
        drain.run(lambda: _ingest("same-write"), write_id="same-write"),
        drain.run(lambda: _ingest("same-write"), write_id="same-write"),
    )
    assert ingest_count == 2
    assert peak == 1


async def test_different_write_ids_run_concurrently() -> None:
    drain = InboxDrain(max_concurrent=10)
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def _work() -> int:
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.05)
        async with lock:
            active -= 1
        return 1

    results = await asyncio.gather(
        drain.run(_work, write_id="w1"),
        drain.run(_work, write_id="w2"),
    )
    assert sum(results) == 2
    assert peak >= 2


async def test_run_inbox_app_ingest_passes_write_id(monkeypatch) -> None:
    seen: list[str | None] = []

    async def _fake_run(coro_factory, *, write_id=None):
        seen.append(write_id)
        return await coro_factory()

    monkeypatch.setattr("topos.ingestion.inbox_drain._inbox_drain.run", _fake_run)

    async def _body() -> dict:
        return {"status": "ok"}

    await run_inbox_app_ingest(_body, write_id="abc")
    assert seen == ["abc"]
