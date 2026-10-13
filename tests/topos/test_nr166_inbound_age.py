"""NR166 R3a: a share-door request that waited past the control plane's patience is not handled.

The control plane waits a fixed time for the node's answer to a share search (25 s) or an own-AI ask (10 s), then
tells the asker to try again. A request still waiting for a handler slot after that has nobody left to answer, but
until now the node did the work anyway: a search held the write gate for seconds, an ask started a model job, and
each made the next request later still. On 13 Oct requests were served 28 to 77 s after they arrived.

At the moment a handler slot comes free, a request of one of the four share-door types that is older than its
type's bound is answered with the busy reply the queue already gives when it is full, and is not handled. One whose
connection closed while it waited is dropped: the control plane failed it when the socket closed, and a reply is
never sent on a newer connection.

Pinned here: aged requests are not handled and fresh ones are; every other type is handled however old (a consent
write, a protocol command, an owner's read); a reply never goes to another connection; inside the bound nothing
changes; the counts are right and the one log line names no request.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

import pytest

import topos.control_plane_client as control_plane_client
from topos.control_plane_client import INBOUND_AGE_BOUND_SECONDS, ControlPlaneClient
from topos.permissions_v2 import answer_transport, search_transport

SEARCH, BATCH = "permissions_v2_message_search", "permissions_v2_message_search_batch"
SUBMIT, FETCH = "permissions_v2_answer_submit", "permissions_v2_answer_fetch"
#: Types that must be applied or answered however long they waited: none of them is in the table.
NEVER_AGED = ("permissions_v2_mutate", "permissions_v2_bind", "permissions_v2_identity_command",
              "permissions_v2_ingest_snapshot", "permissions_v2_evidence_review_record",
              "permissions_v2_evidence_review_revoke", "permissions_v2_evidence_opt_out",
              "permissions_v2_evidence_opt_in", "permissions_v2_projection_review_record",
              "permissions_v2_projection_review_revoke", "permissions_v2_message_review",
              "permissions_v2_share_counts", "get_table_rows", "store_message", "query")
LINE = re.compile(r"control_plane_inbound_shed window_s=\d+ aged=(\d+) gone=(\d+) oldest_s=(\d+\.\d) "
                  r"aged_search=(\d+) aged_search_batch=(\d+) aged_answer_submit=(\d+) aged_answer_fetch=(\d+) "
                  r"gone_search=(\d+) gone_search_batch=(\d+) gone_answer_submit=(\d+) gone_answer_fetch=(\d+)")


class Socket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))


class Node:
    """A client with one handler slot, a share door that records what it was asked, and an app handler that can
    be held."""

    def __init__(self, monkeypatch, *, slots: int = 1):
        self.handled, self.hold = [], asyncio.Event()

        async def app_handler(message):
            if message["type"] == "holds_the_slot":
                await self.hold.wait()
            self.handled.append(message["id"])
            return {"id": message["id"], "status": "ok"}

        async def door(ws, message):
            self.handled.append(message["id"])
            await ws.send(json.dumps({"id": message["id"], "type": message["type"], "status": "ok"}))

        monkeypatch.setattr(search_transport, "dispatch_message_search", door)
        monkeypatch.setattr(search_transport, "dispatch_message_search_batch", door)
        monkeypatch.setattr(answer_transport, "dispatch_answer", door)
        self.client = ControlPlaneClient(control_plane_url="ws://example/ws/engine", api_key="an-invented-key",
                                         handler=app_handler, verify_ssl=False)
        self.client._inbound_semaphore = asyncio.Semaphore(slots)
        self.socket = self.connect()

    def connect(self) -> Socket:
        self.client._ws = Socket()
        return self.client._ws

    async def receive(self, request_id: str, kind: str, socket: Socket | None = None) -> None:
        await self.client._schedule_inbound_message(socket or self.socket, {"id": request_id, "type": kind})

    async def drain(self) -> None:
        self.hold.set()
        while self.client._inbound_tasks:
            await asyncio.sleep(0.01)


def bounds(monkeypatch, seconds: float) -> None:
    monkeypatch.setattr(control_plane_client, "INBOUND_AGE_BOUND_SECONDS",
                        {kind: seconds for kind in INBOUND_AGE_BOUND_SECONDS})


def test_the_table_is_the_four_share_doors_at_the_control_planes_budgets():
    # control_plane/config.py: permissions_beta_v2_search_relay_seconds 25, permissions_beta_v2_answer_relay_seconds 10.
    assert INBOUND_AGE_BOUND_SECONDS == {SEARCH: 25.0, BATCH: 25.0, SUBMIT: 10.0, FETCH: 10.0}
    assert not set(NEVER_AGED) & set(INBOUND_AGE_BOUND_SECONDS)
    assert not set(INBOUND_AGE_BOUND_SECONDS) & control_plane_client._FAST_INBOUND_MESSAGE_TYPES
    assert "ping" not in INBOUND_AGE_BOUND_SECONDS


@pytest.mark.asyncio
async def test_a_request_that_waited_past_its_bound_is_answered_busy_and_not_handled(monkeypatch):
    bounds(monkeypatch, 0.15)
    node = Node(monkeypatch)
    await node.receive("held", "holds_the_slot")
    for request_id, kind in (("s-1", SEARCH), ("b-1", BATCH), ("a-1", SUBMIT), ("f-1", FETCH)):
        await node.receive(request_id, kind)
    await asyncio.sleep(0.3)                      # all four wait for the slot, past the bound
    node.hold.set()
    await asyncio.sleep(0.05)
    await node.receive("s-2", SEARCH)             # arrives once the slot is free: handled
    await node.drain()
    assert node.handled == ["held", "s-2"]
    replies = {reply["id"]: reply for reply in node.socket.sent}
    for request_id, kind in (("s-1", SEARCH), ("b-1", BATCH), ("a-1", SUBMIT), ("f-1", FETCH)):
        assert replies[request_id] == {"id": request_id, "type": kind, "status": "error",
                                       "error": "Engine is busy. Retry shortly.", "error_code": "engine_busy",
                                       "retry_after_seconds": 30}
    assert replies["s-2"]["status"] == "ok"
    assert [reply["id"] for reply in node.socket.sent].count("s-1") == 1          # once
    assert node.client.inbound_shed_counts() == {
        ("aged", SEARCH): 1, ("aged", BATCH): 1, ("aged", SUBMIT): 1, ("aged", FETCH): 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", NEVER_AGED)
async def test_every_other_type_is_handled_however_long_it_waited(kind, monkeypatch):
    bounds(monkeypatch, 0.05)
    node = Node(monkeypatch)
    await node.receive("held", "holds_the_slot")
    await node.receive("late", kind)
    await asyncio.sleep(0.2)
    await node.drain()
    assert node.handled == ["held", "late"]
    assert {reply["id"]: reply["status"] for reply in node.socket.sent} == {"held": "ok", "late": "ok"}
    assert node.client.inbound_shed_counts() == {}


@pytest.mark.asyncio
async def test_inside_the_bound_nothing_changes(monkeypatch):
    node = Node(monkeypatch)                      # the real bounds: 25 s and 10 s
    await node.receive("held", "holds_the_slot")
    for request_id, kind in (("s-1", SEARCH), ("b-1", BATCH), ("a-1", SUBMIT), ("f-1", FETCH)):
        await node.receive(request_id, kind)
    await asyncio.sleep(0.1)
    await node.drain()
    assert node.handled == ["held", "s-1", "b-1", "a-1", "f-1"]
    assert all(reply["status"] == "ok" for reply in node.socket.sent) and len(node.socket.sent) == 5
    assert node.client.inbound_shed_counts() == {}


@pytest.mark.asyncio
async def test_a_request_whose_connection_closed_is_dropped_and_no_reply_goes_to_the_new_one(monkeypatch):
    node = Node(monkeypatch)                      # the real bounds: nothing here is old
    first = node.socket
    await node.receive("held", "holds_the_slot")
    await node.receive("s-1", SEARCH)
    await node.receive("a-1", SUBMIT)
    await node.receive("m-1", "permissions_v2_mutate")     # a protocol command: applied whatever its socket did
    second = node.connect()                       # the first connection is gone; the client has a new one
    await node.receive("s-2", SEARCH, second)
    await node.drain()
    assert node.handled == ["held", "m-1", "s-2"]
    assert [reply["id"] for reply in second.sent] == ["s-2"]                 # nothing of the first connection's
    assert sorted(reply["id"] for reply in first.sent) == ["held", "m-1"]    # as before: sent where it came from
    assert node.client.inbound_shed_counts() == {("gone", SEARCH): 1, ("gone", SUBMIT): 1}


@pytest.mark.asyncio
async def test_one_line_a_window_with_counts_by_type_and_no_request_named(monkeypatch, caplog):
    bounds(monkeypatch, 0.1)
    monkeypatch.setattr(control_plane_client, "INBOUND_SHED_LOG_SECONDS", 0.3)     # a minute, in the node
    node = Node(monkeypatch)

    def written() -> list:
        return [record.getMessage() for record in caplog.records
                if "control_plane_inbound_shed" in record.getMessage()]

    with caplog.at_level(logging.INFO, logger="topos.control_plane_client"):
        await node.receive("held", "holds_the_slot")
        await node.receive("an-id-no-line-may-carry", SEARCH)
        await node.receive("another-id", FETCH)
        await asyncio.sleep(0.25)
        await node.drain()                        # both are shed now; their window has just begun
        assert written() == []
        await node.receive("s-3", SEARCH)         # handled, inside the window
        await node.drain()
        await asyncio.sleep(0.45)
        [line] = written()                        # one line, when the window ended, for everything in it
        match = LINE.fullmatch(line)
        assert match, line
        aged, gone, oldest, *by_type = match.groups()
        assert (int(aged), int(gone)) == (2, 0) and float(oldest) >= 0.2
        assert [int(count) for count in by_type] == [1, 0, 0, 1, 0, 0, 0, 0]
        assert "an-id-no-line-may-carry" not in line and "another-id" not in line
        # Nothing shed since: no line, however long.
        await node.receive("s-4", SEARCH)
        await node.drain()
        await asyncio.sleep(0.45)
        assert len(written()) == 1
    assert node.handled == ["held", "s-3", "s-4"]
    assert node.client.inbound_shed_counts() == {("aged", SEARCH): 1, ("aged", FETCH): 1}       # the totals stay
