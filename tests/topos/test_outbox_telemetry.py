"""Presence/observation delivery is bounded best effort, separate from Offline Inbox."""
import asyncio

import pytest

from topos.control_plane_client import ControlPlaneClient

pytestmark = [pytest.mark.public, pytest.mark.asyncio]


def client():
    async def handler(message):
        return {"id": message["id"], "status": "ok"}

    return ControlPlaneClient(control_plane_url="ws://example/ws/engine", api_key="synthetic", handler=handler)


async def test_failed_flush_retains_entire_tail_and_retry_order(monkeypatch):
    node = client()
    for n in range(3):
        await node._enqueue_presence_message({"id": n})
    attempts = []

    async def send(ws, message):
        attempts.append(message["id"])
        return message["id"] != 1

    monkeypatch.setattr(node, "_send_ws_json", send)
    await node._flush_presence_outbox()
    assert attempts == [0, 1]
    assert [m["id"] for m in node._presence_outbox] == [1, 2]

    async def success(ws, message):
        attempts.append(message["id"])
        return True

    monkeypatch.setattr(node, "_send_ws_json", success)
    await node._flush_presence_outbox()
    assert attempts == [0, 1, 1, 2]
    assert not node._presence_outbox


async def test_concurrent_flushes_preserve_order(monkeypatch):
    node = client()
    for n in range(3):
        await node._enqueue_presence_message({"id": n})
    sent = []

    async def send(ws, message):
        await asyncio.sleep(0)
        sent.append(message["id"])
        return True

    monkeypatch.setattr(node, "_send_ws_json", send)
    await asyncio.gather(node._flush_presence_outbox(), node._flush_presence_outbox())
    assert sent == [0, 1, 2]


async def test_cancellation_restores_inflight_and_tail(monkeypatch):
    node = client()
    for n in range(2):
        await node._enqueue_presence_message({"id": n})
    started = asyncio.Event()

    async def send(ws, message):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(node, "_send_ws_json", send)
    task = asyncio.create_task(node._flush_presence_outbox())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [m["id"] for m in node._presence_outbox] == [0, 1]


async def test_capacity_still_prefers_newest_when_failed_send_returns(monkeypatch):
    node = client()
    node._presence_outbox_size = 2
    drops = []
    monkeypatch.setattr(node, "_record_outbox_drop", drops.append)
    await node._enqueue_presence_message({"id": "old"})

    async def send(ws, message):
        for n in range(2):
            await node._enqueue_presence_message({"id": n})
        return False

    monkeypatch.setattr(node, "_send_ws_json", send)
    await node._flush_presence_outbox()
    assert [m["id"] for m in node._presence_outbox] == [0, 1]
    assert drops == ["presence"]


async def test_bounded_disconnected_sync_queue_drops_oldest(monkeypatch):
    node = client()
    node._presence_outbox_size = 2
    drops = []
    monkeypatch.setattr(node, "_record_outbox_drop", drops.append)
    for n in range(3):
        node.enqueue_unsolicited_message_threadsafe({"id": n})
    assert [m["id"] for m in node._sync_outbox] == [1, 2]
    assert drops == ["sync"]
