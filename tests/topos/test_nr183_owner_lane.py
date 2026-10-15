"""NR183 O1: the owner's own requests go ahead of recipients' when the node is busy (the owner's ruling D3).

With ``TOPOS_PERMISSIONS_V2_OWNER_LANE`` on, some of the control-plane client's inbound handler slots (and as many
waiting places) are kept for frames the control plane signed as the owner's own app; everyone else shares the rest.
Pinned here:

- whose frame it is: only a stamp that verifies, of class ``owner_app``, on a type no recipient door serves, that
  the dispatcher would not refuse as another user's. Everything else, and every error, is the shared class;
- the slots: an owner frame is handled while recipients fill the shared slots, recipients never take a kept slot,
  a freed slot goes to a waiting owner frame first, and every slot comes back, on an exception, a cancellation and a
  stop as on a reply;
- R3a's shedding is the same with the lane on, for both classes;
- with the switch off nothing of this is read: the one semaphore, in arrival order, as before.

Keys are made at run time; every id and text here is invented.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import topos.control_plane_client as control_plane_client
import topos.core.handlers as hub
from topos.control_plane_client import INBOUND_AGE_BOUND_SECONDS, ControlPlaneClient
from topos.permissions_v2 import answer_transport, owner_lane, search_transport, switches
from topos.permissions_v2.owner_lane import GENERAL, RESERVED, InboundSlots, is_owner_frame
from topos.relay_stamp import STAMP_FIELD, canonical_signing_payload

KEY, ANOTHER_KEY = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
OWNER, SOMEONE_ELSE = "owner-nr183", "someone-else-nr183"
SEARCH, BATCH = "permissions_v2_message_search", "permissions_v2_message_search_batch"
SUBMIT, FETCH = "permissions_v2_answer_submit", "permissions_v2_answer_fetch"
#: The recipient doors, written out by hand: the lane's own list must never be wider than "not these".
RECIPIENT_DOORS = (SEARCH, BATCH, SUBMIT, FETCH)
#: Owner types the control plane stamps owner_app today (agent gateway, owner write doors, sources, sharing setup).
OWNER_TYPES = ("query", "tools_retrieve", "upsert_home_chat_session", "store_message", "permissions_v2_mutate",
               "source_sync")
LANE, SLOTS = switches.OWNER_LANE.name, switches.OWNER_LANE_SLOTS.name


def stamped(message, *, cls="owner_app", acting=OWNER, key=KEY, iat=None, exp=None, signed_for=None):
    """The frame with a principal stamp; `signed_for` signs it for another frame (a stamp lifted onto this one)."""
    now = time.time()
    stamp = {"v": 1, "cls": cls, "client_id": "topos_home_chat", "acting_user": acting,
             "iat": now if iat is None else iat, "exp": now + 100 if exp is None else exp}
    target = signed_for or message
    stamp["sig"] = base64.b64encode(key.sign(canonical_signing_payload(
        stamp, msg_id=target["id"], msg_type=target["type"]))).decode("ascii")
    return {**message, STAMP_FIELD: stamp}


def frame(request_id, kind, **payload):
    return {"id": request_id, "type": kind, "payload": payload}


def owner_frame(request_id, kind="query", **how):
    return stamped(frame(request_id, kind, query="an invented question"), **how)


@pytest.fixture(autouse=True)
def pinned_key(monkeypatch):
    """The control plane's stamp key pinned, no sharing switch set, and an unbound node unless a test binds it."""
    monkeypatch.setenv("TOPOS_CP_STAMP_PUBKEY", base64.b64encode(KEY.public_key().public_bytes_raw()).decode("ascii"))
    for name in switches.BY_NAME:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(switches, "is_bound", lambda env=None: False)


@pytest.fixture
def bound(monkeypatch):
    """A node bound for sharing whose bound identity names OWNER."""
    monkeypatch.setattr(switches, "is_bound", lambda env=None: True)
    monkeypatch.setattr(hub, "_bound_owner_id", lambda: OWNER)


# --- whose frame it is -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", OWNER_TYPES)
def test_a_frame_the_control_plane_signed_as_the_owners_app_is_the_owners(kind):
    assert is_owner_frame(owner_frame("o-1", kind)) is True


@pytest.mark.parametrize("kind", OWNER_TYPES + RECIPIENT_DOORS + ("llm_generation",))
def test_a_frame_with_no_stamp_is_never_the_owners(kind):
    # The control plane never stamps a recipient's frame, and does not yet stamp every owner frame (llm_generation):
    # with nothing signed, nothing tells them apart, so neither is the owner's.
    assert is_owner_frame(frame("u-1", kind, user_id=OWNER)) is False


