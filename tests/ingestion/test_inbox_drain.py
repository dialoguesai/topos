"""Tests for Usage Inbox engine drain queue."""

from __future__ import annotations

import asyncio

import pytest

from topos.ingestion.inbox_drain import InboxDrain, run_inbox_app_ingest

pytestmark = pytest.mark.asyncio


async def test_inbox_drain_limits_concurrency() -> None:
    drain = InboxDrain(max_concurrent=2)
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def _work(delay: float) -> int:
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(delay)
        async with lock:
            active -= 1
        return 1

    results = await asyncio.gather(
        *[drain.run(lambda d=d: _work(0.05)) for d in range(6)]
    )
    assert sum(results) == 6
    assert peak <= 2


async def test_run_inbox_app_ingest_returns_result() -> None:
    async def _body() -> dict:
        return {"status": "ok", "payload": {"records_processed": 1}}

    result = await run_inbox_app_ingest(_body, write_id="write-1")
    assert result["status"] == "ok"


async def test_same_write_id_requests_serialize() -> None:
    order: list[int] = []

    async def _body(n: int) -> dict:
        order.append(n)
        await asyncio.sleep(0.02)
        return {"n": n}

    async def _run(n: int) -> dict:
        return await run_inbox_app_ingest(lambda n=n: _body(n), write_id="same-id")

    await asyncio.gather(_run(1), _run(2))
    assert order == [1, 2]
