"""Kept questions Wave 2 (lane OFF2N; ``lanes/OFF2_CONTRACT.md`` §D, §G): the NR183 owner lane on by default, and the
owner's "Share evenly" switching its reserve off live.

Pinned here:

- the lane is on with nothing set, with its built settings (16 slots, 4 kept, at most 2 borrowed, the built types);
- under ``owner_first`` (the default) DR-23's DR-04 property holds: a flood of owner frames never holds more than 2
  shared slots, and recipients keep the other 10;
- under ``even`` every slot is shared: recipients may take every slot, kept ones included, and a freed slot goes to
  the oldest waiting frame whichever its class;
- the mode is read on every acquire and wake: a change frees waiting frames at once (the client's listener wakes the
  pool on its loop), and across any number of changes while frames hold slots no slot is stranded or released twice;
- a mode that cannot be read is the lane's own order.

Every id here is invented.
"""
from __future__ import annotations

import asyncio
import random

import pytest

from topos.control_plane_client import ControlPlaneClient
from topos.permissions_v2 import away_config, switches
from topos.permissions_v2.owner_lane import BORROWED, GENERAL, RESERVED, InboundSlots
from tests.topos.test_nr183_owner_lane import Node, SOMEONE_ELSE, frame, owner_frame, stamped
from tests.topos.test_nr183_owner_lane import pinned_key  # noqa: F401 -- autouse: the stamp key, no switch set

EVEN = {"v": 1, "while_using": "even", "others_when_idle": False}


@pytest.fixture(autouse=True)
def owner_first():
    away_config.apply(away_config.DEFAULT)
    yield
    away_config.apply(away_config.DEFAULT)


def evenly_pool(limit=4, reserved=1, shared_cap=2):
    return InboundSlots(limit, reserved, shared_cap=shared_cap, shares_evenly=away_config.shares_evenly)


def test_with_nothing_set_the_lane_is_on_with_its_built_settings():
    async def handler(message):
        return None
    lane = ControlPlaneClient("ws://example/ws/engine", "an-invented-key", handler, False)._owner_lane
    assert lane is not None and (lane.limit, lane.reserved, lane.general, lane.shared_cap) == (16, 4, 12, 2)
    assert lane.types == switches.OWNER_LANE_TYPES.bound and "llm_generation" in lane.types
    assert lane.evenly() is False
    away_config.apply(EVEN)
    assert lane.evenly() is True                 # the built lane reads the owner's setting, live


@pytest.mark.asyncio
async def test_dr04_owner_frames_borrow_at_most_two_shared_slots_with_the_reserve_on():
    pool = InboundSlots(16, 4, shared_cap=2, shares_evenly=away_config.shares_evenly)
    slots = [await pool.acquire(True) for _ in range(6)]
    waiting = [asyncio.ensure_future(pool.acquire(True)) for _ in range(34)]
    await asyncio.sleep(0)
    assert pool.in_use() == {GENERAL: 2, RESERVED: 4} and pool.borrowed() == 2
    recipients = [await pool.acquire(False) for _ in range(10)]      # the other 10 shared slots are theirs
    assert pool.in_use() == {GENERAL: 12, RESERVED: 4} and pool.borrowed() == 2
    for task in waiting:
        task.cancel()
    await asyncio.gather(*waiting, return_exceptions=True)
    for slot in slots + recipients:
        pool.release(slot)
    assert pool.in_use() == {GENERAL: 0, RESERVED: 0} and pool.borrowed() == 0


@pytest.mark.asyncio
async def test_share_evenly_lets_recipients_take_every_slot_kept_ones_included():
    pool = evenly_pool(limit=4, reserved=1)
    away_config.apply(EVEN)
    taken = [await pool.acquire(False) for _ in range(4)]
    assert sorted(taken) == [GENERAL, GENERAL, GENERAL, RESERVED] and pool.in_use() == {GENERAL: 3, RESERVED: 1}
    waiter = asyncio.ensure_future(pool.acquire(True))   # the owner frame waits like anyone: no kept slot is left
    await asyncio.sleep(0)
    assert not waiter.done()
    pool.release(RESERVED)
    assert await waiter == RESERVED
    for slot in (GENERAL, GENERAL, GENERAL, RESERVED):
        pool.release(slot)
    assert pool.in_use() == {GENERAL: 0, RESERVED: 0}


@pytest.mark.asyncio
async def test_share_evenly_serves_waiting_frames_in_arrival_order_whichever_their_class():
    pool = evenly_pool(limit=2, reserved=1)
    away_config.apply(EVEN)
    held = [await pool.acquire(False), await pool.acquire(False)]
    order = []

    async def wait(name, owner):
        slot = await pool.acquire(owner)
        order.append(name)
        return slot
    tasks = []
    for name, owner in (("r1", False), ("o1", True), ("r2", False), ("o2", True)):
        tasks.append(asyncio.ensure_future(wait(name, owner)))
        await asyncio.sleep(0)
    for slot in held:
        pool.release(slot)
        await asyncio.sleep(0)
    assert order == ["r1", "o1"]
    pool.release(await tasks[0])
    await asyncio.sleep(0)
    pool.release(await tasks[1])
    await asyncio.sleep(0)
    assert order == ["r1", "o1", "r2", "o2"]
    for task in tasks[2:]:
        pool.release(await task)
    assert pool.in_use() == {GENERAL: 0, RESERVED: 0} and pool.borrowed() == 0