def test_a_stamp_that_does_not_verify_is_never_the_owners():
    assert is_owner_frame(owner_frame("o-1", key=ANOTHER_KEY)) is False                       # another key
    lifted = owner_frame("o-2", signed_for=frame("o-other", "query"))                          # lifted from a frame
    assert is_owner_frame(lifted) is False
    retyped = owner_frame("o-3", signed_for=frame("o-3", "store_message"))                     # signed for another type
    assert is_owner_frame(retyped) is False
    old = time.time() - 10_000
    assert is_owner_frame(owner_frame("o-4", iat=old, exp=old + 100)) is False                 # outside its window
    unsigned = owner_frame("o-5")
    unsigned[STAMP_FIELD] = {**unsigned[STAMP_FIELD], "sig": ""}
    assert is_owner_frame(unsigned) is False


def test_a_claimed_owner_class_on_a_stamp_signed_for_another_class_is_not_the_owners():
    message = stamped(frame("o-1", "query"), cls="third_party")
    message[STAMP_FIELD] = {**message[STAMP_FIELD], "cls": "owner_app"}   # the class edited, the signature kept
    assert is_owner_frame(message) is False


@pytest.mark.parametrize("cls", ["third_party", "owner_automation"])
def test_only_the_owner_app_class_is_the_owners(cls):
    # A routine's frames are the owner's, but nobody waits at the screen for them: they share the rest.
    assert is_owner_frame(owner_frame("o-1", cls=cls)) is False


@pytest.mark.parametrize("kind", RECIPIENT_DOORS + (" Permissions_V2_Message_Search ", "PERMISSIONS_V2_ANSWER_SUBMIT"))
def test_a_recipient_doors_type_is_never_the_owners_whatever_its_stamp(kind):
    assert is_owner_frame(owner_frame("r-1", kind)) is False


def test_with_no_key_pinned_nothing_is_the_owners(monkeypatch, tmp_path):
    monkeypatch.delenv("TOPOS_CP_STAMP_PUBKEY")
    from topos import relay_stamp
    monkeypatch.setattr(relay_stamp, "_PINNED_KEY_PATH", str(tmp_path / "no-such-key.pub"))
    assert is_owner_frame(owner_frame("o-1")) is False


def test_on_a_bound_node_a_stamp_naming_another_user_is_not_the_owners(bound):
    assert is_owner_frame(owner_frame("o-1", acting=OWNER)) is True
    assert is_owner_frame(owner_frame("o-2", acting=SOMEONE_ELSE)) is False
    # A stamp that names nobody: the frame's own identity fields are read, as the dispatcher reads them (rule (b)).
    assert is_owner_frame(stamped(frame("o-3", "query", user_id=OWNER), acting="")) is True
    assert is_owner_frame(stamped(frame("o-4", "query", user_id=SOMEONE_ELSE), acting="")) is False
    # The same frames on an unbound node: neither rule applies there, as in the dispatcher.


def test_on_a_bound_node_whose_identity_cannot_be_read_a_named_user_is_not_the_owners(bound, monkeypatch):
    monkeypatch.setattr(hub, "_bound_owner_id", lambda: None)
    assert is_owner_frame(owner_frame("o-1", acting=OWNER)) is False


def test_the_lane_and_the_dispatcher_agree_on_whose_frame_it_is(bound):
    """Every frame the lane calls the owner's is one the dispatcher would resolve as the owner's app and not refuse."""
    from topos.relay_stamp import check_relay_stamp
    frames = [owner_frame("o-1"), owner_frame("o-2", acting=SOMEONE_ELSE), stamped(frame("o-3", "query", user_id=SOMEONE_ELSE), acting=""),
              owner_frame("o-4", cls="third_party"), owner_frame("o-5", key=ANOTHER_KEY), frame("o-6", "query")]
    for message in frames:
        principal, _reason, _offset = check_relay_stamp(message)
        served_as_owner = bool(principal is not None and principal.cls == "owner_app"
                               and (bool(principal.acting_user) or hub._unstamped_naming_refusal(message) is None)
                               and hub._non_owner_relay_refusal(message, principal) is None)
        assert is_owner_frame(message) is served_as_owner, message["id"]


