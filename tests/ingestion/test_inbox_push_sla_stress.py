"""Engine stress tests for Usage Inbox Push SLA."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from unittest.mock import AsyncMock, patch

import pytest

from topos.core import handlers
from topos.ingestion.inbox_drain import InboxDrain
from topos.ingestion.usage_inbox_dedupe import get_prior_delivery


pytestmark = pytest.mark.asyncio


@pytest.fixture
def sqlite_conn(tmp_path, monkeypatch):
    db_path = tmp_path / "stress.db"
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


def _app_ingest_message(write_id: str, *, req_id: str = "req-1") -> dict:
    return {
        "id": req_id,
        "type": "app_ingest",
        "payload": {
            "write_id": write_id,
            "user_id": "owner-1",
            "dataset_id": "owner-1:default",
            "source_id": "chatgpt_ui_conversation",
            "schema_id": "chatgpt.conversation.v1",
            "records": [{"content": "stress"}],
            "resource_id": "dataset:owner-1:owner-1:default:dev",
        },
    }


async def test_stress_ten_concurrent_distinct_write_ids(sqlite_conn, monkeypatch) -> None:
    monkeypatch.setattr(
        "topos.ingestion.ingest_helpers.ingest_ui_payload",
        AsyncMock(
            return_value={
                "status": "ok",
                "_enrichment_ctx": {
                    "source_id": "chatgpt_ui_conversation",
                    "sync_batch_id": "b1",
                    "canonical_records": [],
                },
            }
        ),
    )
    monkeypatch.setattr(
        "topos.ingestion.ingest_helpers.run_ui_payload_enrichment",
        AsyncMock(return_value={"status": "ok"}),
    )
    monkeypatch.setattr(handlers, "record_uma_request", lambda *args, **kwargs: None)

    async def _one(wid: str) -> dict:
        return await handlers.handle_control_plane_request(_app_ingest_message(wid, req_id=wid))

    results = await asyncio.gather(*[_one(f"write-{i}") for i in range(10)])
    assert all(r["status"] == "ok" for r in results)
    assert len({f"write-{i}" for i in range(10) if get_prior_delivery(f"write-{i}")}) == 10


async def test_stress_concurrent_same_write_id_one_dedupe(sqlite_conn, monkeypatch) -> None:
    ingest_mock = AsyncMock(
        side_effect=lambda **kwargs: {
            "status": "ok",
            "_enrichment_ctx": {
                "source_id": "chatgpt_ui_conversation",
                "sync_batch_id": "b1",
                "canonical_records": [],
            },
        }
    )
    monkeypatch.setattr("topos.ingestion.ingest_helpers.ingest_ui_payload", ingest_mock)
    monkeypatch.setattr(
        "topos.ingestion.ingest_helpers.run_ui_payload_enrichment",
        AsyncMock(return_value={"status": "ok"}),
    )
    monkeypatch.setattr(handlers, "record_uma_request", lambda *args, **kwargs: None)

    async def _one(req_id: str) -> dict:
        return await handlers.handle_control_plane_request(_app_ingest_message("write-same", req_id=req_id))

    results = await asyncio.gather(*[_one(f"req-{i}") for i in range(5)])
    ok_count = sum(1 for r in results if r["status"] == "ok")
    dedupe_count = sum(1 for r in results if r.get("payload", {}).get("deduplicated"))
    assert ok_count == 5
    assert dedupe_count >= 4
    assert ingest_mock.await_count == 1


async def test_stress_fast_ack_under_load(sqlite_conn, monkeypatch) -> None:
    enrichment_release = asyncio.Event()

    async def _slow_enrichment(_ctx: dict) -> dict:
        await enrichment_release.wait()
        return {"status": "ok"}

    monkeypatch.setattr(
        "topos.ingestion.ingest_helpers.ingest_ui_payload",
        AsyncMock(
            return_value={
                "status": "ok",
                "_enrichment_ctx": {"source_id": "chatgpt_ui_conversation", "sync_batch_id": "b", "canonical_records": []},
            }
        ),
    )
    monkeypatch.setattr("topos.ingestion.ingest_helpers.run_ui_payload_enrichment", _slow_enrichment)
    monkeypatch.setattr(handlers, "record_uma_request", lambda *args, **kwargs: None)

    latencies: list[float] = []

    async def _timed(wid: str) -> None:
        start = time.monotonic()
        result = await handlers.handle_control_plane_request(_app_ingest_message(wid, req_id=wid))
        latencies.append(time.monotonic() - start)
        assert result["status"] == "ok"

    await asyncio.gather(*[_timed(f"fast-{i}") for i in range(8)])
    assert all(lat < 1.0 for lat in latencies)
    enrichment_release.set()


async def test_stress_drain_lock_serializes_same_write_id() -> None:
    drain = InboxDrain(max_concurrent=10)
    order: list[int] = []

    async def _work(n: int) -> int:
        order.append(n)
        await asyncio.sleep(0.02)
        return n

    await asyncio.gather(
        *[drain.run(lambda n=n: _work(n), write_id="lock-me") for n in range(8)]
    )
    assert order == list(range(8))
