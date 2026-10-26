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
from topos.permissions_v2.owner_lane import BORROWED, GENERAL, RESERVED, InboundSlots, is_owner_frame
from topos.relay_stamp import STAMP_FIELD, canonical_signing_payload

KEY, ANOTHER_KEY = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
OWNER, SOMEONE_ELSE = "owner-nr183", "someone-else-nr183"
SEARCH, BATCH = "permissions_v2_message_search", "permissions_v2_message_search_batch"
SUBMIT, FETCH = "permissions_v2_answer_submit", "permissions_v2_answer_fetch"
#: The recipient doors, written out by hand: the lane's own list must never be wider than "not these".
RECIPIENT_DOORS = (SEARCH, BATCH, SUBMIT, FETCH)
#: Interactive owner types the control plane stamps owner_app today (the agent gateway's home-chat forwards, the
#: explorer, the home chat's store), written out by hand: the lane takes these.
OWNER_TYPES = ("query", "aggregate", "tools_retrieve", "get_table_rows", "upsert_home_chat_session", "home_chat_memory")
#: The owner's background work, also stamped owner_app: never the lane's (it shares the slots in arrival order).
BACKGROUND_OWNER_TYPES = ("app_ingest", "source_sync", "signal_upload", "ingestion_reprocess", "start_ingestion",
                          "store_message", "permissions_v2_mutate", "put_user_identity")
LANE, SLOTS = switches.OWNER_LANE.name, switches.OWNER_LANE_SLOTS.name
SHARED_MAX, TYPES = switches.OWNER_LANE_SHARED_MAX.name, switches.OWNER_LANE_TYPES.name


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


@pytest.mark.parametrize("kind", BACKGROUND_OWNER_TYPES)
def test_the_owners_background_work_is_never_the_lanes(kind):
    """Strict owner-first under a steady background flow (a capture app's ingest, a source sync) gave every freed slot
    to owner frames and none to recipients (WS0's review): only interactive types take the lane."""
    assert is_owner_frame(owner_frame("b-1", kind)) is False
    assert InboundSlots(16, 4).owner_frame(owner_frame("b-2", kind)) is False


def test_the_interactive_list_is_the_switchs_and_can_never_name_a_recipient_door():
    assert set(switches.OWNER_LANE_TYPES.bound) >= set(OWNER_TYPES) and not set(switches.OWNER_LANE_TYPES.bound) & set(
        BACKGROUND_OWNER_TYPES + RECIPIENT_DOORS)
    narrow = InboundSlots(16, 4, types=("query",))
    assert narrow.owner_frame(owner_frame("o-1", "query")) is True
    assert narrow.owner_frame(owner_frame("o-2", "aggregate")) is False
    wide = InboundSlots(16, 4, types=("source_sync", SEARCH, " Query "))
    assert wide.owner_frame(owner_frame("o-3", "source_sync")) is True
    assert wide.owner_frame(owner_frame("o-4", "query")) is True              # the list is read folded too
    assert wide.owner_frame(owner_frame("o-5", SEARCH)) is False              # never a recipient door, whatever it says
    assert InboundSlots(16, 4, types=()).owner_frame(owner_frame("o-6")) is False
    assert is_owner_frame(owner_frame("o-7", " QUERY ")) is True              # a type is read as the dispatcher reads it


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


@pytest.mark.parametrize("cls", ["owner_agent", "owner", "OWNER_APP", "owner_app ", "", "cp_relay", "internal"])
def test_a_class_this_lane_does_not_name_is_the_shared_class(cls):
    """Only `owner_app` is named (`owner_lane.OWNER_LANE_CLASSES`); every other class, one this node does not know
    included, gets the recipients' treatment."""
    assert owner_lane.OWNER_LANE_CLASSES == frozenset({"owner_app"})
    assert is_owner_frame(owner_frame("o-1", cls=cls)) is False