def test_anything_that_cannot_be_read_is_the_shared_class(monkeypatch):
    for message in (None, "a frame", ["query"], {"id": "o-1", "type": "query", STAMP_FIELD: "owner_app"},
                    {"id": "o-2", "type": "query", STAMP_FIELD: None}):
        assert is_owner_frame(message) is False
    import topos.relay_stamp as relay_stamp

    def broken(message):
        raise RuntimeError("an invented failure")
    monkeypatch.setattr(relay_stamp, "check_relay_stamp", broken)
    assert is_owner_frame(owner_frame("o-3")) is False
    assert InboundSlots.owner_frame(owner_frame("o-3")) is False
    monkeypatch.setattr(owner_lane, "is_owner_frame", lambda message: "yes")   # anything but a plain True
    assert InboundSlots.owner_frame(owner_frame("o-4")) is False


# --- the pool --------------------------------------------------------------------------------------------------------

def test_the_shared_class_always_keeps_a_slot():
    assert (InboundSlots(16, 4).general, InboundSlots(16, 4).reserved) == (12, 4)
    assert (InboundSlots(4, 9).general, InboundSlots(4, 9).reserved) == (1, 3)
    assert (InboundSlots(1, 1).general, InboundSlots(1, 1).reserved) == (1, 0)
    assert InboundSlots(16, 4).pending_limit(True, 128) == 128
    assert InboundSlots(16, 4).pending_limit(False, 128) == 124


@pytest.mark.asyncio
async def test_recipients_never_take_a_kept_slot_and_an_owner_takes_one_when_theirs_are_full():
    pool = InboundSlots(4, 1)
    held = [await pool.acquire(False) for _ in range(3)]
    assert held == [GENERAL] * 3
    waiting = asyncio.ensure_future(pool.acquire(False))
    await asyncio.sleep(0.01)
    assert not waiting.done() and pool.in_use() == {GENERAL: 3, RESERVED: 0}     # the kept slot stayed free
    assert await asyncio.wait_for(pool.acquire(True), 1) == RESERVED              # the owner's, at once
    pool.release(GENERAL)
    assert await asyncio.wait_for(waiting, 1) == GENERAL                          # the recipient's, when theirs frees
    assert pool.in_use() == {GENERAL: 3, RESERVED: 1}


@pytest.mark.asyncio
async def test_an_owner_takes_a_shared_slot_first_so_the_kept_one_stays_for_the_next():
    pool = InboundSlots(4, 1)
    assert await pool.acquire(True) == GENERAL
    assert pool.in_use() == {GENERAL: 1, RESERVED: 0}


@pytest.mark.asyncio
async def test_a_freed_slot_goes_to_a_waiting_owner_frame_first():
    pool = InboundSlots(2, 1)
    await pool.acquire(False)                       # the shared slot
    await pool.acquire(True)                        # the kept slot
    recipient = asyncio.ensure_future(pool.acquire(False))
    await asyncio.sleep(0.01)
    owner = asyncio.ensure_future(pool.acquire(True))   # came later
    await asyncio.sleep(0.01)
    pool.release(GENERAL)
    await asyncio.sleep(0.01)
    assert owner.done() and owner.result() == GENERAL and not recipient.done()
    pool.release(RESERVED)                          # a kept slot freed: the recipient may not take it
    await asyncio.sleep(0.01)
    assert not recipient.done() and pool.in_use() == {GENERAL: 1, RESERVED: 0}
    pool.release(GENERAL)
    assert await asyncio.wait_for(recipient, 1) == GENERAL


@pytest.mark.asyncio
async def test_a_frame_cancelled_while_it_waits_holds_no_slot():
    pool = InboundSlots(1, 0)
    await pool.acquire(False)
    waiter = asyncio.ensure_future(pool.acquire(False))
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert pool.waiting() == {"owner": 0, "general": 0}
    pool.release(GENERAL)
    assert pool.in_use() == {GENERAL: 0, RESERVED: 0}
    assert await asyncio.wait_for(pool.acquire(False), 1) == GENERAL


@pytest.mark.asyncio
async def test_a_frame_cancelled_just_after_it_was_handed_a_slot_gives_it_back():
    pool = InboundSlots(1, 0)
    await pool.acquire(False)
    waiter = asyncio.ensure_future(pool.acquire(False))
    await asyncio.sleep(0.01)
    pool.release(GENERAL)          # handed to the waiter, which has not run yet
    waiter.cancel()                # and cancelled before it does
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert pool.in_use() == {GENERAL: 0, RESERVED: 0}


