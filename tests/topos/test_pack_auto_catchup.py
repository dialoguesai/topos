"""The automatic catch-up for fact packs (`features/derivation/auto_catchup`, the switch routes in `api/signal`).

protects: with the owner's standing switch off (the default) the node never starts a backfill run by itself. With it
on, new journal entries or messages from any source, a quiet period with no arrivals and a minimum gap since the last
look start exactly ONE run on the confirmed-run machinery, over the switched-on packs that have something waiting and
over recent records only: what landed since the last look, never the older history. It never starts beside another
run or a single batch; the owner's backfill is never refused because of it (the automatic run stops after the record
in hand and the owner's run starts then). It yields to the owner's requests between records with nothing open on its
connection, holds no write transaction across a model call, and every bookkeeping write is a gated, committed write.
A look that finds nothing waiting loads and calls no model. A node restart ends the run and the next look picks the
same window up again. Only the owner turns the switch: the route is refused 403 without the app or a live
confirmation, for off as for on, and reading it is a read.

The model is a stub (``OllamaAdapter._generate``); databases are scratch files under ``tmp_path``; the clock and the
wall time of each look are passed in.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from tests.topos.test_pack_backfill_runs import HIT, MISS, _Classifier, _Model, _mounted, _pick_topos_facts, _Script
from topos import owner_door_routes as census, uds
from topos.features.derivation import auto_catchup as auto
from topos.features.derivation import backfill_runs as runs
from topos.features.derivation import surfaces
from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.db.write_gate import db_write_lock

JOY = "moments.joy"
EVENTS = "life.events"
AUTO = "/v1/signal/derivation/backfill/auto"
REFUSAL = {"error": "owner_confirmation_required", "message": "This change needs the Topos app."}

#: Entry n of the fixture is dated 9 - n days before the test's `now`: j0 is nine days old, j9 is today's.
HITS = (1, 2, 4, 5, 7, 8)
#: The first look reads seven days back: j2..j9, of which these pass the moments.joy prefilter.
FIRST_WINDOW_HITS = [2, 4, 5, 7, 8]
#: The wall time every test starts at. The looks move it; nothing reads the machine's clock.
NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
QUIET = auto.DEFAULT_QUIET_S
GAP = auto.DEFAULT_MIN_GAP_S


def _open(path) -> sqlite3.Connection:
    return sqlite3.connect(path, check_same_thread=False)


def _stamp(now: datetime, days_back: int) -> str:
    return (now - timedelta(days=days_back)).strftime("%Y-%m-%dT09:00:00Z")


def _day(now: datetime, days_back: int) -> str:
    return (now - timedelta(days=days_back)).date().isoformat()


def _add(conn: sqlite3.Connection, n: int, now: datetime, *, days_back: int = 0, hit: bool = True,
         source: str = "grow_journal") -> None:
    conn.execute("INSERT INTO journal_entries (entry_id, entry_at, content, source_id, source_record_id)"
                 " VALUES (?, ?, ?, ?, ?)",
                 (f"j{n}", _stamp(now, days_back), (HIT if hit else MISS).format(n=n), source, f"r{n}"))
    conn.commit()


@pytest.fixture
def node(tmp_path):
    """A scratch node: an owner, moments.joy switched on (and nothing else), ten journal entries dated one a day
    up to today, six of which the moments.joy prefilter passes."""
    path = tmp_path / "node.db"
    conn = _open(path)
    apply_all_migrations(conn)
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, is_self)"
                 " VALUES ('ent_owner','person','Owner','owner','[]',1)")
    conn.execute("CREATE TABLE IF NOT EXISTS engine_config (key TEXT PRIMARY KEY, value TEXT)")
    seed_pack_registry(conn, bundled_pack_dir())
    conn.execute("UPDATE pack_registry SET enabled=0")
    conn.execute("UPDATE pack_registry SET enabled=1 WHERE pack_id=?", (JOY,))
    conn.commit()
    now = NOW
    for n in range(10):
        _add(conn, n, now, days_back=9 - n, hit=n in HITS)
    yield SimpleNamespace(conn=conn, path=path, now=now)
    conn.close()


@pytest.fixture
def model(monkeypatch):
    from topos.engine.backends import ollama

    stub = _Model()
    monkeypatch.setattr(ollama.OllamaAdapter, "_generate", lambda self, m, p, **kw: stub(self, m, p, **kw))
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model", lambda s, c=None: "stub-model")
    monkeypatch.setattr("topos.features.signal.derived_index.index_derived_objects", lambda *a, **kw: {"embedded": 0})
    return stub


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Each test starts with no run, a watch that has seen nothing and no environment override."""
    for name in (auto.KILL_ENV, auto.QUIET_ENV, auto.MIN_GAP_ENV):
        monkeypatch.delenv(name, raising=False)

    def reset():
        for run in (runs._active, runs._last):
            if run is not None:
                run.request_stop()
                if run.thread is not None:
                    run.thread.join(timeout=10)
        runs._active = None
        runs._last = None
        runs._single_batches = 0
        auto._reset_for_tests()

    reset()
    yield
    reset()


def _until(check: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + 15
    while not check():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.005)


class _InHand:
    """A batch runner whose record in hand lasts until `release` is set: a model call in flight does not end
    early because a stop was asked."""

    def __init__(self, records: int) -> None:
        self.records = records
        self.working = threading.Event()
        self.release = threading.Event()

    def __call__(self, pack_id, limit, should_stop, on_record, since=None) -> Dict[str, Any]:
        stats = {"processed": 0, "assertions": 0, "accepted": 0, "written": 0, "quarantined": 0, "llm_errors": 0,
                 "extract_s": 0.0, "verify_s": 0.0, "extract_model": "stub-reader", "verify_model": None}
        while self.records > 0 and stats["processed"] < limit:
            if should_stop():
                return {**stats, "stopped": True}
            self.working.set()
            assert self.release.wait(10), "the test never released the record in hand"
            self.records -= 1
            stats["processed"] += 1
            stats["written"] += 1
            on_record(dict(stats))
        return stats


