"""Kept questions Wave 2 (lane OFF2N; ``lanes/OFF2_CONTRACT.md`` §D, §E; the owner's ruling OD-13): background work
waits for the owner's own frames, as it already waits for a share's answer job.

Pinned here:

- the kill switch ``TOPOS_PERMISSIONS_V2_BACKGROUND_YIELDS_TO_OWNER`` is on bound and unbound; off is one line;
- every background wait (the model clients' thread and coroutine waits, the sharing assessments' unconditional wait,
  the start hold) waits while an owner frame is in flight and resumes when it ends; the same 110 s bound; a start's
  wait still ends at the node's stop;
- nothing waits for the owner in the owner's own work, inside an answer job, or inside an owner frame's own handling
  (on the client's loop and across to the app's loop): an owner frame never waits on itself;
- "Share evenly" and the kill switch turn it off live, mid-wait included;
- through the client: only a frame the owner lane accepts is counted: forged, lifted, retyped and unstamped owner
  claims never are, and a recipient's never is; the count comes down on a reply, an exception, a cancellation and a
  stop; with the lane off nothing is counted;
- ``owner_idle_seconds``: 0 while a frame is in flight, whole seconds after, from the start when none has been, at
  most 3600.

No model is called: the waits are measured on their own, and the assessments' transport stops before any call.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time

import pytest

from topos.permissions_v2 import answer_gate, away_config, switches
from tests.permissions_v2.test_answer_gate import ASSESSMENTS, Transport
from tests.topos.test_nr183_owner_lane import (ANOTHER_KEY, BATCH, Node, SOMEONE_ELSE, frame, owner_frame,
                                               stamped)
from tests.topos.test_nr183_owner_lane import pinned_key  # noqa: F401 -- autouse: the stamp key, no switch set

SWITCH = "TOPOS_PERMISSIONS_V2_BACKGROUND_YIELDS_TO_OWNER"
EVEN = {"v": 1, "while_using": "even", "others_when_idle": False}


@pytest.fixture(autouse=True)
def nothing_in_flight(monkeypatch, pinned_key):  # noqa: F811 -- after it: it clears every switch
    monkeypatch.setattr(answer_gate, "POLL_SECONDS", 0.01)
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_ANSWER_HOLDS_BACKGROUND", "false")   # the owner's part on its own
    monkeypatch.setattr(answer_gate, "_owner_ended_at", None)
    away_config.apply(away_config.DEFAULT)
    assert answer_gate.owner_frames() == 0 and not answer_gate.active()
    yield
    while answer_gate.owner_frames():
        answer_gate.owner_frame_ended()
    away_config.apply(away_config.DEFAULT)


class OwnerFrame:
    """One of the owner's frames in flight from now until `end()`, as the client counts it."""

    def __init__(self):
        answer_gate.owner_frame_began()
        self.ended = False

    def end(self):
        if not self.ended:
            self.ended = True
            answer_gate.owner_frame_ended()

    def end_after(self, seconds: float) -> threading.Timer:
        timer = threading.Timer(seconds, self.end)
        timer.start()
        return timer


# -- the switch ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
def test_the_kill_switch_is_on_bound_and_unbound_and_off_is_one_line(bound, monkeypatch):
    monkeypatch.setattr(switches, "is_bound", lambda env=None: bound)
    item = switches.lookup(SWITCH)
    assert item is switches.BACKGROUND_YIELDS_TO_OWNER and item.unbound is True and item.bound is True
    assert answer_gate.yields_to_owner() is True
    monkeypatch.setenv(SWITCH, "false")
    assert answer_gate.yields_to_owner() is False


# -- every background wait waits for an owner frame ---------------------------------------------------------------------

def test_no_owner_frame_no_wait():
    with answer_gate.background_work():
        assert answer_gate.yield_if_background() == 0.0
        assert asyncio.run(answer_gate.yield_to_answers_if_background()) == 0.0
        assert answer_gate.hold_background_start() == 0.0
    assert asyncio.run(answer_gate.yield_to_answers()) == 0.0


def test_a_background_model_call_in_a_thread_waits_until_the_owner_frame_ends(caplog):
    frame_ = OwnerFrame()
    frame_.end_after(0.2)
    with caplog.at_level(logging.INFO, logger=answer_gate.__name__), answer_gate.background_work():
        waited = answer_gate.yield_if_background()
    assert 0.15 <= waited < 2.0 and answer_gate.owner_frames() == 0
    assert not caplog.records                       # under LOG_SECONDS: no line (the bound's test reads the line)


def test_a_background_model_call_in_a_coroutine_waits_until_the_owner_frame_ends():
    OwnerFrame().end_after(0.2)

    async def call():
        with answer_gate.background_work():
            return await answer_gate.yield_to_answers_if_background()
    assert 0.15 <= asyncio.run(call()) < 2.0


def test_grounding_waits_for_the_owner_except_where_the_owner_asked_for_it():
    OwnerFrame().end_after(0.2)
    assert 0.15 <= answer_gate.yield_unless_the_owners() < 2.0       # unmarked: background wherever it runs
    OwnerFrame()
    with answer_gate.owners_own_work():
        assert answer_gate.yield_unless_the_owners() == 0.0