def test_owner_agent_is_the_shared_class_even_where_a_relay_stamp_could_carry_it(monkeypatch):
    # `owner_agent` is minted on the node's unix socket, never on a relayed frame; were the relay ever to carry it,
    # it would still wait with the recipients until the lane's one line names it.
    import topos.relay_stamp as relay_stamp
    monkeypatch.setattr(relay_stamp, "ALLOWED_CLASSES", relay_stamp.ALLOWED_CLASSES | {"owner_agent"})
    message = owner_frame("o-1", cls="owner_agent")
    principal, reason, _offset = relay_stamp.check_relay_stamp(message)
    assert reason == relay_stamp.VERIFIED and principal.cls == "owner_agent"
    assert is_owner_frame(message) is False


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
    assert InboundSlots(16, 4).owner_frame(owner_frame("o-3")) is False
    monkeypatch.setattr(owner_lane, "is_owner_frame", lambda message, types=None: "yes")   # anything but a plain True
    assert InboundSlots(16, 4).owner_frame(owner_frame("o-4")) is False


# --- the pool --------------------------------------------------------------------------------------------------------

def test_the_shared_class_always_keeps_a_slot():
    assert (InboundSlots(16, 4).general, InboundSlots(16, 4).reserved) == (12, 4)
    assert (InboundSlots(4, 9).general, InboundSlots(4, 9).reserved) == (1, 3)
    assert (InboundSlots(1, 1).general, InboundSlots(1, 1).reserved) == (1, 0)
    assert InboundSlots(16, 4).shared_cap == 2 and InboundSlots(16, 4, shared_cap=40).shared_cap == 11
    assert InboundSlots(4, 1, shared_cap=0).shared_cap == 0 and InboundSlots(3, 2, shared_cap=5).shared_cap == 0


@pytest.mark.asyncio
async def test_with_no_slot_kept_an_owner_frame_takes_the_shared_one_as_anyone_does():
    pool = InboundSlots(1, 1)                       # one slot: nothing can be kept
    assert await asyncio.wait_for(pool.acquire(True), 1) == BORROWED
    waiting = asyncio.ensure_future(pool.acquire(False))
    await asyncio.sleep(0.01)
    pool.release(BORROWED)
    assert await asyncio.wait_for(waiting, 1) == GENERAL


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
async def test_an_owner_takes_its_kept_slots_first_then_at_most_the_cap_of_shared_ones():
    """DR-04: up to the kept slots, the owner's frames take nothing from recipients; past them, at most the cap."""
    pool = InboundSlots(8, 2, shared_cap=2)
    assert [await pool.acquire(True) for _ in range(2)] == [RESERVED, RESERVED]
    assert pool.in_use() == {GENERAL: 0, RESERVED: 2}
    assert [await pool.acquire(True) for _ in range(2)] == [BORROWED, BORROWED]
    assert pool.in_use() == {GENERAL: 2, RESERVED: 2} and pool.borrowed() == 2
    over = asyncio.ensure_future(pool.acquire(True))
    await asyncio.sleep(0.01)
    assert not over.done()                          # shared slots are free, but the cap is reached
    assert [await pool.acquire(False) for _ in range(4)] == [GENERAL] * 4   # recipients still run on the other four
    pool.release(BORROWED)
    assert await asyncio.wait_for(over, 1) == BORROWED


@pytest.mark.asyncio
async def test_a_freed_slot_goes_to_a_waiting_owner_frame_first():
    pool = InboundSlots(3, 1, shared_cap=1)
    await pool.acquire(False)                       # the two shared slots
    await pool.acquire(False)
    await pool.acquire(True)                        # the kept slot
    recipient = asyncio.ensure_future(pool.acquire(False))
    await asyncio.sleep(0.01)
    owner = asyncio.ensure_future(pool.acquire(True))   # came later
    await asyncio.sleep(0.01)
    pool.release(GENERAL)
    await asyncio.sleep(0.01)
    assert owner.done() and owner.result() == BORROWED and not recipient.done()
    pool.release(RESERVED)                          # a kept slot freed: the recipient may not take it
    await asyncio.sleep(0.01)
    assert not recipient.done() and pool.in_use() == {GENERAL: 2, RESERVED: 0}
    pool.release(BORROWED)
    assert await asyncio.wait_for(recipient, 1) == GENERAL