class _Looks:
    """The scheduler's looks, driven by hand: a clock and a wall time that move only when the test says so, real
    batches on a connection of their own per batch, and the run's end recorded on a connection of its own."""

    def __init__(self, node, on: bool = True) -> None:
        self.node = node
        self.t = 0.0
        self.now = node.now
        self.live: List[sqlite3.Connection] = []
        self.gated: List[bool] = []
        self._opened: List[sqlite3.Connection] = []
        node.conn.set_trace_callback(self._trace)
        if on:
            auto.set_switch(node.conn, True, now=self.now)

    def _trace(self, statement: str) -> None:
        if "INTO engine_config" in statement and "packs_auto_catchup" in statement:
            self.gated.append(db_write_lock()._is_owned())

    def batch(self, pack_id, limit, should_stop, on_record, since=None) -> Dict[str, Any]:
        conn = _open(self.node.path)
        self.live.append(conn)
        try:
            return surfaces.run_pack_backfill(conn, pack_id, limit, should_stop=should_stop, on_record=on_record,
                                              since=since)
        finally:
            self.live.remove(conn)
            conn.close()

    def _factory(self) -> sqlite3.Connection:
        conn = _open(self.node.path)
        conn.set_trace_callback(self._trace)
        self._opened.append(conn)
        return conn

    def look(self, after: float = 0.0, *, run_batch: Any = None) -> Dict[str, Any]:
        self.t += after
        self.now += timedelta(seconds=after)
        return auto.tick(self.node.conn, wall=lambda: self.now, clock=lambda: self.t,
                         run_batch=run_batch or self.batch, conn_factory=self._factory)

    def start(self, *, run_batch: Any = None) -> Dict[str, Any]:
        """The looks a node makes from its start to its first run: the first sight of the tables, then the look
        after the quiet period and the gap."""
        first = self.look(run_batch=run_batch)
        assert first["decision"] in ("ingest_busy", "min_gap"), first
        out = self.look(max(QUIET, GAP) + 5, run_batch=run_batch)
        assert out["decision"] == "started", out
        return out

    def ended(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        """Wait for the run and for its end to be recorded; the bookkeeping's `last`."""
        def recorded() -> bool:
            last = auto.read_state(self.node.conn).get("last") or {}
            return last.get("state") != "running" and (run_id is None or last.get("run_id") == run_id)

        _until(recorded, "the run's end to be recorded")
        _until(lambda: not runs.busy(), "the slot to be free")
        return auto.read_state(self.node.conn)["last"]

    def close(self) -> None:
        self.node.conn.set_trace_callback(None)
        for conn in self._opened:
            conn.close()


@pytest.fixture
def looks(node):
    made: List[_Looks] = []

    def make(on: bool = True) -> _Looks:
        made.append(_Looks(node, on))
        return made[-1]

    yield make
    for one in made:
        one.close()


# ----------------------------------------------------------------------------- off means off

@pytest.mark.parametrize("change,decision", [
    ("owner_off", "off"), ("kill_switch", "kill_switch"), ("longer_quiet", "switch_changed"),
])
def test_switch_changed_while_counting_cannot_start_a_run(node, looks, monkeypatch, change, decision):
    """Counting pending records can take time; owner changes during it must win before any run starts."""
    watch = looks()
    assert watch.look()["decision"] == "ingest_busy"
    waiting = auto.waiting_by_pack

    def count_then_change(conn, since):
        counts = waiting(conn, since)
        assert counts[JOY] > 0
        if change == "owner_off":
            auto.set_switch(conn, False, now=watch.now)
        elif change == "kill_switch":
            monkeypatch.setenv(auto.KILL_ENV, "off")
        else:
            auto.set_switch(conn, True, quiet_s=24 * 3600, now=watch.now)
        return counts

    monkeypatch.setattr(auto, "waiting_by_pack", count_then_change)
    monkeypatch.setattr(runs, "start_run", lambda *a, **kw: pytest.fail("started after the switch changed"))
    assert watch.look(GAP + 5)["decision"] == decision
    assert not runs.busy()


def test_off_is_the_default_and_a_look_then_starts_nothing(node, model, looks) -> None:
    sched = looks(on=False)
    assert auto.read_switch(node.conn)["enabled"] is False
    before = node.conn.total_changes
    for after in (0, QUIET, GAP, 86400):
        assert sched.look(after) == {"decision": "off"}
    assert runs.current() is None and model.extract == [] and model.verify == []
    assert node.conn.total_changes == before and auto.read_state(node.conn) == {}, "an off look writes nothing"
    assert auto._watch.marker is None, "and does not even look at the record tables"


def test_the_environment_switch_holds_it_off_whatever_the_owner_set(node, model, looks, monkeypatch) -> None:
    sched = looks()
    monkeypatch.setenv(auto.KILL_ENV, "off")
    for after in (0, QUIET + GAP):
        assert sched.look(after) == {"decision": "kill_switch"}
    assert runs.current() is None and model.extract == []
    described = auto.describe(node.conn)
    assert described["kill_switch"] is True and described["enabled"] is True
    assert described["next"] == {"state": "kill_switch", "not_before": None}
    assert auto.start_scheduler(lambda coro, name: pytest.fail("the loop was started"), lambda: node.conn) is False


def test_the_loop_is_started_by_name_and_looks_on_a_worker_thread(node, monkeypatch) -> None:
    spawned: List[str] = []

    def spawn(coro, name):
        spawned.append(name)
        coro.close()

    assert auto.start_scheduler(spawn, lambda: node.conn) is True and spawned == ["packs-auto-catchup"]

    seen: List[tuple] = []
    monkeypatch.setattr(auto, "tick", lambda conn: seen.append((threading.current_thread().name, conn)) or {})

    async def two_looks() -> None:
        task = asyncio.create_task(auto.run_scheduler_loop(lambda: node.conn, tick_seconds=0.01,
                                                           startup_delay_seconds=0.0))
        while len(seen) < 2:
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(two_looks())
    assert all(conn is node.conn for _name, conn in seen)
    assert all(name != threading.current_thread().name for name, _conn in seen), "never on the event loop's thread"


@pytest.mark.asyncio
async def test_the_app_starts_the_loop_at_startup_and_reaps_it_at_shutdown(monkeypatch) -> None:
    from topos.app import app
    from topos.core import state
    from topos.testing.lifespan import LifespanManager

    monkeypatch.setenv("TOPOS_KEY", "test-key")
    async with LifespanManager(app):
        mine = [task for task in state.background_tasks if task.get_name() == "packs-auto-catchup"]
        assert len(mine) == 1 and not mine[0].done(), "the loop is one tracked startup task"
    assert mine[0].done(), "and shutdown ends it with the others"


# ----------------------------------------------------------------------------- on: one run, recent records only


def test_new_records_and_a_quiet_period_start_exactly_one_run(node, model, looks) -> None:
    sched = looks()
    assert sched.look()["decision"] == "ingest_busy", "the node's first sight of the tables counts as an arrival"
    assert sched.look(QUIET - 1) == {"decision": "ingest_busy", "wait_s": 1.0}
    assert runs.current() is None and model.extract == []
    started = sched.look(1)
    assert started["decision"] == "started" and started["since"] == _day(node.now, auto.FIRST_WINDOW_DAYS)
    assert started["packs"] == 1 and started["waiting"] == len(FIRST_WINDOW_HITS) and started["new_records"] is None
    last = sched.ended(started["run_id"])
    assert last["state"] == "done" and last["totals"]["processed"] == len(FIRST_WINDOW_HITS)
    assert last["totals"]["written"] >= 1 and last["finished_at"]
    assert sorted(model.entries()) == FIRST_WINDOW_HITS, "one extraction call per waiting record of the window"
    snap = runs.current()
    assert snap["run_id"] == started["run_id"] and snap["origin"] == "auto" and snap["until_done"] is True
    assert snap["pack_ids"] == [JOY] and snap["since"] == started["since"]

    for after in (0, 60, QUIET, GAP, 86400):
        assert sched.look(after) == {"decision": "no_new_records"}
    assert runs.current()["run_id"] == started["run_id"], "no second run without new records"
    assert sorted(model.entries()) == FIRST_WINDOW_HITS


def test_a_later_look_reads_only_what_landed_since(node, model, looks) -> None:
    sched = looks()
    first = sched.ended(sched.start()["run_id"])
    model.extract.clear()
    _add(node.conn, 20, sched.now, hit=True)
    _add(node.conn, 21, sched.now, hit=False)
    assert sched.look(60)["decision"] == "ingest_busy", "the arrival restarts the quiet period"
    assert sched.look(QUIET + GAP)["decision"] == "started"
    second = sched.ended()
    assert second["run_id"] != first["run_id"] and second["state"] == "done"
    assert (second["new_records"], second["older_records"]) == (2, 0)
    assert second["since"] == _day(NOW, auto.OVERLAP_DAYS), "the day before the last look that read everything"
    assert model.entries() == [20], "the new record that the prefilter passes, and none of the history"
    marked = {r[0].rsplit(":", 1)[1] for r in node.conn.execute(
        "SELECT key FROM derivation_progress WHERE key LIKE ?", (f"{JOY}@%",))}
    assert not {"j0", "j1"} & marked, "records older than the first window are neither read nor marked"
    assert runs.backfill_plan(node.conn, [JOY])["packs"][0]["waiting"] == 1, \
        "j1 is history: it still waits for a manual backfill"


def test_a_record_that_lands_late_with_an_older_date_is_counted_and_left_for_a_backfill(node, model, looks) -> None:
    """The window is not widened for a late record: that would also read every unread record of those days (j1
    here), which is history the standing yes does not cover."""
    sched = looks()
    sched.ended(sched.start()["run_id"])
    model.extract.clear()
    _add(node.conn, 30, sched.now, days_back=4, hit=True)       # synced four days late
    _add(node.conn, 31, sched.now, hit=True)
    sched.look(60)
    started = sched.look(QUIET + GAP)
    assert started["decision"] == "started" and (started["new_records"], started["older_records"]) == (2, 1)
    last = sched.ended(started["run_id"])
    assert last["since"] == _day(NOW, auto.OVERLAP_DAYS) and last["older_records"] == 1
    assert model.entries() == [31], "the record inside the window is read; the late one is not, nor is j1"
    assert sched.look(QUIET + GAP) == {"decision": "no_new_records"}, "and it does not trigger look after look"
    assert runs.backfill_plan(node.conn, [JOY])["packs"][0]["waiting"] == 2, "j1 and the late record wait"


def test_the_window_never_reaches_past_the_cap_however_long_the_node_was_off(node) -> None:
    covered = {"covered": {"journal_entries": 10}}
    day = lambda back: _day(NOW, back)  # noqa: E731
    assert auto.window_start({}, NOW) == day(auto.FIRST_WINDOW_DAYS)
    assert auto.window_start({"base_at": auto._iso(NOW)}, NOW) == day(auto.FIRST_WINDOW_DAYS), "no marks, first window"
    assert auto.window_start({**covered, "base_at": auto._iso(NOW - timedelta(days=3))}, NOW) == day(4)
    assert auto.window_start({**covered, "base_at": auto._iso(NOW - timedelta(days=200))}, NOW) == day(
        auto.MAX_WINDOW_DAYS)


def test_a_message_from_a_source_with_no_lane_of_its_own_is_covered(node, model, looks) -> None:
    """The trigger and the walk read the two record tables, not a source's lane: a record an app pushed is seen."""
    sched = looks()
    sched.ended(sched.start()["run_id"])
    model.extract.clear()
    node.conn.execute("CREATE TABLE IF NOT EXISTS conversation_messages (message_id TEXT PRIMARY KEY,"
                      " conversation_id TEXT, content TEXT, event_at TEXT, actor_role TEXT, is_from_self INTEGER,"
                      " sender_id TEXT, sender_type TEXT, source_id TEXT, dataset_id TEXT)")
    node.conn.execute("INSERT INTO conversation_messages VALUES ('m1', 'c1', ?, ?, 'authored', 1, 'self', 'human',"
                      " 'an_app_that_pushes', 'd')", (HIT.format(n=40), _stamp(sched.now, 0)))
    node.conn.commit()
    sched.look(60)
    started = sched.look(QUIET + GAP)
    assert started["decision"] == "started" and started["new_records"] == 1
    assert sched.ended()["state"] == "done" and model.entries() == [40]


def test_the_minimum_gap_holds_the_next_look_back(node, model, looks) -> None:
    sched = looks()
    sched.ended(sched.start()["run_id"])
    model.extract.clear()
    _add(node.conn, 20, sched.now, hit=True)
    assert sched.look(60)["decision"] == "ingest_busy"
    held = sched.look(QUIET)
    assert held == {"decision": "min_gap", "wait_s": float(GAP - 60 - QUIET)}, "quiet, but the last look is recent"
    described = auto.describe(node.conn, now=sched.now, clock=lambda: sched.t)
    assert described["next"] == {"state": "waiting_for_gap",
                                 "not_before": auto._iso(sched.now + timedelta(seconds=GAP - 60 - QUIET))}
    assert sched.look(GAP - 60 - QUIET - 1)["decision"] == "min_gap"
    assert runs.current()["origin"] == "auto" and model.extract == []
    assert sched.look(1)["decision"] == "started"
    sched.ended()
    assert model.entries() == [20]


def test_the_two_periods_are_the_owners_then_the_nodes_then_the_defaults(node, looks, monkeypatch) -> None:
    assert (auto.read_switch(node.conn)["quiet_s"], auto.read_switch(node.conn)["min_gap_s"]) == (600, 3600)
    monkeypatch.setenv(auto.QUIET_ENV, "120")
    monkeypatch.setenv(auto.MIN_GAP_ENV, "900")
    assert (auto.read_switch(node.conn)["quiet_s"], auto.read_switch(node.conn)["min_gap_s"]) == (120, 900)
    monkeypatch.setenv(auto.QUIET_ENV, "1")                       # under the floor: clamped, never zero
    assert auto.read_switch(node.conn)["quiet_s"] == auto.QUIET_RANGE_S[0]
    auto.set_switch(node.conn, True, quiet_s=300, min_gap_s=7200)
    switch = auto.read_switch(node.conn)
    assert (switch["quiet_s"], switch["min_gap_s"], switch["quiet_set"], switch["min_gap_set"]) == (
        300, 7200, True, True)
    auto.set_switch(node.conn, False)
    assert auto.read_switch(node.conn)["quiet_s"] == 300, "turning it off keeps the owner's periods"
    for bad in ({"quiet_s": 5}, {"quiet_s": "600"}, {"min_gap_s": 10}, {"min_gap_s": True}):
        with pytest.raises(auto.AutoRefused):
            auto.set_switch(node.conn, True, **bad)
    with pytest.raises(auto.AutoRefused):
        auto.set_switch(node.conn, "yes")
    assert auto.read_switch(node.conn)["enabled"] is False, "a refused change changes nothing"


def test_the_owners_quiet_period_is_the_one_a_look_uses(node, model, looks) -> None:
    sched = looks(on=False)
    auto.set_switch(node.conn, True, quiet_s=60, min_gap_s=300, now=sched.now)
    assert sched.look()["decision"] == "ingest_busy"
    assert sched.look(59)["decision"] == "ingest_busy"
    assert sched.look(1)["decision"] == "started"
    sched.ended()


# ----------------------------------------------------------------------------- nothing waiting: no model


def test_nothing_waiting_starts_no_run_and_loads_no_model(node, model, looks, monkeypatch) -> None:
    from topos.features.derivation import topos_facts

    sched = looks()
    sched.ended(sched.start()["run_id"])
    first_run = runs.current()["run_id"]
    model.extract.clear()
    model.verify.clear()
    _pick_topos_facts(node.conn)                        # a run would load Topos Facts at its first batch
    loads: List[str] = []
    monkeypatch.setattr(topos_facts, "load", lambda *a, **kw: loads.append("load") or _Classifier())
    _add(node.conn, 50, sched.now, hit=False)           # lands, and no switched-on pack's prefilter passes it
    sched.look(60)
    look = sched.look(QUIET + GAP)
    assert look["decision"] == "nothing_waiting" and look["new_records"] == 1 and look["waiting"] == 0
    assert runs.current()["run_id"] == first_run and not runs.busy(), "no run was started"
    assert loads == [] and model.extract == [] and model.verify == [], "no model was loaded or called"
    last = auto.read_state(node.conn)["last"]
    assert last["state"] == "nothing_waiting" and last["run_id"] is None and last["totals"] is None
    assert sched.look(QUIET + GAP) == {"decision": "no_new_records"}, "what landed is covered"
    assert loads == []


def test_the_look_counts_with_the_plans_rule(node, model) -> None:
    """One counting rule, asked two ways: a key range per pack for the plan, a key per record for the look."""
    from topos.features.derivation.packs import load_packs

    node.conn.execute("UPDATE pack_registry SET enabled=1 WHERE pack_id=?", (EVENTS,))
    node.conn.commit()
    surfaces.run_pack_backfill(node.conn, JOY, limit=2)
    catalog = load_packs(bundled_pack_dir(), only=[JOY, EVENTS])
    for since in (None, _day(node.now, 5)):
        ranged = runs.waiting_counts(node.conn, catalog, [JOY, EVENTS], since)
        asked = runs.waiting_counts(node.conn, catalog, [JOY, EVENTS], since, per_record=True)
        assert ranged == asked, since
        plan = {row["pack_id"]: row["waiting"] for row in runs.backfill_plan(node.conn, [JOY, EVENTS], since)["packs"]}
        assert plan == ranged[0]
    assert auto.waiting_by_pack(node.conn, _day(node.now, 5)) == asked[0]
    before = node.conn.total_changes
    auto.waiting_by_pack(node.conn, _day(node.now, 5))
    assert node.conn.total_changes == before and not node.conn.in_transaction, "counting writes nothing"


# ----------------------------------------------------------------------------- one slot; the owner's backfill wins


def test_it_does_not_start_beside_an_owners_run_or_a_single_batch(node, model, looks, monkeypatch) -> None:
    sched = looks()
    sched.look()
    counted: List[str] = []
    count = auto.waiting_by_pack
    monkeypatch.setattr(auto, "waiting_by_pack", lambda conn, since: counted.append(since) or count(conn, since))
    script = _Script({"a.one": 2})
    script.hold.clear()
    manual = runs.start_run(["a.one"], 2, True, run_batch=script)
    _until(lambda: script.calls, "the owner's run to be working")
    for after in (QUIET + GAP, 60, 3600):
        assert sched.look(after) == {"decision": "run_active"}
    assert runs.current()["run_id"] == manual.run_id and model.extract == []
    script.hold.set()
    manual.thread.join(timeout=10)
    with runs.single_batch():
        assert sched.look(60) == {"decision": "run_active"}
    assert runs.current()["run_id"] == manual.run_id
    assert counted == [], "a look that finds the slot taken does not even count: it reads no record text"
    assert sched.look(60)["decision"] == "started", "and it starts once the slot is free"
    sched.ended()


def test_an_owners_run_starts_as_usual_while_the_automatic_one_is_only_waiting(node, model, looks) -> None:
    sched = looks()
    assert sched.look()["decision"] == "ingest_busy"
    script = _Script({"a.one": 3})
    manual = runs.start_run(["a.one"], 2, True, run_batch=script)
    manual.thread.join(timeout=10)
    snap = manual.snapshot()
    assert snap["state"] == "done" and snap["origin"] == "owner" and snap["waiting_for"] is None
    assert script.calls == [("a.one", 2), ("a.one", 2)]


def test_the_owners_run_wins_over_an_automatic_run_that_is_working(node, model, looks) -> None:
    """Not a refusal: the automatic run stops after the record in hand and the owner's run starts then."""
    sched = looks()
    automatic = _InHand(5)
    started = sched.start(run_batch=automatic)
    _until(automatic.working.is_set, "the automatic run to have a record in hand")
    auto_run = runs._active
    owners = _Script({"a.one": 3})
    beside: List[bool] = []

    def owners_batch(*args, **kwargs):
        beside.append(not auto_run.ended.is_set())
        return owners(*args, **kwargs)

    manual = runs.start_run(["a.one"], 2, True, run_batch=owners_batch)       # no BackfillBusy
    waiting = manual.snapshot()
    assert waiting["waiting_for"] == "automatic_run" and waiting["state"] == "running"
    assert runs.current()["run_id"] == manual.run_id, "the slot is the owner's at once"
    assert auto_run.snapshot()["state"] == "stopping"
    time.sleep(0.05)
    assert owners.calls == [], "its first batch waits for the automatic run's record in hand"
    with pytest.raises(runs.BackfillBusy):
        runs.start_run(["b.two"], 2, False, run_batch=_Script({"b.two": 1}))
    assert sched.look(60) == {"decision": "run_active"}

    automatic.release.set()
    manual.thread.join(timeout=10)
    done = manual.snapshot()
    assert done["state"] == "done" and done["waiting_for"] is None and done["totals"]["processed"] == 3
    assert beside == [False, False], "no batch of the owner's run ran beside the automatic one"
    last = sched.ended(started["run_id"])
    assert (last["state"], last["reason"]) == ("stopped", "owner_backfill")
    assert last["totals"]["processed"] == 1, "the record in hand was finished and kept"
    state = auto.read_state(node.conn)
    assert "covered" not in state and "base_at" not in state, "a stopped run covers nothing"
    again = sched.look(GAP + 5, run_batch=automatic)
    assert again["decision"] == "started" and again["since"] == started["since"], "so the next look does it over"
    assert sched.ended(again["run_id"])["totals"]["processed"] == 4


def test_a_single_batch_waits_for_the_automatic_run_to_stop(node, model, looks) -> None:
    sched = looks()
    automatic = _InHand(5)
    started = sched.start(run_batch=automatic)
    _until(automatic.working.is_set, "the automatic run to have a record in hand")
    assert runs.yield_to_owner(timeout_s=0.05) is False, "asked to stop; still on the record in hand"
    assert runs.current()["state"] == "stopping"
    threading.Timer(0.05, automatic.release.set).start()
    assert runs.yield_to_owner(timeout_s=10) is True
    with runs.single_batch():
        pass
    assert sched.ended(started["run_id"])["reason"] == "owner_backfill"
    held = _Script({"a.one": 1})
    held.hold.clear()
    manual = runs.start_run(["a.one"], 2, False, run_batch=held)
    _until(lambda: held.calls, "the owner's run to be working")
    assert runs.yield_to_owner(timeout_s=0.05) is True, "an owner's run is not asked to stop"
    assert manual.snapshot()["state"] == "running"
    held.hold.set()
    manual.thread.join(timeout=10)
    assert manual.snapshot()["state"] == "done"


def test_packs_stop_and_switching_it_off_stop_the_automatic_run(node, model, looks) -> None:
    sched = looks()
    automatic = _Script({JOY: 5})
    automatic.hold.clear()
    started = sched.start(run_batch=automatic)
    _until(lambda: automatic.calls, "the automatic run to be working")
    assert auto.set_switch(node.conn, False, now=sched.now)["enabled"] is False
    automatic.hold.set()
    last = sched.ended(started["run_id"])
    assert (last["state"], last["reason"]) == ("stopped", "switched_off")
    assert sched.look(GAP + QUIET) == {"decision": "off"}

    auto.set_switch(node.conn, True, now=sched.now)
    automatic.hold.clear()
    calls = len(automatic.calls)
    again = sched.look(GAP + QUIET, run_batch=automatic)
    assert again["decision"] == "started"
    _until(lambda: len(automatic.calls) > calls, "the second automatic run to be working")
    assert runs.stop()["state"] in ("stopping", "stopped")   # `topos-node packs stop`
    automatic.hold.set()
    assert sched.ended(again["run_id"])["reason"] == "stopped"


# ----------------------------------------------------------------------------- how it writes, and how it yields


def test_it_yields_between_records_with_nothing_open_and_no_write_spans_a_model_call(node, model, looks,
                                                                                       monkeypatch) -> None:
    from topos.core import owner_requests

    sched = looks()
    yields: List[tuple] = []
    during: List[bool] = []
    monkeypatch.setattr(owner_requests, "pace", lambda: yields.append(
        (threading.current_thread().name, [c.in_transaction for c in sched.live])) or 0.0)
    model.before = lambda: during.append(any(c.in_transaction for c in sched.live))
    last = sched.ended(sched.start()["run_id"])
    assert last["state"] == "done"
    assert [name for name, _open_txn in yields] == [runs.THREAD_NAME] * len(FIRST_WINDOW_HITS)
    assert all(open_txn == [False] for _name, open_txn in yields), "a yield with a write transaction open"
    assert len(during) == len(model.extract) + len(model.verify) and not any(during), \
        "a model call ran inside the run's open write transaction"


def test_every_bookkeeping_write_is_gated_and_committed(node, model, looks) -> None:
    sched = looks()
    other = _open(node.path)
    try:
        def stored() -> Dict[str, Any]:
            row = other.execute("SELECT value FROM engine_config WHERE key=?",
                                (auto.ENGINE_CONFIG_KEY_STATE,)).fetchone()
            return json.loads(row[0]) if row else {}

        started = sched.start()
        assert not node.conn.in_transaction
        sched.ended(started["run_id"])
        assert stored()["last"]["state"] == "done", "another connection reads the run's end: it was committed"
        _add(node.conn, 50, sched.now, hit=False)
        sched.look(60)
        assert sched.look(QUIET + GAP)["decision"] == "nothing_waiting"
        assert not node.conn.in_transaction and stored()["last"]["state"] == "nothing_waiting"
        auto.set_switch(node.conn, False, now=sched.now)
        assert not node.conn.in_transaction
        assert json.loads(other.execute("SELECT value FROM engine_config WHERE key=?",
                                        (auto.ENGINE_CONFIG_KEY_SWITCH,)).fetchone()[0])["enabled"] is False
    finally:
        other.close()
    # the switch on, the run's start, its end, the look with nothing waiting, the switch off
    assert len(sched.gated) >= 5 and all(sched.gated), "a bookkeeping write was made without the write gate"


def test_the_bookkeeping_holds_dates_states_and_counts_only(node, model, looks) -> None:
    sched = looks()
    sched.ended(sched.start()["run_id"])
    state = auto.read_state(node.conn)
    assert set(state) == {"covered", "base_at", "last"}
    assert set(state["covered"]) <= {"journal_entries", "conversation_messages"}
    assert all(isinstance(v, int) for v in state["covered"].values())
    assert set(state["last"]) == {"run_id", "state", "reason", "started_at", "finished_at", "since", "packs",
                                  "waiting", "new_records", "older_records", "totals"}
    assert set(state["last"]["totals"]) == {"processed", "assertions", "accepted", "written", "quarantined",
                                            "llm_errors", "extract_s", "verify_s"}
    raw = node.conn.execute("SELECT value FROM engine_config WHERE key=?", (auto.ENGINE_CONFIG_KEY_STATE,)).fetchone()[0]
    assert "lake" not in raw and "entry" not in raw, "no record text"


def test_a_run_with_model_errors_covers_what_landed_and_walks_the_window_again(node, model, looks) -> None:
    sched = looks()
    failed: List[int] = []

    def flaky() -> None:
        # The second extraction call: one extraction and its one verifier call have been answered.
        if not failed and len(model.extract) == 1 and len(model.verify) == 1:
            failed.append(1)
            raise RuntimeError("Ollama request failed: timed out")

    model.before = flaky
    last = sched.ended(sched.start()["run_id"])
    assert last["state"] == "done" and last["totals"]["llm_errors"] == 1
    state = auto.read_state(node.conn)
    assert "covered" in state and "base_at" not in state
    assert sched.look(GAP + QUIET) == {"decision": "no_new_records"}, "the failed record alone starts nothing"
    model.before = None
    asked = len(model.extract)
    _add(node.conn, 60, sched.now, hit=False)
    sched.look(60)
    again = sched.look(QUIET + GAP)
    assert again["decision"] == "started" and again["since"] == _day(sched.now, auto.FIRST_WINDOW_DAYS)
    assert sched.ended(again["run_id"])["totals"]["processed"] == 1, "only the record whose call failed is asked again"
    assert len(model.extract) == asked + 1
    assert "base_at" in auto.read_state(node.conn)


# ----------------------------------------------------------------------------- a restart


def test_it_picks_the_same_window_up_after_a_restart(node, model, looks) -> None:
    from topos import runtime_shutdown

    sched = looks()
    model.before = lambda: len(model.extract) >= 2 and runtime_shutdown.request_shutdown("sigterm")
    try:
        started = sched.start()
        last = sched.ended(started["run_id"])
    finally:
        runtime_shutdown.clear_shutdown()
        model.before = None
    assert (last["state"], last["reason"]) == ("stopped", "node_stopping")
    read_before = model.entries()
    assert 0 < len(read_before) < len(FIRST_WINDOW_HITS)
    assert "covered" not in auto.read_state(node.conn), "the unfinished run moved no mark"

    runs._active = runs._last = None           # a new node process: no run, a watch that has seen nothing,
    auto._reset_for_tests()                    # and the switch and the bookkeeping as the database holds them
    assert auto.read_switch(node.conn)["enabled"] is True
    assert sched.look(5)["decision"] == "ingest_busy", "nothing in the first quiet period after a start"
    again = sched.look(QUIET + GAP)
    assert again["decision"] == "started" and again["since"] == started["since"]
    assert sched.ended(again["run_id"])["state"] == "done"
    assert sorted(model.entries()) == FIRST_WINDOW_HITS, "each waiting record was asked exactly once across both"
    assert sched.look(QUIET + GAP) == {"decision": "no_new_records"}


def test_a_run_a_crash_cut_short_reads_as_interrupted_and_is_done_over(node, model, looks) -> None:
    sched = looks()
    automatic = _Script({JOY: 5})
    automatic.hold.clear()
    started = sched.start(run_batch=automatic)
    _until(lambda: automatic.calls, "the automatic run to be working")
    assert auto.describe(node.conn)["last"]["state"] == "running"
    assert auto.describe(node.conn)["next"]["state"] == "run_active"
    crashed = runs._active
    runs._active = runs._last = None           # the process died: the row still says running, nothing runs
    auto._reset_for_tests()
    try:
        assert auto.describe(node.conn)["last"]["state"] == "interrupted"
        sched.look(5)
        again = sched.look(QUIET + GAP)
        assert again["decision"] == "started" and again["since"] == started["since"]
    finally:
        crashed.request_stop()
        automatic.hold.set()
        crashed.thread.join(timeout=10)
    sched.ended(again["run_id"])


# ----------------------------------------------------------------------------- what the owner reads


def test_the_status_says_what_the_last_look_did_and_when_the_next_may_start(node, model, looks) -> None:
    off = auto.describe(node.conn)
    assert off["enabled"] is False and off["last"] is None and off["next"] == {"state": "off", "not_before": None}
    assert (off["quiet_s"], off["min_gap_s"], off["first_window_days"], off["max_window_days"]) == (
        600, 3600, auto.FIRST_WINDOW_DAYS, auto.MAX_WINDOW_DAYS)
    sched = looks()
    clock = lambda: sched.t  # noqa: E731
    before = node.conn.total_changes
    fresh = auto.describe(node.conn, now=sched.now, clock=clock)
    assert fresh["enabled"] is True and fresh["changed_at"] and fresh["next"]["state"] == "waiting_for_quiet"
    sched.look()
    sched.look(QUIET - 60)
    soon = auto.describe(node.conn, now=sched.now, clock=clock)
    assert soon["next"]["state"] == "waiting_for_quiet"
    assert soon["next"]["not_before"] == auto._iso(sched.now + timedelta(seconds=60))
    sched.t += 60
    sched.now += timedelta(seconds=60)
    assert auto.describe(node.conn, now=sched.now, clock=clock)["next"]["state"] == "due"
    assert node.conn.total_changes == before and auto._watch.changed_at == 0.0, "reading it moves nothing"
    started = sched.look()
    sched.ended(started["run_id"])
    done = auto.describe(node.conn, now=sched.now, clock=clock)
    assert done["last"]["run_id"] == started["run_id"] and done["last"]["state"] == "done"
    assert done["last"]["totals"]["processed"] == len(FIRST_WINDOW_HITS)
    assert done["next"] == {"state": "waiting_for_records", "not_before": None}


def test_the_node_log_says_it_was_automatic_what_it_read_and_when_the_next_may_start(node, model, looks,
                                                                                      caplog) -> None:
    import logging

    sched = looks()
    with caplog.at_level(logging.INFO):
        started = sched.start()
        sched.ended(started["run_id"])
        _add(node.conn, 50, sched.now, hit=False)
        sched.look(60)
        sched.look(QUIET + GAP)
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert f"pack backfill run {started['run_id']} started (automatic)" in text
    assert f"automatic catch-up: run {started['run_id']} started: first look since the switch was turned on, " \
           f"1 packs with 5 record-pack pairs waiting among records dated {started['since']} or later" in text
    assert f"automatic catch-up: run {started['run_id']} ended: done; 5 processed" in text
    assert "the next one may start after " in text and "once new records have landed" in text
    assert "automatic catch-up: 1 new records since the last look, nothing waiting" in text
    assert "no run, no model loaded" in text
    assert "lake" not in text and "(entry" not in text, "counts and dates only"


# ----------------------------------------------------------------------------- the switch is the owner's


@pytest.fixture
def door(node, tmp_path, monkeypatch):
    """The mounted routes behind the owner socket's wrapper, in process."""
    monkeypatch.setenv("TOPOS_UDS_PATH", str(tmp_path / "engine.sock"))
    app, script = _mounted(node, monkeypatch)
    return SimpleNamespace(app=app, script=script, socket=lambda: TestClient(uds.UDSChannelApp(app)))


def _yes() -> Dict[str, str]:
    return {"X-Topos-Owner-Confirm": uds.write_owner_confirmation()}


def test_the_switch_is_in_the_census_as_the_owners_and_reading_it_is_a_read() -> None:
    assert census.lookup("PUT", AUTO).verdict == census.DANGEROUS
    assert census.lookup("GET", AUTO).verdict == census.READ
    for method in ("PUT", "GET"):
        assert not census.owner_agent_route_allowed(method, AUTO), "a coding agent's label does not reach it"
        assert not census.app_agent_route_allowed(method, AUTO), "nor does an app's"


def test_reading_the_switch_needs_no_yes_and_setting_it_does_for_off_as_for_on(door, node) -> None:
    with door.socket() as client:
        read = client.get(AUTO)
        assert read.status_code == 200 and read.json()["enabled"] is False
        for body in ({"enabled": True}, {"enabled": False}):
            refused = client.put(AUTO, json=body)
            wrong = client.put(AUTO, json=body, headers={"X-Topos-Owner-Confirm": "a" * 64})
            for response in (refused, wrong):
                assert response.status_code == 403 and response.json() == REFUSAL
        assert auto.read_switch(node.conn)["enabled"] is False

        header = _yes()
        on = client.put(AUTO, json={"enabled": True, "quiet_s": 300}, headers=header)
        assert on.status_code == 200, on.text
        assert on.json()["enabled"] is True and on.json()["quiet_s"] == 300 and on.json()["min_gap_s"] == 3600
        assert not uds.owner_confirmation_path().exists(), "the confirmation is consumed by the change"
        again = client.put(AUTO, json={"enabled": False}, headers=header)
        assert again.status_code == 403 and again.json() == REFUSAL, "one yes, one change"
        assert client.get(AUTO).json()["enabled"] is True

        off = client.put(AUTO, json={"enabled": False}, headers=_yes())
        assert off.status_code == 200 and off.json()["enabled"] is False
    assert runs.current() is None and door.script.calls == [], "setting the switch starts nothing by itself"


def test_a_body_that_is_not_the_switch_is_refused_and_changes_nothing(door, node) -> None:
    with door.socket() as client:
        for body in ({}, {"enabled": "yes"}, {"enabled": True, "pack_ids": [JOY]}, {"enabled": True, "quiet_s": 1},
                     {"enabled": True, "min_gap_s": "soon"}, {"quiet_s": 600}):
            response = client.put(AUTO, json=body, headers=_yes())
            assert response.status_code == 400 and response.json()["error"] == "auto_catchup_invalid", body
    assert auto.read_switch(node.conn)["enabled"] is False


def test_the_switch_is_refused_over_tcp_and_with_the_socket_gate_switched_off(door, node, monkeypatch) -> None:
    from topos.auth import require_api_key, resolve_request_principal
    from topos.principal import OWNER_APP, Principal

    # Over TCP the socket gate never runs; the owner's key alone must not set the switch.
    door.app.dependency_overrides[require_api_key] = lambda: "key"
    door.app.dependency_overrides[resolve_request_principal] = lambda: Principal(OWNER_APP, "local_http", acting_user="o")
    try:
        with TestClient(door.app) as tcp:
            _yes()                                   # a live token on disk changes nothing for a TCP caller
            token = json.loads(uds.owner_confirmation_path().read_text())["token"]
            response = tcp.put(AUTO, json={"enabled": True}, headers={"X-Topos-Owner-Confirm": token})
            assert response.status_code == 403 and response.json() == REFUSAL
            assert tcp.get(AUTO).json()["enabled"] is False
    finally:
        door.app.dependency_overrides.clear()
    uds.clear_owner_confirmation()

    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "0")
    with door.socket() as client:
        bare = client.put(AUTO, json={"enabled": True})
        assert bare.status_code == 403 and bare.json() == REFUSAL, "the route asks for the yes itself"
        assert auto.read_switch(node.conn)["enabled"] is False
        confirmed = client.put(AUTO, json={"enabled": True}, headers=_yes())
        assert confirmed.status_code == 200 and confirmed.json()["enabled"] is True


