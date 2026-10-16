"""NR179 G1: with `ANSWER_HOLDS_BACKGROUND` on, background starts that are not model calls wait for a share's answer job.

Seen on the owner's node on 15 Oct (1.5.9): the three answer jobs of 100 s or more all ran while graph-rebuild
subprocesses ran and a search-index build held the write gate for 44.5 s. NR166 R4a made the node's background MODEL
calls wait for an answer job (`answer_gate.background_work`); the builds did not wait. Pinned here:

- the switch is off by default, bound and unbound, and off nothing waits (`hold_background_start` is 0.0 at once);
- on, a search-index build the owner-change queue or the refresh loop's restore would start, and an automatic
  graph rebuild, start only once the answer job has ended, or after the same 110 s bound as every other wait;
- a build already running is never stopped, and an ended share's removal never waits;
- nothing waits inside an answer job (the job counts itself, so a wait there would wait for itself), on a thread
  that runs an event loop, while the thread holds the node's write gate, or in the owner's own work;
- the owner's own graph rebuild (`run_graph_rebuild`, which the owner's endpoint and the upgrade steps call) does
  not wait.

No model is called; no build here reaches a model host.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from topos.permissions_v2 import answer_gate, switches
from topos.permissions_v2.canonical import PolicyError

pytestmark = pytest.mark.public

SWITCH = "TOPOS_PERMISSIONS_V2_ANSWER_HOLDS_BACKGROUND"


@pytest.fixture(autouse=True)
def no_answer_left_active(monkeypatch):
    # 1.5.10's default is on (owner decision D2); each test below says on, or runs with 1.5.9's off.
    monkeypatch.setenv(SWITCH, "false")
    monkeypatch.setattr(answer_gate, "POLL_SECONDS", 0.01)
    assert not answer_gate.active()
    yield
    while answer_gate.active():
        answer_gate.delta(-1)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setenv(SWITCH, "true")


class AnswerJob:
    """An answer job that is active from now until `end()`."""

    def __init__(self):
        answer_gate.delta(1)
        self.ended = False

    def end(self):
        if not self.ended:
            self.ended = True
            answer_gate.delta(-1)

    def end_after(self, seconds: float) -> threading.Timer:
        timer = threading.Timer(seconds, self.end)
        timer.start()
        return timer


# -- the switch -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bound", [False, True], ids=["unbound", "bound"])
def test_the_switch_is_on_bound_and_unbound_and_off_is_one_environment_line(bound, monkeypatch):
    monkeypatch.delenv(SWITCH, raising=False)
    monkeypatch.setattr(switches, "is_bound", lambda env=None: bound)
    item = switches.lookup(SWITCH)
    assert item is switches.ANSWER_HOLDS_BACKGROUND and item.kind == "bool"
    assert item.unbound is True and item.bound is True and switches.on(item) is True
    assert answer_gate.holds_background() is True
    monkeypatch.setenv(SWITCH, "false")
    assert switches.on(item) is False and answer_gate.holds_background() is False


def test_with_nothing_set_it_is_on_and_a_start_waits_for_the_answer_job(monkeypatch):
    """1.5.10's default (owner decision D2): no environment line means the hold is on."""
    monkeypatch.delenv(SWITCH, raising=False)
    job = AnswerJob()
    job.end_after(0.2)
    waited = answer_gate.hold_background_start()
    assert 0.15 <= waited < 2.0 and not answer_gate.active()


def test_off_nothing_waits_even_with_an_answer_job_running():
    AnswerJob()                                                  # never ends
    sleeps = []
    clock = iter(x * 10.0 for x in range(100))
    assert answer_gate.hold_background_start(clock=lambda: next(clock), sleep=sleeps.append) == 0.0 and sleeps == []


# -- the wait -----------------------------------------------------------------------------------------------------------

def test_on_without_an_answer_job_nothing_waits(on):
    assert answer_gate.hold_background_start(sleep=lambda _s: pytest.fail("waited")) == 0.0


def test_on_a_start_waits_until_the_answer_job_ends(on):
    job = AnswerJob()
    job.end_after(0.2)
    waited = answer_gate.hold_background_start()
    assert 0.15 <= waited < 2.0 and not answer_gate.active()