def test_a_slot_is_never_given_back_twice():
    with pytest.raises(RuntimeError):
        InboundSlots(2, 1).release(GENERAL)


# --- through the client, the lane on ---------------------------------------------------------------------------------

class Socket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))


class Node:
    """A client with the lane on (`limit` slots, `reserved` kept), share doors that hold until released, and an app
    handler that records what it handled."""

    def __init__(self, monkeypatch, *, limit=3, reserved=1, lane=True, door_raises=False):
        self.handled, self.hold = [], asyncio.Event()

        async def app_handler(message):
            if message["type"] == "holds_the_slot":
                await self.hold.wait()
            if message["type"] == "raises":
                raise RuntimeError("an invented handler failure")
            self.handled.append(message["id"])
            return {"id": message["id"], "status": "ok"}

        async def door(ws, message):
            if door_raises:
                raise RuntimeError("an invented door failure")
            if message["id"].startswith("hold"):
                await self.hold.wait()
            self.handled.append(message["id"])
            await ws.send(json.dumps({"id": message["id"], "type": message["type"], "status": "ok"}))

        monkeypatch.setattr(search_transport, "dispatch_message_search", door)
        monkeypatch.setattr(search_transport, "dispatch_message_search_batch", door)
        monkeypatch.setattr(answer_transport, "dispatch_answer", door)
        if lane:
            monkeypatch.setenv(LANE, "true")
        self.client = ControlPlaneClient(control_plane_url="ws://example/ws/engine", api_key="an-invented-key",
                                         handler=app_handler, verify_ssl=False)
        if lane:
            assert self.client._owner_lane is not None
            self.client._owner_lane = self.pool = InboundSlots(limit, reserved)
        else:
            assert self.client._owner_lane is None
            self.client._inbound_semaphore = asyncio.Semaphore(limit)
        self.socket = Socket()
        self.client._ws = self.socket

    async def receive(self, message) -> None:
        await self.client._schedule_inbound_message(self.socket, message)
        await asyncio.sleep(0.01)

    async def settle(self) -> None:
        self.hold.set()
        for _ in range(200):
            if not self.client._inbound_tasks:
                break
            await asyncio.sleep(0.01)
        assert not self.client._inbound_tasks

    def replies(self) -> dict:
        return {reply["id"]: reply for reply in self.socket.sent}


@pytest.mark.asyncio
async def test_the_owners_frame_is_handled_while_recipients_fill_the_shared_slots(monkeypatch):
    node = Node(monkeypatch, limit=3, reserved=1)
    for number in range(4):                                          # two run, two wait
        await node.receive(stamped(frame(f"hold-{number}", BATCH), cls="third_party", acting=SOMEONE_ELSE))
    assert node.pool.in_use() == {GENERAL: 2, RESERVED: 0}
    await node.receive(owner_frame("owner-1"))
    assert node.handled == ["owner-1"]                               # at once, on the kept slot
    assert node.replies()["owner-1"]["status"] == "ok"
    await node.settle()
    assert sorted(node.handled) == ["hold-0", "hold-1", "hold-2", "hold-3", "owner-1"]
    assert node.pool.in_use() == {GENERAL: 0, RESERVED: 0}
    assert node.pool.granted[(True, RESERVED)] == 1 and node.pool.granted[(False, GENERAL)] == 4


@pytest.mark.asyncio
async def test_a_frame_that_only_claims_the_owner_waits_with_the_recipients(monkeypatch):
    node = Node(monkeypatch, limit=2, reserved=1)
    await node.receive(stamped(frame("hold-0", BATCH), cls="third_party", acting=SOMEONE_ELSE))
    for message in (owner_frame("forged", key=ANOTHER_KEY), frame("unstamped", "query", user_id=OWNER),
                    owner_frame("door", SEARCH)):                    # an owner stamp on a recipient door's type
        await node.receive(message)
    assert node.handled == [] and node.pool.in_use() == {GENERAL: 1, RESERVED: 0}
    assert node.pool.waiting() == {"owner": 0, "general": 3}
    await node.settle()
    assert node.pool.granted[(True, RESERVED)] == 0 and node.pool.granted[(True, GENERAL)] == 0


@pytest.mark.asyncio
async def test_recipients_never_hold_more_than_the_shared_slots(monkeypatch):
    node = Node(monkeypatch, limit=4, reserved=2)
    for number in range(10):
        kind = RECIPIENT_DOORS[number % 4]
        await node.receive(stamped(frame(f"hold-{number}", kind), cls="third_party", acting=SOMEONE_ELSE))
    assert node.pool.in_use() == {GENERAL: 2, RESERVED: 0}
    assert node.pool.waiting()["general"] == 8
    await node.settle()
    assert len(node.handled) == 10 and node.pool.granted.get((True, RESERVED)) == 0