def test_turning_it_on_again_reads_the_first_window_and_not_what_piled_up(node, model, looks) -> None:
    sched = looks()
    sched.ended(sched.start()["run_id"])
    auto.set_switch(node.conn, False, now=sched.now)
    assert "covered" in auto.read_state(node.conn), "off keeps the bookkeeping"
    sched.now += timedelta(days=20)                     # three weeks with the switch off
    _add(node.conn, 70, sched.now, days_back=15, hit=True)
    _add(node.conn, 71, sched.now, days_back=1, hit=True)
    auto.set_switch(node.conn, True, now=sched.now)
    state = auto.read_state(node.conn)
    assert "covered" not in state and "base_at" not in state and state["last"]["state"] == "done"
    model.extract.clear()
    sched.look()
    started = sched.look(QUIET + GAP)
    assert started["decision"] == "started" and started["since"] == _day(sched.now, auto.FIRST_WINDOW_DAYS)
    sched.ended(started["run_id"])
    assert model.entries() == [71], "the yes covers recent records; the gap stays a manual backfill"


def test_the_apps_single_batch_is_not_refused_because_of_an_automatic_run(door, node, model, looks) -> None:
    sched = looks()
    automatic = _InHand(5)
    started = sched.start(run_batch=automatic)
    _until(automatic.working.is_set, "the automatic run to have a record in hand")
    threading.Timer(0.1, automatic.release.set).start()
    with door.socket() as client:
        batch = client.post(f"/v1/signal/derivation/packs/{JOY}/backfill", json={"limit": 2}, headers=_yes())
    assert batch.status_code == 200, batch.text
    assert batch.json()["processed"] == 2
    assert sched.ended(started["run_id"])["reason"] == "owner_backfill"