def test_the_wait_is_bounded_by_an_answer_jobs_own_limit_and_logged_without_content(on, caplog):
    AnswerJob()                                                  # never ends
    clock = iter(x * 10.0 for x in range(100))
    sleeps = []

    def sleep(_seconds):
        sleeps.append(1)
        if len(sleeps) > 50:
            pytest.fail("the wait never looked at its limit")

    with caplog.at_level(logging.INFO, logger=answer_gate.__name__):
        waited = answer_gate.hold_background_start(clock=lambda: next(clock), sleep=sleep)
    assert answer_gate.LIMIT_SECONDS == 110.0 and 110.0 <= waited <= 120.0
    [line] = [record.getMessage() for record in caplog.records]
    assert line == f"background build waited {waited:.1f} s for an answer job (the limit; it starts anyway)"


def test_the_limit_and_the_poll_are_read_when_the_wait_starts(on, monkeypatch):
    AnswerJob()
    monkeypatch.setattr(answer_gate, "LIMIT_SECONDS", 0.1)
    started = time.monotonic()
    assert 0.1 <= answer_gate.hold_background_start() < 1.0 and time.monotonic() - started < 1.0


def test_the_owners_own_work_never_waits(on):
    AnswerJob()
    with answer_gate.owners_own_work():
        assert answer_gate.hold_background_start(sleep=lambda _s: pytest.fail("waited")) == 0.0


def test_nothing_waits_inside_an_answer_job_which_would_wait_for_itself(on):
    AnswerJob()                                                  # the job itself is what keeps the count up
    with answer_gate.answer_job():
        assert answer_gate.hold_background_start(sleep=lambda _s: pytest.fail("waited")) == 0.0

        def in_a_worker_thread():
            return answer_gate.hold_background_start(sleep=lambda _s: pytest.fail("waited"))

        async def awaited():
            return await asyncio.to_thread(in_a_worker_thread)   # the mark follows the job's own threads

        assert asyncio.run(awaited()) == 0.0
    assert answer_gate.in_answer_job() is False


def test_a_wait_is_never_taken_on_a_thread_that_runs_an_event_loop(on):
    AnswerJob()

    async def on_the_loop():
        return answer_gate.hold_background_start(sleep=lambda _s: pytest.fail("waited"))

    assert asyncio.run(on_the_loop()) == 0.0


def test_a_wait_is_never_taken_while_this_thread_holds_the_write_gate(on):
    """An answer job takes the write gate (admission, its reads, its re-check): a start that waited while holding it
    would hold the job up for as long as the job holds the start up."""
    from topos.storage.db.write_gate import with_db_write
    AnswerJob()
    with with_db_write():
        assert answer_gate.hold_background_start(sleep=lambda _s: pytest.fail("waited")) == 0.0


def test_a_gate_that_cannot_say_whether_it_is_held_counts_as_held(on, monkeypatch):
    from topos.storage.db import write_gate

    def unreadable():
        raise RuntimeError("no gate here")

    monkeypatch.setattr(write_gate, "db_write_lock", unreadable)
    AnswerJob()
    assert answer_gate.hold_background_start(sleep=lambda _s: pytest.fail("waited")) == 0.0


def test_only_if_says_whether_this_start_waits_at_all(on):
    AnswerJob()
    assert answer_gate.hold_background_start(only_if=lambda: False, sleep=lambda _s: pytest.fail("waited")) == 0.0

    def unreadable():
        raise RuntimeError("the ledger could not say")

    assert answer_gate.hold_background_start(only_if=unreadable, sleep=lambda _s: pytest.fail("waited")) == 0.0
    asked = []
    clock = iter(x * 10.0 for x in range(100))
    assert answer_gate.hold_background_start(only_if=lambda: asked.append(1) or True, clock=lambda: next(clock),
                                             sleep=lambda _s: None) >= 110.0
    assert asked == [1]


def test_only_if_is_not_asked_when_nothing_would_wait(on):
    def never():
        pytest.fail("asked")

    assert answer_gate.hold_background_start(only_if=never) == 0.0              # no answer job
    AnswerJob()
    with answer_gate.owners_own_work():
        assert answer_gate.hold_background_start(only_if=never) == 0.0


