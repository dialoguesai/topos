"""Kept questions Wave 2 (lane OFF2N; ``lanes/OFF2_CONTRACT.md`` §D as revised after R-OFF2 F3): ``put_away_config``,
the owner's away settings as the control plane pushes them.

Pinned here:

- the payload is exactly ``{"v": 1, "seq": <int>, "while_using": "owner_first" | "even", "others_when_idle": bool}``;
  anything else (a missing or non-int ``seq`` included) is the node's usual error reply and nothing changes;
- a good one is held at once (no restart) and answered with what the node now holds; nothing is written to disk;
- the settings hold for one control-plane connection: a new connection (and one that ends) goes back to
  ``owner_first``, ``false``, and ``owner_idle_seconds`` back to null, until the next push;
- a push whose ``seq`` is lower than the one held is answered with what is held, and nothing changes; two pushes that
  arrive out of order end on the higher ``seq``;
- only the owner class reaches it: a recipient, a routine, an unstamped relay frame and a stamp that does not verify
  get the dispatcher's usual refusal, and nothing changes;
- the capability says 1.

Keys are made at run time; every id here is invented.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

import topos.core.handlers as hub
from topos.core.handlers import dispatch_relay_message
from topos.core.handlers.registry import OFF_LOOP_MESSAGE_TYPES, OWNER_ONLY_MESSAGE_TYPES
from topos.engine import registration
from topos.permissions_v2 import answer_gate, away_config, sharing_readiness
from tests.topos.test_nr183_owner_lane import ANOTHER_KEY, OWNER, SOMEONE_ELSE, frame, stamped
from tests.topos.test_nr183_owner_lane import pinned_key  # noqa: F401 -- the fixture: the stamp key pinned

PUT = "put_away_config"
EVEN = {"v": 1, "while_using": "even", "others_when_idle": True}
FIRST = {"v": 1, "while_using": "owner_first", "others_when_idle": False}


def push(held, seq):
    return {**held, "seq": seq}


@pytest.fixture(autouse=True)
def held_defaults(pinned_key):  # noqa: F811 -- the fixture above, asked for by name
    away_config.reset()
    yield
    away_config.reset()


def put(payload, request_id="away-1", **how):
    return asyncio.run(dispatch_relay_message(stamped({"id": request_id, "type": PUT, "payload": payload}, **how)))


def test_never_pushed_the_node_holds_owner_first_and_no_idleness():
    assert away_config.DEFAULT == FIRST == away_config.current()
    assert away_config.shares_evenly() is False and away_config.others_when_idle() is False


def test_a_good_push_is_held_at_once_answered_with_what_is_held_and_never_written(monkeypatch):
    def no_database():
        raise AssertionError("put_away_config touched the database")
    monkeypatch.setattr(hub, "get_db_connection", no_database)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: no_database())
    assert put(push(EVEN, 1)) == {"id": "away-1", "status": "ok", "payload": EVEN}
    assert away_config.current() == EVEN and away_config.shares_evenly() and away_config.others_when_idle()
    assert put(push(FIRST, 2))["payload"] == FIRST == away_config.current()


@pytest.mark.parametrize("payload", [
    None, [], "even", {}, EVEN, FIRST,                                # no seq at all
    push(EVEN, None), push(EVEN, "3"), push(EVEN, 3.0), push(EVEN, True),
    {"v": 1, "seq": 1, "while_using": "even"},
    {**push(EVEN, 1), "quiet_hours": None},                           # a key the contract does not name
    {**push(EVEN, 1), "v": 2}, {**push(EVEN, 1), "v": True}, {**push(EVEN, 1), "v": "1"},
    {**push(EVEN, 1), "while_using": "Even"}, {**push(EVEN, 1), "while_using": "me_first"},
    {**push(EVEN, 1), "others_when_idle": 1}, {**push(EVEN, 1), "others_when_idle": "true"},
])
def test_a_malformed_payload_is_the_usual_error_and_nothing_changes(payload):
    put(push(EVEN, 5))
    assert put(payload) == {"id": "away-1", "status": "error", "error": "away_config_invalid"}
    assert away_config.current() == EVEN
    assert put(push(FIRST, 5))["payload"] == FIRST                    # and the seq held is still 5


def test_a_lower_seq_is_answered_with_what_is_held_and_changes_nothing():
    put(push(EVEN, 20))
    assert put(push(FIRST, 19)) == {"id": "away-1", "status": "ok", "payload": EVEN}
    assert away_config.current() == EVEN
    assert put(push(FIRST, 20))["payload"] == FIRST                   # the same seq again is a push, not a stale one


def test_two_pushes_out_of_order_end_on_the_higher_seq():
    newer, older = push(FIRST, 1_761_300_000_200), push(EVEN, 1_761_300_000_100)
    assert put(newer)["payload"] == FIRST
    assert put(older)["payload"] == FIRST and away_config.current() == FIRST


def test_a_new_connection_goes_back_to_the_defaults_and_idleness_to_null():
    answer_gate.owner_frames_counted(True)
    put(push(EVEN, 30))
    assert sharing_readiness.build()["owner_idle_seconds"] is not None
    away_config.reset()                                               # what the client does at each connect and close
    assert away_config.current() == FIRST and sharing_readiness.build()["owner_idle_seconds"] is None
    assert put(push(EVEN, 1))["payload"] == EVEN                      # a new connection: any seq is newer


@pytest.mark.asyncio
async def test_the_client_resets_at_each_connect_and_each_close(monkeypatch):
    from topos import control_plane_client
    from tests.topos.test_control_plane_client import FakeConnect, FakeWebSocket

    applied = asyncio.Event()
    heard = []

    class OpenUntilApplied(FakeWebSocket):
        async def __anext__(self):
            if self._messages:
                return self._messages.pop(0)
            await asyncio.wait_for(applied.wait(), timeout=5)
            raise StopAsyncIteration

    away_config.apply(EVEN)                                           # what an earlier connection held
    stop = away_config.on_change(lambda: heard.append(away_config.current()))
    message = stamped({"id": "away-9", "type": PUT, "payload": push(EVEN, 7)})
    ws = OpenUntilApplied([json.dumps(message)])
    monkeypatch.setattr(control_plane_client, "connect", FakeConnect(ws))

    async def relay(frame_):
        reply = await dispatch_relay_message(frame_)
        applied.set()
        return reply
    client = control_plane_client.ControlPlaneClient("ws://example/ws/engine", "an-invented-key", relay, False)
    try:
        task = asyncio.create_task(client._run())
        await asyncio.wait_for(applied.wait(), timeout=5)
        await asyncio.sleep(0.05)
        client._stop.set()
        await asyncio.wait_for(task, timeout=5)
    finally:
        stop()
    assert heard[:3] == [FIRST, EVEN, FIRST]                          # connect: defaults; the push; the close
    assert away_config.current() == FIRST


@pytest.mark.parametrize("how", [
    {"cls": "third_party", "acting": SOMEONE_ELSE}, {"cls": "third_party", "acting": OWNER},
    {"cls": "owner_automation"}, {"key": ANOTHER_KEY},
], ids=["a recipient", "a third party naming the owner", "a routine", "a stamp that does not verify"])
def test_only_the_owner_class_reaches_it(how):
    reply = put(push(EVEN, 1), **how)
    assert reply["status"] == "error" and reply.get("code") == 403
    assert away_config.current() == away_config.DEFAULT


def test_an_unstamped_relay_frame_is_refused_too():
    reply = asyncio.run(dispatch_relay_message({"id": "away-2", "type": PUT, "payload": push(EVEN, 1)}))
    assert reply == {"id": "away-2", "status": "error", "code": 403, "error": "owner_mode_required"}
    assert away_config.current() == away_config.DEFAULT


def test_listeners_hear_every_change_and_one_that_fails_undoes_nothing():
    heard = []
    stop = away_config.on_change(lambda: heard.append(away_config.while_using()))
    away_config.on_change(lambda: 1 / 0)
    try:
        away_config.apply(EVEN)
        assert heard == ["even"] and away_config.current() == EVEN
    finally:
        stop()
        away_config._listeners.clear()
    away_config.apply(away_config.DEFAULT)
    assert heard == ["even"]


def test_it_is_owner_only_off_the_loop_and_the_capability_says_one():
    assert PUT in OWNER_ONLY_MESSAGE_TYPES and PUT in OFF_LOOP_MESSAGE_TYPES
    assert registration.build_engine_capabilities()["permissions_v2_away_config"] == 1 == away_config.CAPABILITY_VERSION
    snapshot = json.loads(open("topos/protocol/handled_message_types.json", encoding="utf-8").read())
    assert PUT in snapshot["handled_message_types"] and PUT in snapshot["owner_only_message_types"]


def test_two_concurrent_pushes_end_on_the_higher_seq_whatever_the_interleaving(monkeypatch):
    """R-OFF2 R2.1: the seq check and the hold are one step. The probe parks the older push's thread right after its
    first pass through the lock, lets the newer push run to the end, then lets the older one finish."""
    import threading

    parked, newer_done = threading.Event(), threading.Event()
    older_thread = []

    class ParkingLock:
        def __init__(self):
            self._lock, self._parked_once = threading.Lock(), False

        def __enter__(self):
            self._lock.acquire()
            return self

        def __exit__(self, *exc):
            self._lock.release()
            if threading.current_thread() in older_thread and not self._parked_once:
                self._parked_once = True
                parked.set()
                newer_done.wait(5)
            return False

    monkeypatch.setattr(away_config, "_lock", ParkingLock())
    older = threading.Thread(target=lambda: away_config.put(push(FIRST, 1)))
    older_thread.append(older)
    older.start()
    assert parked.wait(5)
    away_config.put(push(EVEN, 2))
    newer_done.set()
    older.join(5)
    assert away_config.current() == EVEN


@pytest.mark.asyncio
async def test_a_push_queued_on_a_connection_that_closes_is_never_applied_on_the_next(monkeypatch):
    """R-OFF2 R2.3: a push waiting for an inbound slot when its connection closes is dropped at dequeue ("gone"), as
    the share doors' frames are; it never lands on the next connection."""
    from tests.topos.test_nr183_owner_lane import BATCH, Node, Socket

    node = Node(monkeypatch, limit=2, reserved=1)
    real = node.client.handler

    async def relay(message):
        if message["type"] == PUT:
            return await dispatch_relay_message(message)
        return await real(message)
    node.client.handler = relay
    await node.receive(stamped(frame("hold-r1", BATCH), cls="third_party", acting=SOMEONE_ELSE))   # the shared slot
    await node.receive(stamped({"id": "away-q", "type": PUT, "payload": push(EVEN, 9)}))         # waits for it
    assert node.pool.waiting()["general"] == 1
    node.client._ws = node.socket = Socket()                          # the connection closed; a new one is up
    away_config.reset()
    await node.settle()
    assert away_config.current() == FIRST                              # the queued push was not applied
    assert "away-q" not in node.replies()
    assert node.client.inbound_shed_counts().get(("gone", PUT)) == 1