@pytest.mark.asyncio
async def test_recipients_keep_progress_under_a_steady_owner_flow():
    """WS0's review: with an owner frame always waiting at every release, strict owner-first served 200 owner frames
    and 0 of 5 recipients. Owner frames now hold at most the cap of shared slots: recipients keep the rest."""
    pool = InboundSlots(16, 4, shared_cap=2)
    held = [await pool.acquire(False) for _ in range(12)] + [await pool.acquire(True) for _ in range(4)]
    recipients = [asyncio.ensure_future(pool.acquire(False)) for _ in range(5)]
    await asyncio.sleep(0)
    most_borrowed = 0
    for _ in range(200):
        owner = asyncio.ensure_future(pool.acquire(True))
        await asyncio.sleep(0)
        pool.release(held.pop(0))
        await asyncio.sleep(0)
        if owner.done():
            held.append(owner.result())
        else:
            owner.cancel()
        most_borrowed = max(most_borrowed, pool.borrowed())
        if all(r.done() for r in recipients):
            break
    assert all(r.done() for r in recipients) and most_borrowed <= 2


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
    with pytest.raises(RuntimeError):
        InboundSlots(4, 1).release(BORROWED)


# --- through the client, the lane on ---------------------------------------------------------------------------------

class Socket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))


class Node:
    """A client with the lane on (`limit` slots, `reserved` kept), share doors that hold until released, and an app
    handler that records what it handled."""

    def __init__(self, monkeypatch, *, limit=3, reserved=1, lane=True, door_raises=False, shared_cap=2):
        self.handled, self.hold, self.holds = [], asyncio.Event(), {}

        async def app_handler(message):
            if message["type"] == "holds_the_slot" or message["id"].startswith("hold"):
                await self.held(message["id"])
            if message["type"] == "raises" or message["id"].startswith("raise"):
                raise RuntimeError("an invented handler failure")
            self.handled.append(message["id"])
            return {"id": message["id"], "status": "ok"}

        async def door(ws, message):
            if door_raises:
                raise RuntimeError("an invented door failure")
            if message["id"].startswith("hold"):
                await self.held(message["id"])
            self.handled.append(message["id"])
            await ws.send(json.dumps({"id": message["id"], "type": message["type"], "status": "ok"}))

        monkeypatch.setattr(search_transport, "dispatch_message_search", door)
        monkeypatch.setattr(search_transport, "dispatch_message_search_batch", door)
        monkeypatch.setattr(answer_transport, "dispatch_answer", door)
        monkeypatch.setenv(LANE, "true" if lane else "false")   # on by default since Wave 2 (D-10): off is one line
        self.client = ControlPlaneClient(control_plane_url="ws://example/ws/engine", api_key="an-invented-key",
                                         handler=app_handler, verify_ssl=False)
        if lane:
            assert self.client._owner_lane is not None
            self.client._owner_lane = self.pool = InboundSlots(limit, reserved, shared_cap=shared_cap)
        else:
            assert self.client._owner_lane is None
            self.client._inbound_semaphore = asyncio.Semaphore(limit)
        self.socket = Socket()
        self.client._ws = self.socket

    async def held(self, request_id) -> None:
        """Until `finish(request_id)` or `settle()`."""
        event = self.holds.setdefault(request_id, asyncio.Event())
        if not self.hold.is_set():
            await event.wait()

    def finish(self, request_id) -> None:
        self.holds.setdefault(request_id, asyncio.Event()).set()

    async def receive(self, message) -> None:
        await self.client._schedule_inbound_message(self.socket, message)
        await asyncio.sleep(0.01)

    async def settle(self) -> None:
        self.hold.set()
        for event in self.holds.values():
            event.set()
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
    await node.receive(owner_frame("raise-o1"))
    await node.settle()
    assert node.replies()["raise-o1"]["status"] == "error"
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
    await node.receive(owner_frame("hold-o1"))                                                    # runs, kept slot
    await node.receive(stamped(frame("hold-r2", BATCH), cls="third_party", acting=SOMEONE_ELSE))   # waits
    await node.receive(owner_frame("hold-o2"))                                                    # waits
    assert node.pool.in_use() == {GENERAL: 1, RESERVED: 1}
    assert node.pool.waiting() == {"owner": 1, "general": 1}
    await node.client._cancel_inbound_tasks()          # what a stop does with what is left
    assert node.pool.in_use() == {GENERAL: 0, RESERVED: 0}
    assert node.pool.waiting() == {"owner": 0, "general": 0}
    assert node.handled == []
    await node.receive(owner_frame("o-after"))         # and the pool still serves
    assert node.handled == ["o-after"]