# -- the owner-change queue (index_rebuilds) --------------------------------------------------------------------------

class _Ledger:
    """What `IndexRebuilds` reads of the ledger: the owner, the grant's authority (or its absence), question counts."""

    def __init__(self, active: set):
        self.identity = SimpleNamespace(owner_id="owner-invented")
        self.active = active

    def _transaction(self):
        import contextlib
        return contextlib.nullcontext(object())

    def _authority(self, _db, grant_id, _now):
        if grant_id not in self.active:
            raise PolicyError("grant_inactive")
        return SimpleNamespace(policy_hash="policy-" + grant_id), None

    def question_counts(self, now=None):
        return {}


class _Index:
    def __init__(self):
        self.started = []

    def restamp(self, grant_id, now=None):
        return None

    def rebuild(self, grant_id, now=None):
        self.started.append((grant_id, answer_gate.active()))
        return {"state": "ready" if grant_id != "grant-ended" else "removed", "member_count": 1}


def _queue(tmp_path, active):
    from topos.permissions_v2.index_rebuilds import IndexRebuilds
    index = _Index()
    queue = IndexRebuilds(ledger=_Ledger(active), root=tmp_path, index=lambda: index)
    return queue, index


def test_off_the_queue_builds_while_an_answer_job_runs(tmp_path):
    queue, index = _queue(tmp_path, {"grant-a"})
    AnswerJob()
    assert queue._build("grant-a")[0] == "ready" and index.started == [("grant-a", True)]


def test_on_the_queue_starts_a_build_only_once_the_answer_job_has_ended(tmp_path, on):
    queue, index = _queue(tmp_path, {"grant-a"})
    job = AnswerJob()
    job.end_after(0.2)
    state, _count, seconds = queue._build("grant-a")
    assert state == "ready" and index.started == [("grant-a", False)]
    assert seconds < 0.15                                        # the build's own seconds, not the wait's


def test_on_an_ended_shares_removal_never_waits(tmp_path, on, monkeypatch):
    monkeypatch.setattr(answer_gate, "LIMIT_SECONDS", 1.0)
    queue, index = _queue(tmp_path, {"grant-a"})
    AnswerJob()                                                  # never ends
    started = time.monotonic()
    assert queue._build("grant-ended")[0] == "removed" and index.started == [("grant-ended", True)]
    assert time.monotonic() - started < 0.8


def test_on_the_queue_waits_before_it_takes_the_build_slot(tmp_path, on):
    """The refresh loop's restore takes the same slot; a queue build waiting while holding it would hold the restore
    too, and with it whatever the slot orders."""
    from topos.permissions_v2.search_index import BUILD_SLOT
    queue, _index = _queue(tmp_path, {"grant-a"})
    job = AnswerJob()
    held = []
    builder = threading.Thread(target=queue._build, args=("grant-a",))
    builder.start()
    time.sleep(0.1)
    held.append(BUILD_SLOT.locked())
    job.end()
    builder.join(5)
    assert held == [False] and not builder.is_alive()


# -- the refresh loop's restore, on a real index ------------------------------------------------------------------------

from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: E402,F401 -- the fixture's fixture
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: E402,F401 -- the fixture


def _dropped(legacy, tmp_path, monkeypatch):
    """A node whose one share's index a review change dropped, and a loop that has seen it go."""
    from tests.permissions_v2.test_knowledge_search import node_for
    from tests.permissions_v2.test_refresh_loop import Clock, loop_for, reassess
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    assert node.rebuild() == {"grant-search": "ready"}
    loop = loop_for(node, Clock(node.now[0]))
    loop.observe(node.index)
    reassess(node, identity)
    assert node.index.sweep(now=node.now[0]) == 1
    loop.observe(node.index)
    started = []
    real = node.index.rebuild

    def rebuild(grant_id, **kwargs):
        started.append(answer_gate.active())
        started_at.append(time.monotonic())
        return real(grant_id, **kwargs)

    started_at: list = []
    monkeypatch.setattr(node.index, "rebuild", rebuild)
    loop.started_at = started_at
    return node, loop, started