# ----------------------------------------------------------------------------- the command, over a real socket


def test_the_command_end_to_end_over_a_real_owner_socket(node, model, monkeypatch) -> None:
    """`topos-node packs auto on|status|off` against the mounted routes behind the socket wrapper on a real unix
    socket: the real client, the real confirmation file, the real gate. Only the person (a patched terminal and
    prompt) is a stand-in."""
    import os
    import sys
    from pathlib import Path

    import click
    from click.testing import CliRunner

    from tests.topos.fake_node_socket import serve_fake_node
    from topos.cli import packs_cmd
    from topos.mcp_server import NodeRefused, NodeSocket

    app, script = _mounted(node, monkeypatch)
    socket_path = Path(os.environ["TOPOS_UDS_PATH"])
    uds.clear_owner_confirmation()
    prompts: List[str] = []
    terminal = SimpleNamespace(isatty=lambda: True)
    pipe = SimpleNamespace(isatty=lambda: False)
    monkeypatch.setattr(click, "confirm", lambda text, **kw: prompts.append(text) or True)
    monkeypatch.setenv("TOPOS_DERIVATION_VERIFIER_MODEL", "stub-verifier")

    with serve_fake_node(SimpleNamespace(app=uds.UDSChannelApp(app)), socket_path):
        # What a coding agent with a shell can do: call the route, or run the command without a terminal.
        for body in ({"enabled": True}, {"enabled": False}):
            with pytest.raises(NodeRefused) as refused:
                NodeSocket().request_sync("PUT", AUTO, json_body=body)
            assert (refused.value.status, refused.value.code) == (403, "owner_confirmation_required")
        monkeypatch.setattr(packs_cmd, "sys", SimpleNamespace(stdin=pipe, stdout=pipe, exit=sys.exit))
        piped = CliRunner().invoke(packs_cmd.packs_group, ["auto", "on"], catch_exceptions=False)
        assert piped.exit_code == packs_cmd.EXIT_NEEDS_HUMAN and prompts == []
        assert auto.read_switch(node.conn)["enabled"] is False and not uds.owner_confirmation_path().exists()
        read = CliRunner().invoke(packs_cmd.packs_group, ["auto", "status"], catch_exceptions=False)
        assert read.exit_code == 0 and "Automatic catch-up: off." in read.output, "reading it needs no terminal"

        # The owner, on a terminal.
        monkeypatch.setattr(packs_cmd, "sys", SimpleNamespace(stdin=terminal, stdout=terminal, exit=sys.exit))
        on = CliRunner().invoke(packs_cmd.packs_group, ["auto", "on", "--quiet-minutes", "5"], catch_exceptions=False)
        assert on.exit_code == packs_cmd.EXIT_OK, on.output
        assert auto.read_switch(node.conn)["enabled"] is True and auto.read_switch(node.conn)["quiet_s"] == 300
        assert not uds.owner_confirmation_path().exists(), "the one confirmation was spent on the one change"
        status = CliRunner().invoke(packs_cmd.packs_group, ["status"], catch_exceptions=False)
        off = CliRunner().invoke(packs_cmd.packs_group, ["auto", "off"], catch_exceptions=False)
        assert off.exit_code == packs_cmd.EXIT_OK and auto.read_switch(node.conn)["enabled"] is False

    assert prompts == ["Turn it on?", "Turn it off?"]
    assert "1 of " in on.output and "packs are switched on" in on.output
    assert "none has arrived for 5m 00s" in on.output and "Automatic catch-up: on since " in on.output
    assert "Read by the local model stub-model; every fact found is checked by stub-verifier." in on.output
    assert "No backfill run since the node started." in status.output and "Automatic catch-up: on" in status.output
    assert "Next: " in status.output
    assert "Automatic catch-up: off." in off.output
    assert runs.current() is None and script.calls == [] and model.extract == [], "the switch alone starts nothing"


