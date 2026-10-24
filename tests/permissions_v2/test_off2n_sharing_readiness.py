"""Kept questions Wave 2 (lane OFF2N; ``lanes/OFF2_CONTRACT.md`` §C): whether this node has caught up, as counts.

Pinned here:

- served now, per grant, on a real share index: unserved at start whether or not a file is on disk, served once a
  build publishes it or the sweep or the request path proves it, unserved when a check finds it stale or the sweep
  drops it, served again when the restore rebuilds it, and out of ``total`` when the share ends;
- ``unserved_grants`` sorted and at most 64, with ``unserved_truncated``;
- ``catching_up`` for each of its conditions, and the backlog threshold at 10;
- the counters come from the code that passes them: the refresh loop's startup backlog pass while it runs, a
  pipeline lane's own claim for what is queued, ``process_job`` for what runs;
- ``owner_idle_seconds`` null unless "Answer others only when …" is on (and the owner lane counts frames);
- the object is the same on the heartbeat, the register and ``get_sharing_readiness``, every field present;
- the read is owner-class only, answers in the client's fast lane with the write gate held and every inbound slot
  full (p95 under 250 ms here), and builds from memory only (no SQLite connection, no write gate);
- the capability says 1.

Every id here is invented; keys are made at run time.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time

import pytest

from topos.core.handlers import dispatch_relay_message
from topos.core.handlers.registry import OFF_LOOP_MESSAGE_TYPES, OWNER_ONLY_MESSAGE_TYPES
from topos.engine import registration
from topos.permissions_v2 import answer_gate, away_config, sharing_readiness as readiness, switches
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401 -- legacy's fixture
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- the fixture
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_refresh_loop import Clock, loop_for, reassess
from tests.topos.test_nr183_owner_lane import (ANOTHER_KEY, BATCH, Node, SOMEONE_ELSE, frame, owner_frame,
                                               stamped)
from tests.topos.test_nr183_owner_lane import pinned_key  # noqa: F401 -- autouse: the stamp key, no switch set

READ = "get_sharing_readiness"
FIELDS = {"v", "started_at", "share_indexes", "unserved_grants", "unserved_truncated", "pipeline_backlog",
          "catching_up", "owner_idle_seconds"}
IDLE = {"v": 1, "while_using": "owner_first", "others_when_idle": True}


@pytest.fixture(autouse=True)
def fresh(monkeypatch, pinned_key):  # noqa: F811 -- after it: it clears every switch
    monkeypatch.setenv(switches.MESSAGE_SEARCH.name, "true")
    readiness.reset()
    away_config.apply(away_config.DEFAULT)
    yield
    readiness.reset()
    away_config.apply(away_config.DEFAULT)
    while answer_gate.owner_frames():
        answer_gate.owner_frame_ended()


def counts():
    built = readiness.build()
    return built["share_indexes"]["ready"], built["share_indexes"]["total"], built["unserved_grants"]


# -- served now, on a real index ------------------------------------------------------------------------------------

def test_a_share_index_is_served_only_once_proven_and_unserved_when_it_goes(legacy, tmp_path, monkeypatch):
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    root = node.index.root
    assert readiness.build()["share_indexes"] == {"ready": 0, "total": 0} and readiness.build()["catching_up"]
    assert node.rebuild() == {"grant-search": "ready"}                 # a build publishes: served
    assert counts() == (0, 0, []) and readiness.build()["catching_up"] is True   # which grants: not known yet
    readiness.note_grants(root, ["grant-search"])
    assert counts() == (1, 1, []) and readiness.build()["catching_up"] is False
    loop = loop_for(node, Clock(node.now[0]))
    loop.observe(node.index)                                           # the restore has seen it published
    readiness.reset()                                                  # a restart: the file is still on disk
    readiness.note_grants(root, ["grant-search"])                      # the ledger read every round makes
    assert list(root.glob("grant-*.db")) and counts() == (0, 1, ["grant-search"])
    assert readiness.build()["catching_up"] is True                    # a file is not "served"
    node.index.sweep(now=node.now[0])                                  # the sweep finds it current: served
    assert counts() == (1, 1, [])
    output, refused = node.search_request("Synthetic message", k=10)
    assert refused is None and counts() == (1, 1, [])                  # a passing request keeps it served
    reassess(node, identity)                                           # the review digest moves: stale
    output, refused = node.search_request("Synthetic message", k=10)
    assert refused is not None and counts() == (0, 1, ["grant-search"])   # the request path's check said so
    loop.observe(node.index)
    receipt = loop.run_pending()                                       # the restore rebuilds it: served again
    assert [grant.state for grant in receipt.grants] == ["ready"] and counts() == (1, 1, [])
    reassess(node, identity)
    node.index.sweep(now=node.now[0])                                  # dropped by the sweep: shredded, unserved
    assert not list(root.glob("grant-*.db")) and counts() == (0, 1, ["grant-search"])
    with owner():
        node.ledger.revoke("grant-search", expected_epoch=node.epoch(), command_id="revoke-search")
        assert node.index.rebuild("grant-search", now=node.now[0])["state"] == "removed"   # the queue's build of it
    assert counts() == (0, 0, []) and readiness.build()["catching_up"] is False   # the share ended


def test_a_share_whose_build_did_not_publish_still_counts_as_unserved(tmp_path):
    readiness.note_grants(tmp_path, ["grant-a"])
    readiness.note_owed(tmp_path, "grant-new")                         # a new share, its build ended stale
    assert counts() == (0, 2, ["grant-a", "grant-new"])
    readiness.note_owed(tmp_path / "elsewhere", "grant-other")
    assert counts()[1] == 2


def test_a_sweep_counts_the_grants_and_drops_an_ended_ones_index(legacy, tmp_path, monkeypatch):
    node, _identity = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    readiness.reset()
    node.index.sweep(now=node.now[0])
    assert counts() == (1, 1, [])
    with owner():
        node.ledger.revoke("grant-search", expected_epoch=node.epoch(), command_id="revoke-search")
    node.index.sweep(now=node.now[0])                                  # the sweep reads the ledger: ended
    assert counts() == (0, 0, []) and not list(node.index.root.glob("grant-*.db"))


def test_a_verdict_about_an_older_file_never_overrides_a_newer_one(tmp_path):
    readiness.note_grants(tmp_path, ["grant-a"])
    readiness.note_served(tmp_path, "grant-a")
    readiness.note_unserved(tmp_path, "grant-a", still=lambda: False)   # that file was replaced since
    assert counts() == (1, 1, [])
    readiness.note_unserved(tmp_path, "grant-a", still=lambda: 1 / 0)   # cannot tell: no change
    assert counts() == (1, 1, [])
    readiness.note_unserved(tmp_path, "grant-a", still=lambda: True)
    readiness.note_served(tmp_path, "grant-a", still=lambda: False)
    assert counts() == (0, 1, ["grant-a"])


def test_another_folders_verdicts_are_not_this_ones(tmp_path):
    readiness.note_grants(tmp_path / "a", ["grant-a"])
    readiness.note_served(tmp_path / "b", "grant-a")
    assert counts() == (0, 1, ["grant-a"])
    readiness.note_grants(tmp_path / "b", ["grant-b"])                 # the served folder changed: afresh
    assert counts() == (0, 1, ["grant-b"])


def test_a_sweep_that_cannot_run_leaves_nothing_served(tmp_path):
    readiness.note_grants(tmp_path, ["grant-a", "grant-b"])
    readiness.note_served(tmp_path, "grant-a")
    readiness.note_none_served(tmp_path)
    assert counts() == (0, 2, ["grant-a", "grant-b"])


def test_unserved_grants_are_sorted_and_at_most_64(tmp_path):
    grants = [f"grant-{number:03d}" for number in range(70)]
    readiness.note_grants(tmp_path, reversed(grants))
    for grant in grants[:3]:
        readiness.note_served(tmp_path, grant)
    built = readiness.build()
    assert built["share_indexes"] == {"ready": 3, "total": 70}
    assert built["unserved_grants"] == grants[3:67] and built["unserved_truncated"] is True
    for grant in grants[3:6]:
        readiness.note_served(tmp_path, grant)
    built = readiness.build()
    assert len(built["unserved_grants"]) == 64 and built["unserved_truncated"] is False


# -- catching up --------------------------------------------------------------------------------------------------------

def test_catching_up_for_each_of_its_conditions(tmp_path, monkeypatch):
    readiness.note_grants(tmp_path, [])
    assert readiness.build()["catching_up"] is False
    readiness.backlog_pass(True)
    assert readiness.build()["catching_up"] is True
    readiness.backlog_pass(False)
    for kind in ("local_sync_embeddings_catchup", "imported_ai_chat_embeddings_catchup", "ai_tool_enrichment_catchup"):
        readiness.note_queued("pipeline-catchup", {kind: 1})
        assert readiness.build()["catching_up"] is True, kind
        readiness.note_queued("pipeline-catchup", {})
        with readiness.job_running(kind):
            assert readiness.build()["catching_up"] is True, kind
        assert readiness.build()["catching_up"] is False, kind
    readiness.note_queued("pipeline", {"inbox_deferred_enrichment": 6, "truth_inbound_check": 1})
    readiness.note_queued("pipeline-long", {"local_sync": 2})
    assert readiness.build()["pipeline_backlog"] == 9 and readiness.build()["catching_up"] is False
    readiness.note_queued("pipeline-long", {"local_sync": 3})
    assert readiness.build()["pipeline_backlog"] == 10 == readiness.CATCHING_UP_BACKLOG
    assert readiness.build()["catching_up"] is True


def test_grants_not_yet_known_are_catching_up_only_while_search_is_on(monkeypatch):
    assert readiness.build()["catching_up"] is True
    monkeypatch.setenv(switches.MESSAGE_SEARCH.name, "false")
    assert readiness.build()["catching_up"] is False


def test_the_refresh_loops_startup_backlog_pass_counts_while_it_runs(tmp_path):
    from tests.permissions_v2.test_refresh_loop import daytime
    from tests.permissions_v2.test_refresh_loop_slices import NEWEST, MIDDLE, SliceWorker, aged, walk, windowed
    T = daytime()
    clock, worker = Clock(T), SliceWorker(aged(T, [*NEWEST, *MIDDLE]))
    loop = windowed(tmp_path, clock, worker, window=60 * 86400)
    readiness.note_grants(tmp_path, [])
    loop.run_catchup()                                                 # no state file: the backlog, first slice
    assert loop._pass["origin"] == "startup_backlog" and readiness.build()["catching_up"] is True
    assert walk(loop, worker, clock).cause_class == "startup_backlog"
    assert readiness.build()["catching_up"] is False


def test_a_lanes_claim_counts_what_it_has_queued_and_process_job_what_runs(tmp_path, monkeypatch):
    from topos.pipeline import job_runner
    from topos.pipeline.job_store import enqueue_job
    readiness.note_grants(tmp_path, [])
    conn = sqlite3.connect(tmp_path / "jobs.db", check_same_thread=False)
    for number in range(4):
        enqueue_job(conn, kind="inbox_deferred_enrichment", source_id="an-invented-source",
                    idempotency_key=f"inbox:{number}", payload={"n": number})
    enqueue_job(conn, kind="ai_tool_enrichment_catchup", source_id="an-invented-source",
                idempotency_key="catchup:1", payload={})
    job_runner._note_queued(conn, "pipeline", ["inbox_deferred_enrichment", "ai_tool_enrichment_catchup"])
    built = readiness.build()
    assert built["pipeline_backlog"] == 5 and built["catching_up"] is True
    job_runner._note_queued(conn, "pipeline", ["inbox_deferred_enrichment"])
    assert readiness.build()["pipeline_backlog"] == 4
    job_runner._note_queued(conn, "pipeline", [])                      # nothing to count: the last count stands
    job_runner._note_queued(object(), "pipeline", ["x"])               # a count that fails changes nothing
    assert readiness.build()["pipeline_backlog"] == 4
    seen = []

    async def executor(payload):
        seen.append(readiness.build()["catching_up"])
        return {"status": "ok"}
    monkeypatch.setitem(job_runner.EXECUTORS, "ai_tool_enrichment_catchup", executor)
    readiness.note_queued("pipeline", {})
    job = {"job_id": "job-1", "kind": "ai_tool_enrichment_catchup", "payload": {}, "attempts": 0}
    monkeypatch.setattr(job_runner, "_run_db", lambda *a, **k: _done())
    try:
        asyncio.run(job_runner.process_job(lambda: conn, job))
    except Exception:  # noqa: BLE001 -- the bookkeeping after the executor is not this test's
        pass
    assert seen == [True] and readiness.build()["catching_up"] is False
    conn.close()


async def _done(*_args, **_kwargs):
    return None


# -- owner idleness -------------------------------------------------------------------------------------------------------

def test_owner_idle_seconds_is_null_unless_idle_only_is_on_and_frames_are_counted(monkeypatch):
    answer_gate.owner_frames_counted(True)
    assert readiness.build()["owner_idle_seconds"] is None
    away_config.apply(IDLE)
    assert isinstance(readiness.build()["owner_idle_seconds"], int)
    answer_gate.owner_frame_began()
    assert readiness.build()["owner_idle_seconds"] == 0
    answer_gate.owner_frame_ended()
    assert 0 <= readiness.build()["owner_idle_seconds"] <= 1
    answer_gate.owner_frames_counted(False)                          # the owner lane off: nothing is told
    assert readiness.build()["owner_idle_seconds"] is None
    answer_gate.owner_frames_counted(True)


# -- the carriers and the read --------------------------------------------------------------------------------------------

def test_the_same_object_on_the_heartbeat_the_register_and_the_read(tmp_path, monkeypatch):
    readiness.note_grants(tmp_path, ["grant-a", "grant-b"])
    readiness.note_served(tmp_path, "grant-b")
    monkeypatch.setattr(registration, "build_engine_capabilities", lambda: {"permissions_v2_sharing_readiness": 1})
    beat = registration.build_engine_heartbeat_message()["payload"]["sharing_readiness"]
    register = registration.build_engine_register_message()["payload"]["sharing_readiness"]
    read = asyncio.run(dispatch_relay_message(owner_frame("read-1", READ)))
    assert read["status"] == "ok" and beat == register == read["payload"] == readiness.build()
    assert set(beat) == FIELDS and beat["v"] == 1 and isinstance(beat["started_at"], int)
    assert beat["share_indexes"] == {"ready": 1, "total": 2} and beat["unserved_grants"] == ["grant-a"]
    assert json.loads(json.dumps(beat)) == beat


def test_a_presence_message_without_readiness_still_goes(monkeypatch):
    monkeypatch.setattr(readiness, "build", lambda: 1 / 0)
    payload = registration.build_engine_heartbeat_message()["payload"]
    assert "sharing_readiness" not in payload and "capabilities" in payload
    reply = asyncio.run(dispatch_relay_message(owner_frame("read-2", READ)))
    assert reply == {"id": "read-2", "status": "error", "error": "sharing_readiness_unavailable"}


@pytest.mark.parametrize("how", [
    {"cls": "third_party", "acting": SOMEONE_ELSE}, {"cls": "third_party", "acting": "owner-nr183"},
    {"cls": "owner_automation"}, {"key": ANOTHER_KEY},
], ids=["a recipient", "a third party naming the owner", "a routine", "a stamp that does not verify"])
def test_the_read_is_the_owner_class_only(how):
    reply = asyncio.run(dispatch_relay_message(stamped(frame("read-3", READ), **how)))
    assert reply["status"] == "error" and reply["code"] == 403 and "payload" not in reply
    unstamped = asyncio.run(dispatch_relay_message(frame("read-4", READ)))
    assert unstamped == {"id": "read-4", "status": "error", "code": 403, "error": "owner_mode_required"}


def test_it_is_owner_only_off_the_loop_in_the_fast_lane_and_the_capability_says_one():
    from topos import control_plane_client
    assert READ in OWNER_ONLY_MESSAGE_TYPES and READ in OFF_LOOP_MESSAGE_TYPES
    assert READ in control_plane_client._FAST_INBOUND_MESSAGE_TYPES
    assert registration.build_engine_capabilities()["permissions_v2_sharing_readiness"] == 1
    snapshot = json.loads(open("topos/protocol/handled_message_types.json", encoding="utf-8").read())
    assert READ in snapshot["handled_message_types"] and READ in snapshot["owner_only_message_types"]


def test_building_it_opens_no_database_and_takes_no_write_gate(monkeypatch, tmp_path):
    import topos.core.handlers as hub
    from topos.storage.db import write_gate
    readiness.note_grants(tmp_path, ["grant-a"])
    away_config.apply(IDLE)
    answer_gate.owner_frames_counted(True)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("readiness touched the database")
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(hub, "get_db_connection", forbidden)
    gate = write_gate.db_write_lock()
    holder = threading.Thread(target=lambda: (gate.acquire(), time.sleep(0.6), gate.release()))
    holder.start()
    time.sleep(0.05)
    try:
        started = time.monotonic()
        built = readiness.build()
        reply = asyncio.run(dispatch_relay_message(owner_frame("read-5", READ)))
        assert time.monotonic() - started < 0.4                      # the gate is held for 0.6 s meanwhile
    finally:
        holder.join()
    assert reply["status"] == "ok" and reply["payload"]["share_indexes"] == built["share_indexes"]


@pytest.mark.asyncio
async def test_the_read_answers_in_the_fast_lane_with_the_gate_held_and_every_slot_full(monkeypatch, tmp_path):
    from topos.storage.db import write_gate
    node = Node(monkeypatch, limit=3, reserved=1)

    async def app(message):
        if message["id"].startswith("hold"):
            await node.held(message["id"])
            return {"id": message["id"], "status": "ok"}
        return await dispatch_relay_message(message)
    node.client.handler = app
    readiness.note_grants(tmp_path, ["grant-a"])
    for number in range(2):                                           # every shared slot held by a recipient
        await node.receive(stamped(frame(f"hold-r{number}", BATCH), cls="third_party", acting=SOMEONE_ELSE))
    await node.receive(owner_frame("hold-owner"))                     # and the kept one by the owner
    await node.receive(stamped(frame("hold-waiting", BATCH), cls="third_party", acting=SOMEONE_ELSE))
    assert node.pool.in_use() == {"general": 2, "reserved": 1} and node.pool.waiting()["general"] == 1
    gate = write_gate.db_write_lock()
    release = threading.Event()
    holder = threading.Thread(target=lambda: (gate.acquire(), release.wait(10), gate.release()))
    holder.start()
    seconds = []
    try:
        for number in range(40):
            began = time.monotonic()
            await node.client._schedule_inbound_message(node.socket, owner_frame(f"read-{number}", READ))
            for _ in range(500):
                if f"read-{number}" in node.replies():
                    break
                await asyncio.sleep(0.002)
            seconds.append(time.monotonic() - began)
            assert node.replies()[f"read-{number}"]["status"] == "ok"
    finally:
        release.set()
        holder.join()
        await node.settle()
    seconds.sort()
    p95 = seconds[int(len(seconds) * 0.95) - 1]
    assert p95 < 0.25, f"p95 {p95 * 1000:.0f} ms"