def recipient(request_id):
    return stamped(frame(request_id, BATCH), cls="third_party", acting=SOMEONE_ELSE)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_frames", [0, 4, 6, 40])
async def test_dr04_recipients_are_admitted_and_refused_exactly_as_with_no_owner_traffic(monkeypatch, owner_frames):
    """DR-04 (the owner, 15 Oct): whether a recipient gets an answer must not depend on the node's other activity. The
    recipients' waiting places (128 less the 4 kept slots) count recipients' frames only: at the boundary, with the
    owner idle, within its kept slots, past them, or flooding past its own places, the same recipient frames are
    admitted and the same one is refused, and after one finishes the next is admitted. The owner's own places (32)
    are the owner's only matter."""
    node = Node(monkeypatch, limit=16, reserved=4)
    assert node.client._inbound_max_pending == 128
    schedule = node.client._schedule_inbound_message
    for number in range(owner_frames):
        await schedule(node.socket, owner_frame(f"hold-o{number}"))
    for number in range(125):
        await schedule(node.socket, recipient(f"hold-r{number}"))
    await asyncio.sleep(0.01)
    busy = {request_id for request_id, reply in node.replies().items() if reply.get("error_code") == "engine_busy"}
    assert {request_id for request_id in busy if request_id.startswith("hold-r")} == {"hold-r124"}
    assert {request_id for request_id in busy if request_id.startswith("hold-o")} == {
        f"hold-o{number}" for number in range(32, owner_frames)}
    assert node.pool.in_hand() == {"owner": min(owner_frames, 32), "general": 124}
    node.finish("hold-r0")                                         # one recipient frame finishes
    await asyncio.sleep(0.02)
    await schedule(node.socket, recipient("hold-r-next"))
    await asyncio.sleep(0.01)
    assert node.replies().get("hold-r-next", {}).get("error_code") != "engine_busy"
    assert node.pool.in_hand()["general"] == 124
    await node.settle()
    assert node.pool.in_hand() == {"owner": 0, "general": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_running, shared_cap, recipients_running", [
    (0, 2, 12), (1, 2, 12), (4, 2, 12), (6, 2, 10), (10, 2, 10), (10, 0, 12)])
async def test_dr04_recipients_run_on_every_shared_slot_while_the_owner_stays_within_its_kept_ones(
        monkeypatch, owner_running, shared_cap, recipients_running):
    """Kept slots first: up to 4 owner frames running take nothing from the 12 shared slots; past that they borrow at
    most the cap (2), and with the cap at 0 recipients' slots never depend on the owner at all."""
    node = Node(monkeypatch, limit=16, reserved=4, shared_cap=shared_cap)
    for number in range(owner_running):
        await node.client._schedule_inbound_message(node.socket, owner_frame(f"hold-o{number}"))
    for number in range(16):
        await node.client._schedule_inbound_message(node.socket, recipient(f"hold-r{number}"))
    await asyncio.sleep(0.02)
    assert node.pool.in_use()[GENERAL] - node.pool.borrowed() == recipients_running
    assert node.pool.in_use()[RESERVED] == min(owner_running, 4)
    await node.settle()