def test_off_the_restore_builds_while_an_answer_job_runs(legacy, tmp_path, monkeypatch):
    node, loop, started = _dropped(legacy, tmp_path, monkeypatch)
    AnswerJob()
    receipt = loop.run_pending()
    assert started == [True] and [g.state for g in receipt.grants] == ["ready"]


def test_on_the_restore_builds_once_the_answer_job_has_ended(legacy, tmp_path, monkeypatch):
    node, loop, started = _dropped(legacy, tmp_path, monkeypatch)
    monkeypatch.setenv(SWITCH, "true")
    job = AnswerJob()
    job.end_after(0.3)
    receipt = loop.run_pending()
    assert started == [False] and [g.state for g in receipt.grants] == ["ready"]
    output, refused = node.search_request("Synthetic message", k=10)
    assert refused is None and len(output["records"]) == 1      # the share serves again, as without the switch


def test_on_the_restore_waits_before_the_protection_sync_of_its_pass(legacy, tmp_path, monkeypatch):
    node, loop, started = _dropped(legacy, tmp_path, monkeypatch)
    monkeypatch.setenv(SWITCH, "true")
    synced = []
    loop._sync_protection = lambda: synced.append(answer_gate.active()) or True
    job = AnswerJob()
    job.end_after(0.3)
    loop.run_pending()
    assert synced == [False] and started == [False]


def test_on_each_build_of_the_restore_waits_again_for_an_ask_that_came_after_the_pass_began(legacy, tmp_path,
                                                                                            monkeypatch):
    node, loop, started = _dropped(legacy, tmp_path, monkeypatch)
    monkeypatch.setenv(SWITCH, "true")
    jobs = []

    def sync_then_an_ask_arrives():
        job = AnswerJob()
        job.end_after(0.3)
        jobs.append(job)
        return True

    loop._sync_protection = sync_then_an_ask_arrives
    receipt = loop.run_pending()
    assert len(jobs) == 1 and started == [False] and [g.state for g in receipt.grants] == ["ready"]


def test_on_the_restore_of_a_share_that_ended_never_waits(legacy, tmp_path, monkeypatch):
    node, loop, started = _dropped(legacy, tmp_path, monkeypatch)
    monkeypatch.setenv(SWITCH, "true")
    monkeypatch.setattr(answer_gate, "LIMIT_SECONDS", 1.0)
    monkeypatch.setattr(loop, "_policy_hash", lambda grant_id, now: None)   # the ledger no longer has the share
    AnswerJob()                                                  # never ends
    began = time.monotonic()
    loop.run_pending()
    assert started == [True] and loop.started_at[0] - began < 0.8            # neither the pass nor its build waited


# -- the graph rebuild --------------------------------------------------------------------------------------------------

def _graph_conn():
    return sqlite3.connect(":memory:", check_same_thread=False)


def _graph_rebuild(started):
    def rebuild(conn):
        started.append(answer_gate.active())
        return {"edges": 0}
    return rebuild


def test_off_an_automatic_graph_rebuild_starts_while_an_answer_job_runs():
    from topos.features.entities import graph_refresh
    started = []
    AnswerJob()
    assert graph_refresh.rebuild_if_inputs_changed(_graph_conn(), rebuild=_graph_rebuild(started)).get("ran")
    assert started == [True]


def test_on_an_automatic_graph_rebuild_starts_once_the_answer_job_has_ended(on):
    from topos.features.entities import graph_refresh
    started = []
    job = AnswerJob()
    job.end_after(0.2)
    assert graph_refresh.rebuild_if_inputs_changed(_graph_conn(), rebuild=_graph_rebuild(started)).get("ran")
    assert started == [False]