def test_records_that_land_on_rowids_a_delete_freed_still_count_as_new(node, model, looks) -> None:
    sched = looks()
    sched.ended(sched.start()["run_id"])
    model.extract.clear()
    node.conn.execute("DELETE FROM journal_entries WHERE entry_id IN ('j8', 'j9')")     # the two newest rows
    node.conn.commit()
    assert sched.look(60) == {"decision": "no_new_records"}
    assert auto.read_state(node.conn)["covered"]["journal_entries"] == 8, "the mark came down with the table"
    _add(node.conn, 80, sched.now, hit=True)                                           # lands on a freed rowid
    assert sched.look(60)["decision"] == "ingest_busy"
    assert sched.look(QUIET + GAP)["decision"] == "started"
    assert sched.ended()["new_records"] == 1 and model.entries() == [80]


def test_the_end_of_a_run_is_on_record_before_the_slot_is_free(node, model, looks, monkeypatch) -> None:
    """A look between the run's last record and its bookkeeping must not read the run as still running and start
    another: the slot is released only after the end is written."""
    sched = looks()
    seen: List[tuple] = []
    real = auto._record_end

    def record_end(snap, *args, **kwargs):
        seen.append(("before", runs.busy(), snap["state"]))
        real(snap, *args, **kwargs)
        seen.append(("after", runs.busy(), auto.read_state(sched._factory())["last"]["state"]))

    monkeypatch.setattr(auto, "_record_end", record_end)
    sched.ended(sched.start()["run_id"])
    assert seen == [("before", True, "done"), ("after", True, "done")]
    assert not runs.busy()


def test_switching_off_and_on_while_a_run_works_keeps_the_first_window(node, model, looks) -> None:
    sched = looks()
    first = sched.ended(sched.start()["run_id"])
    assert "covered" in auto.read_state(node.conn)
    automatic = _InHand(1)
    _add(node.conn, 90, sched.now, hit=True)
    sched.look(60)
    started = sched.look(QUIET + GAP, run_batch=automatic)
    assert started["decision"] == "started" and started["run_id"] != first["run_id"]
    _until(automatic.working.is_set, "the run to have a record in hand")
    auto.set_switch(node.conn, False, now=sched.now)
    auto.set_switch(node.conn, True, now=sched.now)          # forgets the marks, on purpose
    automatic.release.set()
    sched.ended(started["run_id"])
    state = auto.read_state(node.conn)
    assert "covered" not in state and "base_at" not in state, "the run's end did not bring the old marks back"