@pytest.mark.asyncio
async def test_the_owners_background_work_waits_in_arrival_order_with_the_recipients(monkeypatch):
    node = Node(monkeypatch, limit=2, reserved=1)
    await node.receive(recipient("hold-r0"))                       # the shared slot
    for message in (recipient("r1"), owner_frame("b1", "source_sync"), recipient("r2"),
                    owner_frame("b2", "app_ingest"), recipient("r3")):
        await node.receive(message)
    assert node.pool.waiting() == {"owner": 0, "general": 5}
    await node.settle()
    assert node.handled == ["hold-r0", "r1", "b1", "r2", "b2", "r3"]
    assert node.pool.granted[(True, RESERVED)] == node.pool.granted[(True, GENERAL)] == 0


@pytest.mark.asyncio
async def test_a_frame_cancelled_before_it_ran_leaves_its_waiting_place(monkeypatch):
    node = Node(monkeypatch, limit=1, reserved=0)
    node.client._inbound_max_pending = 1                           # one place for the shared class (1 less 0 kept)
    await node.client._schedule_inbound_message(node.socket, recipient("hold-r0"))
    await node.client._schedule_inbound_message(node.socket, recipient("hold-r1"))   # no place left: busy
    assert node.replies()["hold-r1"]["error_code"] == "engine_busy"
    for task in list(node.client._inbound_tasks):
        task.cancel()                                              # before it ever ran
    await asyncio.sleep(0.01)
    assert node.pool.in_hand() == {"owner": 0, "general": 0}
    await node.receive(recipient("r2"))
    assert node.handled == ["r2"]


# --- R3a with the lane on --------------------------------------------------------------------------------------------

def bounds(monkeypatch, seconds: float) -> None:
    monkeypatch.setattr(control_plane_client, "INBOUND_AGE_BOUND_SECONDS",
                        {kind: seconds for kind in INBOUND_AGE_BOUND_SECONDS})


@pytest.mark.asyncio
async def test_shedding_is_the_same_with_the_lane_on_for_both_classes(monkeypatch):
    bounds(monkeypatch, 0.1)
    node = Node(monkeypatch, limit=2, reserved=1)
    await node.receive(recipient("hold-r"))                                  # the shared slot
    await node.receive(owner_frame("hold-o"))                                # the kept slot
    late = [stamped(frame(f"late-{kind}", kind), cls="third_party", acting=SOMEONE_ELSE) for kind in RECIPIENT_DOORS]
    late.append(owner_frame("late-owner-stamped-door", SEARCH))              # an owner stamp on a door: shed as theirs
    for message in late:
        await node.receive(message)
    await node.receive(owner_frame("o-late"))                                # the owner's interactive frame: never shed
    await node.receive(owner_frame("o-late-background", "store_message"))    # the owner's background frame: never shed
    await asyncio.sleep(0.25)                                                # past the bound, all still waiting
    await node.settle()
    replies = node.replies()
    for message in late:
        assert replies[message["id"]]["error_code"] == "engine_busy", message["id"]
    assert sorted(node.handled) == ["hold-o", "hold-r", "o-late", "o-late-background"]
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
    await node.receive(owner_frame("hold-o"))
    await node.receive(stamped(frame("gone-1", SEARCH), cls="third_party", acting=SOMEONE_ELSE))
    node.client._ws = Socket()                                               # the socket was replaced meanwhile
    await node.settle()
    assert "gone-1" not in node.handled and "gone-1" not in node.replies()
    assert node.client.inbound_shed_counts() == {("gone", SEARCH): 1}


# --- the switch off: as before ---------------------------------------------------------------------------------------