@pytest.mark.asyncio
async def test_under_the_owners_order_a_recipient_never_takes_the_kept_slot_it_could_take_evenly():
    pool = evenly_pool(limit=2, reserved=1)
    held = [await pool.acquire(False), await pool.acquire(True)]   # the shared slot and the kept one
    order = []

    async def wait(name, owner):
        slot = await pool.acquire(owner)
        order.append(name)
        return slot
    tasks = [asyncio.ensure_future(wait(name, owner)) for name, owner in (("r1", False), ("o1", True))]
    await asyncio.sleep(0)
    pool.release(held[0])                                          # a shared slot frees: the owner may not borrow it
    await asyncio.sleep(0)                                         # (cap 0 here: one shared slot only)
    assert order == ["r1"]
    pool.release(held[1])
    await asyncio.sleep(0)
    assert order == ["r1", "o1"]
    for task in tasks:
        pool.release(await task)


@pytest.mark.asyncio
async def test_a_change_of_mode_frees_waiting_frames_through_the_clients_listener(monkeypatch):
    node = Node(monkeypatch, limit=3, reserved=1)
    node.client._owner_lane = node.pool = evenly_pool(limit=3, reserved=1)
    node.client._loop = asyncio.get_running_loop()
    for number in range(3):                                        # two run on the shared slots, one waits
        await node.receive(stamped(frame(f"hold-{number}", "permissions_v2_message_search_batch"), cls="third_party",
                                   acting=SOMEONE_ELSE))
    assert node.pool.in_use() == {GENERAL: 2, RESERVED: 0} and node.pool.waiting()["general"] == 1
    away_config.apply(EVEN)                                        # what put_away_config does, from another thread
    await asyncio.sleep(0.05)
    assert node.pool.in_use() == {GENERAL: 2, RESERVED: 1} and node.pool.waiting()["general"] == 0
    await node.settle()
    assert node.pool.in_use() == {GENERAL: 0, RESERVED: 0}


@pytest.mark.asyncio
async def test_no_slot_is_stranded_or_released_twice_across_mode_changes_while_frames_hold_slots():
    rng = random.Random(20261024)
    pool = evenly_pool(limit=6, reserved=2, shared_cap=2)
    held: list = []
    waiting: list = []
    for step in range(600):
        action = rng.random()
        if action < 0.12:
            away_config.apply(EVEN if rng.random() < 0.5 else away_config.DEFAULT)
            pool.wake()
        elif action < 0.55:
            owner = rng.random() < 0.5
            waiting.append((owner, asyncio.ensure_future(pool.acquire(owner))))
        elif action < 0.62 and waiting:
            _owner, task = waiting.pop(rng.randrange(len(waiting)))
            task.cancel()
        elif held:
            pool.release(held.pop(rng.randrange(len(held))))
        await asyncio.sleep(0)
        for item in list(waiting):
            if item[1].done():
                waiting.remove(item)
                if not item[1].cancelled():
                    held.append(item[1].result())
        used = pool.in_use()
        assert 0 <= used[GENERAL] <= pool.general and 0 <= used[RESERVED] <= pool.reserved, step
        assert used[GENERAL] + used[RESERVED] == len(held), step
        assert pool.borrowed() == sum(1 for slot in held if slot == BORROWED) <= used[GENERAL], step
    for _owner, task in waiting:
        task.cancel()
    results = await asyncio.gather(*(task for _owner, task in waiting), return_exceptions=True)
    held += [slot for slot in results if isinstance(slot, str)]
    for slot in held:
        pool.release(slot)
    assert pool.in_use() == {GENERAL: 0, RESERVED: 0} and pool.borrowed() == 0 and not pool._arrival
    with pytest.raises(RuntimeError):
        pool.release(GENERAL)


def test_a_mode_that_cannot_be_read_is_the_lanes_own_order():
    def broken():
        raise RuntimeError("an invented failure")
    assert InboundSlots(4, 1, shares_evenly=broken).evenly() is False
    assert InboundSlots(4, 1, shares_evenly=lambda: "even").evenly() is False   # only a plain True
    assert InboundSlots(4, 1).evenly() is False


@pytest.mark.asyncio
async def test_under_share_evenly_owner_frames_are_still_told_apart_for_the_yield_rules_idleness(monkeypatch):
    node = Node(monkeypatch, limit=3, reserved=1)
    away_config.apply(EVEN)
    assert node.pool.owner_frame(owner_frame("o-1")) is True