def test_on_a_graph_rebuild_that_waited_still_steps_aside_for_a_derivation_that_began_meanwhile(on, monkeypatch):
    """The cooperative check (never start against a derivation batch) runs after the wait, so a batch that began
    while the rebuild waited still defers it."""
    from topos.enrichment import pipeline_activity
    from topos.features.entities import graph_refresh
    from topos.storage.db.write_gate import WriteGateDeferred
    deriving = []
    monkeypatch.setattr(pipeline_activity, "is_derivation_in_flight", lambda: bool(deriving))
    job = AnswerJob()

    def end_and_derive():
        deriving.append(1)
        job.end()

    threading.Timer(0.2, end_and_derive).start()
    started = []
    with pytest.raises(WriteGateDeferred):
        graph_refresh.rebuild_if_inputs_changed(_graph_conn(), rebuild=_graph_rebuild(started))
    assert started == []


def test_on_a_graph_rebuild_already_running_is_never_stopped(on):
    from topos.features.entities import graph_refresh
    finished = []

    def rebuild(conn):
        AnswerJob()                                              # an ask arrives while the rebuild runs
        finished.append(answer_gate.active())
        return {"edges": 0}

    began = time.monotonic()
    assert graph_refresh.rebuild_if_inputs_changed(_graph_conn(), rebuild=rebuild).get("ran")
    assert finished == [True] and time.monotonic() - began < 1.0


def test_on_the_owners_own_graph_rebuild_does_not_wait(on, monkeypatch):
    """`run_graph_rebuild` is what the owner's endpoint calls: no hold inside it (the upgrade steps hold at their own
    call, tested below)."""
    from topos.features.entities import maintenance, rebuild_subprocess
    started = []
    monkeypatch.setattr(maintenance, "rebuild_entity_graph", lambda conn: started.append(answer_gate.active()) or {})
    AnswerJob()                                                  # never ends
    began = time.monotonic()
    rebuild_subprocess.run_graph_rebuild(_graph_conn())          # in-memory: the in-process path
    assert started == [True] and time.monotonic() - began < 1.0


# -- the answer job itself: no deadlock ---------------------------------------------------------------------------------

def test_an_answer_job_never_waits_on_itself(legacy, tmp_path, monkeypatch, on):
    """Inside a running job the count includes that job, so a start the job itself made would wait for its own end.
    The job runs inside `answer_job()`: the start inside it does not wait (without the mark it would wait the limit)."""
    from tests.permissions_v2.message_search_harness import owner as as_owner
    from tests.permissions_v2.test_answer_release import _ask, _finished, _service
    from tests.permissions_v2.test_knowledge_search import node_for
    node, _identity = node_for(legacy, tmp_path, monkeypatch, answers="only")
    with as_owner():
        node.index.rebuild("grant-search", now=node.now[0])
    service = _service(node, "unused")
    waits = []

    async def generate(_prompt, *, deadline):
        waits.append((answer_gate.active(), await asyncio.to_thread(answer_gate.hold_background_start, limit=0.5)))
        return "The owner is working on a synthetic task [1]."

    service.generate = generate
    try:
        answer_id = _ask(node, service, "Synthetic message")
        assert _finished(node, service, answer_id)["outcome"] == "answered"
    finally:
        service.close()
    assert waits == [(True, 0.0)]


# -- WS0 review (15 Oct): shutdown, upgrade steps, and the review's surviving mutants -----------------------------------

def test_a_wait_ends_at_once_when_the_node_stops(on, monkeypatch):
    """A stop (the runtime generation the wait began under is told to stop) ends the wait and wakes its sleep: with a
    3 s poll the hold still returns within a fraction of a second."""
    from topos import runtime_shutdown
    run = runtime_shutdown.RuntimeGeneration(10_001)
    monkeypatch.setattr(runtime_shutdown, "current_generation", lambda: run)
    monkeypatch.setattr(answer_gate, "LIMIT_SECONDS", 6.0)
    monkeypatch.setattr(answer_gate, "POLL_SECONDS", 3.0)
    AnswerJob()                                                  # never ends
    threading.Timer(0.2, run.request_stop, args=("an invented stop",)).start()
    began = time.monotonic()
    waited = answer_gate.hold_background_start()
    assert 0.15 <= waited < 1.5 and time.monotonic() - began < 1.5