@pytest.mark.asyncio
async def test_the_slot_comes_back_when_a_door_raises(monkeypatch):
    node = Node(monkeypatch, limit=2, reserved=1, door_raises=True)
    await node.receive(stamped(frame("r-1", BATCH), cls="third_party", acting=SOMEONE_ELSE))
    await node.settle()
    assert node.pool.in_use() == {GENERAL: 0, RESERVED: 0}


@pytest.mark.asyncio
async def test_the_slot_comes_back_when_the_app_handler_raises(monkeypatch):
    node = Node(monkeypatch, limit=1, reserved=0)
    await node.receive(owner_frame("o-1", "raises"))
    await node.settle()
    assert node.replies()["o-1"]["status"] == "error"
    assert node.pool.in_use() == {GENERAL: 0, RESERVED: 0}


@pytest.mark.asyncio
async def test_the_slot_comes_back_when_the_shed_check_raises(monkeypatch):
    node = Node(monkeypatch, limit=2, reserved=1)

    async def broken(*args):
        raise RuntimeError("an invented failure")
    monkeypatch.setattr(node.client, "_shed_at_dequeue", broken)
    await node.receive(owner_frame("o-1"))
    await node.receive(stamped(frame("r-1", SEARCH), cls="third_party", acting=SOMEONE_ELSE))
    await node.settle()
    assert node.pool.in_use() == {GENERAL: 0, RESERVED: 0}


@pytest.mark.asyncio
async def test_every_slot_comes_back_when_frames_are_cancelled_running_or_waiting(monkeypatch):
    node = Node(monkeypatch, limit=2, reserved=1)
    await node.receive(stamped(frame("hold-r1", BATCH), cls="third_party", acting=SOMEONE_ELSE))   # runs
    await node.receive(owner_frame("o-hold", "holds_the_slot"))                                   # runs, kept slot
    await node.receive(stamped(frame("hold-r2", BATCH), cls="third_party", acting=SOMEONE_ELSE))   # waits
    await node.receive(owner_frame("o-wait", "holds_the_slot"))                                   # waits
    assert node.pool.in_use() == {GENERAL: 1, RESERVED: 1}
    assert node.pool.waiting() == {"owner": 1, "general": 1}
    await node.client._cancel_inbound_tasks()          # what a stop does with what is left
    assert node.pool.in_use() == {GENERAL: 0, RESERVED: 0}
    assert node.pool.waiting() == {"owner": 0, "general": 0}
    assert node.handled == []
    await node.receive(owner_frame("o-after"))         # and the pool still serves
    assert node.handled == ["o-after"]


@pytest.mark.asyncio
async def test_the_owners_waiting_places_are_kept_too(monkeypatch):
    node = Node(monkeypatch, limit=2, reserved=1)
    node.client._inbound_max_pending = 4
    for number in range(4):
        await node.receive(stamped(frame(f"hold-{number}", BATCH), cls="third_party", acting=SOMEONE_ELSE))
    replies = node.replies()
    assert [request_id for request_id, reply in replies.items() if reply.get("error_code") == "engine_busy"] == ["hold-3"]
    await node.receive(owner_frame("owner-1"))
    assert node.handled == ["owner-1"]                 # the place and the slot the recipients could not take
    await node.settle()


# --- R3a with the lane on --------------------------------------------------------------------------------------------

def bounds(monkeypatch, seconds: float) -> None:
    monkeypatch.setattr(control_plane_client, "INBOUND_AGE_BOUND_SECONDS",
                        {kind: seconds for kind in INBOUND_AGE_BOUND_SECONDS})