@pytest.mark.parametrize("name", sorted(ASSESSMENTS))
def test_no_sharing_assessment_starts_while_an_owner_frame_is_in_flight(name):
    transport, frame_ = Transport(), OwnerFrame()
    ended = []
    timer = threading.Timer(0.3, lambda: (ended.append(time.monotonic()), frame_.end()))
    timer.start()
    with pytest.raises(Exception):
        asyncio.run(ASSESSMENTS[name]({"input": {}}, transport=transport))
    timer.join()
    assert len(transport.started) == 1 and transport.started[0] >= ended[0], name


def test_a_start_waits_for_the_owner_even_with_the_answer_hold_off():
    assert switches.on(switches.ANSWER_HOLDS_BACKGROUND) is False
    OwnerFrame().end_after(0.2)
    assert 0.15 <= answer_gate.hold_background_start() < 2.0


def test_the_wait_is_bounded_by_the_same_110_seconds(caplog):
    OwnerFrame()                                                      # never ends
    clock = iter(x * 10.0 for x in range(100))
    with caplog.at_level(logging.INFO, logger=answer_gate.__name__), answer_gate.background_work():
        waited = answer_gate.yield_if_background(clock=lambda: next(clock), sleep=lambda _seconds: None)
    assert answer_gate.LIMIT_SECONDS == 110.0 and 110.0 <= waited <= 120.0
    assert caplog.records[-1].getMessage().endswith("for the owner's own request (the limit; it starts anyway)")
    assert 0.25 <= asyncio.run(answer_gate.yield_to_answers(limit=0.25)) < 1.0


def test_a_starts_wait_ends_when_the_node_stops(monkeypatch):
    from topos import runtime_shutdown
    run = runtime_shutdown.RuntimeGeneration(20_241)
    monkeypatch.setattr(runtime_shutdown, "current_generation", lambda: run)
    monkeypatch.setattr(answer_gate, "LIMIT_SECONDS", 6.0)
    monkeypatch.setattr(answer_gate, "POLL_SECONDS", 3.0)
    OwnerFrame()                                                      # never ends
    threading.Timer(0.2, run.request_stop, args=("an invented stop",)).start()
    began = time.monotonic()
    assert 0.15 <= answer_gate.hold_background_start() < 1.5 and time.monotonic() - began < 1.5


# -- never for the owner's own work, an answer job, or the owner frame itself --------------------------------------------

@pytest.mark.parametrize("mark", ["owners_own_work", "answer_job", "owner_frame"])
def test_the_owners_own_work_an_answer_job_and_an_owner_frames_handling_never_wait_for_the_owner(mark):
    OwnerFrame()
    started = time.monotonic()
    with getattr(answer_gate, mark)():
        assert asyncio.run(answer_gate.yield_to_answers()) == 0.0
        assert answer_gate.hold_background_start() == 0.0
        assert answer_gate.yield_unless_the_owners() == 0.0
        if mark != "owners_own_work":                 # owners_own_work is itself the opposite of background work
            with answer_gate.background_work():
                assert answer_gate.yield_if_background() == 0.0
                assert asyncio.run(answer_gate.yield_to_answers_if_background()) == 0.0
    assert time.monotonic() - started < 0.5


def test_an_answer_job_still_waits_for_nothing_but_answers_never_for_the_owner():
    OwnerFrame()
    with answer_gate.answer_job():
        assert asyncio.run(answer_gate.yield_to_answers()) == 0.0


@pytest.mark.asyncio
async def test_an_owner_frames_own_background_work_never_waits_on_itself_through_the_client(monkeypatch):
    node = Node(monkeypatch, limit=3, reserved=1)
    waited = {}

    async def app_handler(message):
        with answer_gate.background_work():
            waited[message["id"]] = (await answer_gate.yield_to_answers_if_background(),
                                     await asyncio.to_thread(answer_gate.yield_if_background))
        return {"id": message["id"], "status": "ok"}
    node.client.handler = app_handler
    await node.receive(owner_frame("owner-1", "llm_generation"))
    await node.settle()
    assert waited == {"owner-1": (0.0, 0.0)}


@pytest.mark.asyncio
async def test_the_mark_crosses_to_the_apps_loop_with_the_frame(monkeypatch):
    node = Node(monkeypatch, limit=3, reserved=1)
    seen = {}

    async def app_handler(message):
        seen[message["id"]] = (answer_gate.in_owner_frame(), threading.get_ident())
        return {"id": message["id"], "status": "ok"}
    node.client.handler = app_handler
    app_loop = asyncio.new_event_loop()
    thread = threading.Thread(target=app_loop.run_forever, daemon=True)
    thread.start()
    try:
        node.client._handler_loop = app_loop
        await node.receive(owner_frame("owner-1"))
        await node.receive(stamped(frame("recipient-1", "query"), cls="third_party", acting=SOMEONE_ELSE))
        await node.settle()
    finally:
        app_loop.call_soon_threadsafe(app_loop.stop)
        thread.join(2)
        app_loop.close()
    assert seen["owner-1"] == (True, thread.ident)
    assert seen.get("recipient-1", (False,))[0] is False