def test_a_start_after_the_node_stopped_does_not_wait(on, monkeypatch):
    from topos import runtime_shutdown
    run = runtime_shutdown.RuntimeGeneration(10_002)
    run.request_stop("an invented stop")
    monkeypatch.setattr(runtime_shutdown, "current_generation", lambda: run)
    AnswerJob()
    assert answer_gate.hold_background_start(sleep=lambda _s: pytest.fail("waited")) < 0.1


def _upgrade_step(which):
    from topos.upgrades import runner
    if which == "engine_endpoint":
        return runner._exec_engine_endpoint({"params": {"path": "/v1/signal/entities/graph/rebuild"}}, _graph_conn())
    return runner._exec_derived_rebuild({"params": {"targets": ["entity_graph"]}}, _graph_conn())


@pytest.mark.parametrize("which", ["engine_endpoint", "derived_rebuild"])
def test_off_an_upgrade_steps_graph_rebuild_starts_while_an_answer_job_runs(which, monkeypatch):
    from topos.features.entities import rebuild_subprocess
    started = []
    monkeypatch.setattr(rebuild_subprocess, "run_graph_rebuild", lambda conn: started.append(answer_gate.active()) or {})
    AnswerJob()
    _upgrade_step(which)
    assert started == [True]


@pytest.mark.parametrize("which", ["engine_endpoint", "derived_rebuild"])
def test_on_an_upgrade_steps_graph_rebuild_starts_once_the_answer_job_has_ended(which, on, monkeypatch):
    """Upgrade steps are the node's own background work (WS0 review), not the owner's."""
    from topos.features.entities import rebuild_subprocess
    started = []
    monkeypatch.setattr(rebuild_subprocess, "run_graph_rebuild", lambda conn: started.append(answer_gate.active()) or {})
    job = AnswerJob()
    job.end_after(0.2)
    _upgrade_step(which)
    assert started == [False]


def test_on_work_marked_background_holds_too(on):
    job = AnswerJob()
    job.end_after(0.2)
    with answer_gate.background_work():
        assert answer_gate.hold_background_start() >= 0.15


def test_on_the_pipelines_inline_graph_fill_holds(on):
    """The pipeline's inline fill runs the graph rebuild check in a worker thread of a pipeline job, which job_runner
    marks as background work: the mark must not exempt it."""
    from topos.features.entities import graph_refresh
    started = []
    job = AnswerJob()
    job.end_after(0.2)

    async def a_pipeline_job():
        with answer_gate.background_work():
            return await asyncio.to_thread(graph_refresh.rebuild_if_inputs_changed, _graph_conn(),
                                           rebuild=_graph_rebuild(started))

    assert asyncio.run(a_pipeline_job()).get("ran") and started == [False]


def test_on_each_build_of_the_restore_waits_with_the_build_slot_free(legacy, tmp_path, monkeypatch):
    """The per-build hold comes before the slot: a build waiting for an ask must not hold up the owner-change queue."""
    from topos.permissions_v2.search_index import BUILD_SLOT
    node, loop, started = _dropped(legacy, tmp_path, monkeypatch)
    monkeypatch.setenv(SWITCH, "true")
    seen = []

    def sync_then_an_ask_arrives():
        job = AnswerJob()

        def look_then_end():
            seen.append(BUILD_SLOT.locked())
            job.end()

        threading.Timer(0.2, look_then_end).start()
        return True

    loop._sync_protection = sync_then_an_ask_arrives
    loop.run_pending()
    assert seen == [False] and started == [False]


def test_on_a_pass_with_an_ended_share_and_a_live_one_still_waits(legacy, tmp_path, monkeypatch):
    """The pass waits when ANY due share is still shared, not only when all are."""
    import copy
    node, loop, started = _dropped(legacy, tmp_path, monkeypatch)
    monkeypatch.setenv(SWITCH, "true")
    with loop._lock:
        loop._pending["grant-ended"] = copy.deepcopy(loop._pending["grant-search"])
    real = loop._policy_hash
    monkeypatch.setattr(loop, "_policy_hash",
                        lambda grant_id, now: None if grant_id == "grant-ended" else real(grant_id, now))
    synced = []
    loop._sync_protection = lambda: synced.append(answer_gate.active()) or True
    job = AnswerJob()
    job.end_after(0.3)
    loop.run_pending()
    assert synced == [False]