def test_the_lane_is_on_by_default_and_off_is_one_environment_line(monkeypatch):
    """NR183 shipped off; Wave 2 of kept questions turns it on by default (the owner's ruling D-10), bound and not,
    with its built settings. Off is still one line, and then the count alone turns nothing on."""
    async def handler(message):
        return None

    def made():
        return ControlPlaneClient("ws://example/ws/engine", "an-invented-key", handler, False)._owner_lane
    lane = made()
    assert (lane.limit, lane.reserved, lane.general, lane.shared_cap) == (16, 4, 12, 2)
    assert lane.types == switches.OWNER_LANE_TYPES.bound
    monkeypatch.setattr(switches, "is_bound", lambda env=None: True)         # on on a bound node too
    assert made() is not None
    monkeypatch.setenv(LANE, "false")
    assert made() is None
    monkeypatch.setenv(SLOTS, "2")                                            # the count alone turns nothing on
    assert made() is None
    assert made() is None
    monkeypatch.setenv(LANE, "maybe")                                         # unreadable is off
    assert made() is None
    monkeypatch.setenv(LANE, "on")
    lane = made()
    assert (lane.limit, lane.reserved, lane.general, lane.shared_cap) == (16, 2, 14, 2)
    assert lane.types == switches.OWNER_LANE_TYPES.bound
    monkeypatch.delenv(SLOTS)
    assert made().reserved == 4
    monkeypatch.setenv(SHARED_MAX, "0")
    monkeypatch.setenv(TYPES, "query, aggregate")
    lane = made()
    assert (lane.shared_cap, lane.types) == (0, ("query", "aggregate"))


@pytest.mark.asyncio
async def test_with_the_lane_off_nothing_of_it_is_read_and_the_semaphore_serves_in_arrival_order(monkeypatch):
    def never(message, types=None):
        raise AssertionError("the lane read a frame while off")
    monkeypatch.setattr(owner_lane, "is_owner_frame", never)
    monkeypatch.setattr(InboundSlots, "owner_frame", lambda self, message: never(message))
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


# --- WS0 review additions (kill rv_* survivors) ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_within_a_class_the_oldest_waiting_frame_goes_first_and_a_freed_kept_slot_wakes_an_owner():
    # NR183 after DR-04: owner frames borrow a shared slot only below the cap, which is never all of the shared slots;
    # the reviewer's (2, 1) pool has one shared slot and so nothing to borrow. Same order, a pool with two.
    pool = InboundSlots(3, 1, shared_cap=1)
    await pool.acquire(False)                       # the shared slots
    await pool.acquire(False)
    await pool.acquire(True)                        # the kept slot
    order = []

    async def waits(owner, name):
        slot = await pool.acquire(owner)
        order.append(name)
        return slot
    for name, owner in (("r1", False), ("r2", False), ("o1", True), ("o2", True)):
        asyncio.ensure_future(waits(owner, name))
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)
    pool.release(GENERAL)
    await asyncio.sleep(0.01)
    assert order == ["o1"]                          # the older owner frame
    pool.release(RESERVED)
    await asyncio.sleep(0.01)
    assert order == ["o1", "o2"]                    # a freed kept slot goes to the waiting owner frame at once
    pool.release(GENERAL)
    await asyncio.sleep(0.01)
    assert order == ["o1", "o2", "r1"]              # the older recipient frame
    pool.release(GENERAL)
    await asyncio.sleep(0.01)
    assert order == ["o1", "o2", "r1", "r2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError, asyncio.TimeoutError, TimeoutError, KeyError, asyncio.CancelledError])
async def test_the_slot_comes_back_whatever_the_door_raises(monkeypatch, error):
    node = Node(monkeypatch, limit=2, reserved=1)

    async def door(ws, message):
        raise error("an invented failure")
    monkeypatch.setattr(search_transport, "dispatch_message_search_batch", door)
    await node.receive(stamped(frame("r-1", BATCH), cls="third_party", acting=SOMEONE_ELSE))
    await node.receive(owner_frame("o-1", "raises"))
    await node.settle()
    assert node.pool.in_use() == {GENERAL: 0, RESERVED: 0}