@pytest.mark.asyncio
async def test_shedding_is_the_same_with_the_lane_on_for_both_classes(monkeypatch):
    bounds(monkeypatch, 0.1)
    node = Node(monkeypatch, limit=2, reserved=1)
    await node.receive(owner_frame("o-hold", "holds_the_slot"))              # the shared slot (owner takes it first)
    await node.receive(owner_frame("o-hold-2", "holds_the_slot"))            # the kept slot
    late = [stamped(frame(f"late-{kind}", kind), cls="third_party", acting=SOMEONE_ELSE) for kind in RECIPIENT_DOORS]
    late.append(owner_frame("late-owner-stamped-door", SEARCH))              # an owner stamp on a door: shed as theirs
    for message in late:
        await node.receive(message)
    await node.receive(owner_frame("o-late", "store_message"))               # the owner's own: never shed
    await asyncio.sleep(0.25)                                                # past the bound, all still waiting
    await node.settle()
    replies = node.replies()
    for message in late:
        assert replies[message["id"]]["error_code"] == "engine_busy", message["id"]
    assert sorted(node.handled) == ["o-hold", "o-hold-2", "o-late"]
    assert sum(node.client.inbound_shed_counts().values()) == 5


@pytest.mark.asyncio
async def test_inside_the_bound_both_classes_are_handled_with_the_lane_on(monkeypatch):
    node = Node(monkeypatch, limit=2, reserved=1)
    await node.receive(stamped(frame("r-1", SUBMIT), cls="third_party", acting=SOMEONE_ELSE))
    await node.receive(owner_frame("o-1"))
    await node.settle()
    assert sorted(node.handled) == ["o-1", "r-1"] and node.client.inbound_shed_counts() == {}


@pytest.mark.asyncio
async def test_a_reply_never_goes_to_a_newer_connection_with_the_lane_on(monkeypatch):
    node = Node(monkeypatch, limit=1, reserved=0)
    await node.receive(owner_frame("o-hold", "holds_the_slot"))
    await node.receive(stamped(frame("gone-1", SEARCH), cls="third_party", acting=SOMEONE_ELSE))
    node.client._ws = Socket()                                               # the socket was replaced meanwhile
    await node.settle()
    assert "gone-1" not in node.handled and "gone-1" not in node.replies()
    assert node.client.inbound_shed_counts() == {("gone", SEARCH): 1}


# --- the switch off: as before ---------------------------------------------------------------------------------------

def test_the_lane_is_off_unless_its_switch_is_on(monkeypatch):
    async def handler(message):
        return None

    def made():
        return ControlPlaneClient("ws://example/ws/engine", "an-invented-key", handler, False)._owner_lane
    assert made() is None
    monkeypatch.setattr(switches, "is_bound", lambda env=None: True)         # off on a bound node too
    assert made() is None
    monkeypatch.setenv(SLOTS, "2")                                            # the count alone turns nothing on
    assert made() is None
    monkeypatch.setenv(LANE, "false")
    assert made() is None
    monkeypatch.setenv(LANE, "maybe")                                         # unreadable is off
    assert made() is None
    monkeypatch.setenv(LANE, "on")
    lane = made()
    assert (lane.limit, lane.reserved, lane.general) == (16, 2, 14)
    monkeypatch.delenv(SLOTS)
    assert made().reserved == 4


@pytest.mark.asyncio
async def test_with_the_lane_off_nothing_of_it_is_read_and_the_semaphore_serves_in_arrival_order(monkeypatch):
    def never(message):
        raise AssertionError("the lane read a frame while off")
    monkeypatch.setattr(owner_lane, "is_owner_frame", never)
    monkeypatch.setattr(InboundSlots, "owner_frame", staticmethod(never))
    guarded = []
    original = ControlPlaneClient._handle_message_guarded

    async def recording(self, ws, data, arrived=None):
        guarded.append(data["id"])
        return await original(self, ws, data, arrived)
    monkeypatch.setattr(ControlPlaneClient, "_handle_message_guarded", recording)
    node = Node(monkeypatch, limit=1, lane=False)
    await node.receive(stamped(frame("hold-r1", BATCH), cls="third_party", acting=SOMEONE_ELSE))
    await node.receive(owner_frame("owner-1"))
    assert node.handled == []                          # the owner's frame waits behind the recipient's, as before
    await node.settle()
    assert node.handled == ["hold-r1", "owner-1"] and guarded == ["hold-r1", "owner-1"]


@pytest.mark.asyncio
async def test_with_the_lane_off_every_frame_meets_the_one_saturation_limit(monkeypatch):
    node = Node(monkeypatch, limit=1, lane=False)
    node.client._inbound_max_pending = 2
    await node.receive(stamped(frame("hold-0", BATCH), cls="third_party", acting=SOMEONE_ELSE))
    await node.receive(stamped(frame("hold-1", BATCH), cls="third_party", acting=SOMEONE_ELSE))
    await node.receive(owner_frame("owner-1"))
    assert node.replies()["owner-1"]["error_code"] == "engine_busy"
    await node.settle()