# -- turned off live -------------------------------------------------------------------------------------------------------

def test_share_evenly_and_the_kill_switch_turn_it_off_live_mid_wait_included(monkeypatch):
    OwnerFrame()
    away_config.apply(EVEN)
    with answer_gate.background_work():
        assert answer_gate.yield_if_background() == 0.0 and answer_gate.hold_background_start() == 0.0
    away_config.apply(away_config.DEFAULT)
    threading.Timer(0.2, away_config.apply, args=(EVEN,)).start()      # the owner chooses "Share evenly" meanwhile
    with answer_gate.background_work():
        assert 0.15 <= answer_gate.yield_if_background() < 2.0
    away_config.apply(away_config.DEFAULT)
    monkeypatch.setenv(SWITCH, "false")
    with answer_gate.background_work():
        assert answer_gate.yield_if_background() == 0.0
        assert asyncio.run(answer_gate.yield_to_answers()) == 0.0


def test_with_the_rule_off_an_answer_job_is_still_waited_for_exactly_as_before():
    away_config.apply(EVEN)
    OwnerFrame()
    answer_gate.delta(1)
    threading.Timer(0.2, answer_gate.delta, args=(-1,)).start()
    with answer_gate.background_work():
        assert 0.15 <= answer_gate.yield_if_background() < 2.0


# -- through the client: what counts ------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_only_a_frame_the_lane_accepts_is_counted_and_it_comes_down_when_it_ends(monkeypatch):
    node = Node(monkeypatch, limit=4, reserved=1)
    claims = [
        owner_frame("hold-forged", key=ANOTHER_KEY),                                     # signed by another key
        owner_frame("hold-lifted", signed_for=frame("hold-other", "query")),             # lifted from another frame
        owner_frame("hold-retyped", signed_for=frame("hold-retyped", "store_message")),  # signed for another type
        frame("hold-unstamped", "llm_generation"),                                       # no stamp at all
        stamped(frame("hold-recipient", BATCH), cls="third_party", acting=SOMEONE_ELSE),
        owner_frame("hold-background", "source_sync"),                                   # the owner's, not the lane's
    ]
    for claim in claims:
        await node.receive(claim)
    assert answer_gate.owner_frames() == 0 and answer_gate.counts_owner_frames() is True
    await node.receive(owner_frame("hold-owner", "llm_generation"))
    assert answer_gate.owner_frames() == 1
    node.finish("hold-owner")
    await asyncio.sleep(0.05)
    assert answer_gate.owner_frames() == 0
    await node.settle()


@pytest.mark.asyncio
async def test_the_count_comes_down_on_an_exception_a_cancellation_and_a_stop(monkeypatch):
    node = Node(monkeypatch, limit=3, reserved=1)
    await node.receive(owner_frame("raise-1"))                            # the app handler raises
    await asyncio.sleep(0.05)
    assert answer_gate.owner_frames() == 0
    await node.receive(owner_frame("hold-1"))
    await node.receive(owner_frame("hold-2"))
    assert answer_gate.owner_frames() == 2
    for task in list(node.client._inbound_tasks):                        # what a stop does to the frames in hand
        task.cancel()
    await asyncio.sleep(0.05)
    assert answer_gate.owner_frames() == 0 and not node.client._inbound_tasks


@pytest.mark.asyncio
async def test_a_frame_refused_busy_at_admission_is_never_counted(monkeypatch):
    node = Node(monkeypatch, limit=1, reserved=0)
    node.pool.admit = lambda owner, max_pending: False
    await node.receive(owner_frame("owner-1"))
    assert node.replies()["owner-1"]["error_code"] == "engine_busy" and answer_gate.owner_frames() == 0


@pytest.mark.asyncio
async def test_with_the_lane_off_nothing_is_counted(monkeypatch):
    node = Node(monkeypatch, limit=1, lane=False)
    assert answer_gate.counts_owner_frames() is False
    await node.receive(owner_frame("hold-owner"))
    assert answer_gate.owner_frames() == 0
    await node.settle()


# -- owner idleness ------------------------------------------------------------------------------------------------------

def test_owner_idle_seconds_is_zero_in_flight_counts_up_after_and_is_capped():
    now = [1000.0]
    clock = lambda: now[0]                                                 # noqa: E731
    assert answer_gate.owner_idle_seconds(since=990.0, clock=clock) == 10     # none yet: since the start
    answer_gate.owner_frame_began()
    now[0] = 1500.0
    assert answer_gate.owner_idle_seconds(since=990.0, clock=clock) == 0
    answer_gate.owner_frame_ended(clock=clock)
    now[0] = 1512.7
    assert answer_gate.owner_idle_seconds(since=990.0, clock=clock) == 12
    now[0] = 1500.0 + 99_999
    assert answer_gate.owner_idle_seconds(since=990.0, clock=clock) == 3600
