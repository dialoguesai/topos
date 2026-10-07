"""Third fix round, review R2-M2: the exclude carry runs first, and sharing waits for it.

Until the step has run, a person the owner had excluded from sharing is not withheld: the re-check released the
excluded contact's own thread and handed it to the answer model in the "before the step" half of every door case.
Nothing tied sharing to the step. It ran in declaring order, after every step of every earlier release a node had
skipped (hours of reprocessing for a node coming from 1.3.x), and only after the runner had waited up to 80 s for the
app; with the runner switched off it never ran. An owner who turned sharing on in that time shared the person.

protects:
  - the step is first in every plan that holds it, whichever release the node comes from, and `start_background`
    runs it at once, before the wait for the UI and whether or not an older step fails later: no node is stranded
    behind another release's work;
  - while the step is owed and has not finished, and some exclude is not yet carried, the node holds back
    (`contact_excludes.owed`, `hold`): a share read is refused and a new bind is refused, each with a code the node
    can name; a node with nothing to carry, and a fresh install, are never held;
  - what an owner sees when the step fails for good.
The manifest is read as the release cut leaves it (the step under 1.5.0), whether or not this tree is cut yet.
Every person, handle and id here is invented.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import topos
from tests.topos.test_carry_step_review_r1 import cid, conn, contact  # noqa: F401 (conn: fixture)
from topos.features.lifecycle import contact_excludes
from topos.features.lifecycle.blackhole import BlackholeStore
from topos.features.lifecycle.contact_excludes import FAILED, OWED, STEP_ID, carry_contact_excludes, hold, owed
from topos.permissions_v2.canonical import PolicyError
from topos.upgrades import runner

pytestmark = pytest.mark.public

RELEASE = "1.5.0"


@pytest.fixture(autouse=True)
def the_release_as_cut(tmp_path, monkeypatch):
    """`manifests.json` as `cut_release.py` leaves it: the staging entry stamped with the version, and the node
    running that version. A tree that is already cut is left as it is."""
    import topos.upgrades as upgrades

    data = json.loads((Path(topos.__file__).parent / "upgrades" / "manifests.json").read_text())
    for release in data["releases"]:
        if release["version"] == "unreleased" and any(s.get("id") == STEP_ID for s in release.get("steps", [])):
            release["version"] = RELEASE
            data["releases"].append({"version": "unreleased", "summary": "staging", "steps": [], "notes": []})
            break
    path = tmp_path / "manifests.json"
    path.write_text(json.dumps(data))
    monkeypatch.setattr(upgrades, "_MANIFESTS_PATH", path)
    monkeypatch.setattr(runner, "_shipped_version", lambda: RELEASE)
    monkeypatch.setenv("TOPOS_UPGRADE_RUNNER", "on")
    contact_excludes.forget_hold()
    yield
    contact_excludes.forget_hold()


def an_upgraded_home(c, baseline):
    """A node with data that last finished its upgrades at `baseline`, and one contact the owner had excluded."""
    c.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, "
              "identifiers_json, mention_count, metadata_json) VALUES ('ent-any','person','Perrin Ashgrove',"
              "'perrin ashgrove','[]','[]',1,'{}')")
    c.commit()
    runner._stamp_baseline(c, baseline)
    contact(c, cid("0a"), "Sam")


def recording(calls, *, fail=()):
    """Executors that record each step they are given; the carry step runs for real."""
    def executor(step, c):
        calls.append(step["id"])
        if step["id"] in fail:
            raise RuntimeError("an older release's step that fails")
        if step["id"] == STEP_ID:
            return runner._exec_engine_endpoint(step, c)
        return {}
    return {kind: executor for kind in ("enrichment_reprocess", "engine_endpoint", "canonical_reprocess",
                                        "derived_rebuild", "reembed", "none")}


# ------------------------------------------------------------------------------------------------- it runs first

@pytest.mark.parametrize("baseline", ["1.3.5", "1.4.4"])
def test_the_step_is_first_in_the_plan_whatever_release_the_node_comes_from(conn, baseline):
    """Rule: `_plan_steps` puts a step declared `runs_first` ahead of every other. Leave the declaring order and a
    node from 1.3.x has every step of 1.3.6 to 1.4.4 queued ahead of it."""
    an_upgraded_home(conn, baseline)
    plan = runner.plan_upgrade(conn)
    ids = [step["id"] for step in plan["steps"]]
    assert ids[0] == STEP_ID and ids.count(STEP_ID) == 1
    assert runner.runs_first(plan["steps"][0]) and not any(runner.runs_first(step) for step in plan["steps"][1:])
    if baseline == "1.3.5":
        assert len(ids) > 1, "control: a node from 1.3.x owes older releases' steps too"


def test_a_node_from_1_3_x_is_not_stranded_behind_an_older_step_that_fails(conn):
    """The whole plan through the runner: the carry is done first; a later, older step fails for good; the baseline
    stays behind as it must, and sharing is NOT held for it: the hold reads the carry's own row."""
    an_upgraded_home(conn, "1.3.5")
    older = [step["id"] for step in runner.plan_upgrade(conn)["steps"] if step["id"] != STEP_ID]
    assert owed(conn) == OWED
    calls = []
    out = runner.run_pending_upgrades(conn, executors=recording(calls, fail={older[0]}))
    assert calls[0] == STEP_ID and older[0] in calls
    assert out["steps_failed"] >= 1 and out["baseline_advanced"] is False
    assert owed(conn) is None
    assert [e["carried_waiting"] for e in BlackholeStore(conn).list()] == [True]
    # the next start: the carry is not run again, the failed step is
    again = []
    runner.run_pending_upgrades(conn, executors=recording(again, fail={older[0]}))
    assert STEP_ID not in again and older[0] in again


def test_the_first_pass_runs_only_the_steps_that_run_first_and_stamps_nothing(conn):
    an_upgraded_home(conn, "1.3.5")
    calls = []
    out = runner.run_pending_upgrades(conn, executors=recording(calls), only_first=True)
    assert calls == [STEP_ID] and (out["steps_run"], out["baseline_advanced"]) == (1, False)
    assert runner.read_baseline(conn) == "1.3.5"
    # on a node that owes only this step, that pass finishes the upgrade
    other = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        from topos.permissions_v2.protection_clock import TOMBSTONES_SQL
        from topos.storage.canonical import ConversationsTablesManager
        from topos.storage.db.migrations import apply_all_migrations

        apply_all_migrations(other)
        other.execute(TOMBSTONES_SQL)
        ConversationsTablesManager(other).ensure_tables()
        an_upgraded_home(other, "1.4.4")
        if [s["id"] for s in runner.plan_upgrade(other)["steps"]] == [STEP_ID]:
            done = runner.run_pending_upgrades(other, executors=recording([]), only_first=True)
            assert done["baseline_advanced"] is True and runner.read_baseline(other) == RELEASE
    finally:
        other.close()


def test_start_up_runs_the_step_at_once_without_waiting_for_the_app(conn):
    """Rule: `start_background` makes the first pass before the ready wait and the grace window. Remove it and the
    person is shareable for as long as the app takes to come up (60 s and 20 s by default), or for ever with the
    ready event never set."""
    an_upgraded_home(conn, "1.3.5")
    stop, never_ready = threading.Event(), threading.Event()
    thread = runner.start_background(conn, ready_event=never_ready, ready_timeout_s=600, ui_grace_s=600, stop_event=stop)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not BlackholeStore(conn).list():
            time.sleep(0.02)
        assert [e["carried_waiting"] for e in BlackholeStore(conn).list()] == [True]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and owed(conn) is not None:
            time.sleep(0.02)
        assert owed(conn) is None                                         # ledgered done, while the app is not up
        assert runner.read_baseline(conn) == "1.3.5" and thread.is_alive()   # the older steps still wait for the UI
    finally:
        stop.set()
        thread.join(timeout=10)
    assert not thread.is_alive()


# ----------------------------------------------------------------------------------------------------- the hold

def test_when_the_node_holds_and_when_it_does_not(conn):
    """`owed`: both must hold, the runner's plan still has the step not done AND an exclude is not yet carried."""
    assert owed(conn) is None                                             # a fresh install: nothing is planned
    an_upgraded_home(conn, "1.4.4")
    assert owed(conn) == OWED                                             # never started
    runner._ledger_set(conn, RELEASE, STEP_ID, "running", started=True)
    assert owed(conn) == OWED
    runner._ledger_set(conn, RELEASE, STEP_ID, "failed", {"error": "x"})
    assert owed(conn) == FAILED
    carry_contact_excludes(conn)                                          # the exclude is carried: nobody is unprotected
    assert owed(conn) is None                                             # even with the row still `failed`
    runner._ledger_set(conn, RELEASE, STEP_ID, "done", {})
    contact(conn, cid("0z"), "Perrin Ashgrove")                           # an exclude written after the step was done
    assert owed(conn) is None                                             # is not this step's: no hold for ever


def test_a_node_with_nobody_excluded_is_never_held(conn):
    """The runner switched off, the step never run: nothing to carry, so nobody the hold would protect."""
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, "
                 "identifiers_json, mention_count, metadata_json) VALUES ('ent-any','person','Perrin Ashgrove',"
                 "'perrin ashgrove','[]','[]',1,'{}')")
    conn.commit()
    runner._stamp_baseline(conn, "1.4.4")
    contact(conn, cid("0a"), "Sam", policy=None)                          # no stored choice
    contact(conn, cid("0b"), "Mine", is_self=1)                           # the owner's own card is never carried
    assert STEP_ID in [s["id"] for s in runner.plan_upgrade(conn)["steps"]] and owed(conn) is None


def test_what_cannot_be_read_holds(conn, monkeypatch):
    """Unknown is never "nothing is owed"."""
    an_upgraded_home(conn, "1.4.4")
    monkeypatch.setattr(runner, "plan_upgrade", lambda c, shipped=None: (_ for _ in ()).throw(RuntimeError("no")))
    assert owed(conn) == FAILED
    assert hold("/nonexistent/folder/canonical.db") == FAILED


def test_a_database_that_cannot_be_read_right_now_holds(conn, tmp_path):
    """The runner's own readers swallow a read error and plan a fresh install, which owes nothing. A node that owes
    the step and whose database is locked for a moment (a backup, a migration) must not read as "nothing owed":
    the answer is remembered for half a minute, which could outlast the lock."""
    an_upgraded_home(conn, "1.4.4")
    conn.commit()
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    assert hold(path) == OWED
    contact_excludes.forget_hold()
    locker = sqlite3.connect(path, isolation_level=None)
    reader = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=0.05)
    try:
        locker.execute("PRAGMA locking_mode=EXCLUSIVE")
        locker.execute("BEGIN EXCLUSIVE")
        assert owed(reader) == FAILED
    finally:
        locker.execute("ROLLBACK")
        locker.close()
        reader.close()
    assert hold(path) == OWED


def test_start_up_touches_nothing_before_the_wait_when_no_step_runs_first(conn, monkeypatch):
    """The first pass is for a plan that holds a step declared to run first. Every other plan keeps the older rule
    (tests/topos/test_startup_background_reaping.py): nothing runs until the wait and the grace are over."""
    an_upgraded_home(conn, "1.3.5")
    plan = runner.plan_upgrade(conn)
    others = {**plan, "steps": [step for step in plan["steps"] if not runner.runs_first(step)]}
    monkeypatch.setattr(runner, "plan_upgrade", lambda c, shipped=None: others)
    called = []
    monkeypatch.setattr(runner, "run_pending_upgrades", lambda *a, **k: called.append(k) or {})
    stop = threading.Event()
    thread = runner.start_background(conn, ready_event=threading.Event(), ready_timeout_s=600, ui_grace_s=600, stop_event=stop)
    time.sleep(0.3)
    stop.set()
    thread.join(timeout=10)
    assert called == [] and not thread.is_alive()


def test_the_hold_by_path_remembers_only_that_the_step_is_done(conn):
    an_upgraded_home(conn, "1.4.4")
    conn.commit()
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    assert hold(path) == OWED
    runner.run_pending_upgrades(conn, executors=recording([]), only_first=True)
    contact_excludes._hold_cache.clear()                                  # as two seconds later
    assert hold(path) is None and path in contact_excludes._hold_done
    conn.execute("DELETE FROM derivation_ledger")                         # even if the ledger were lost later
    conn.commit()
    assert hold(path) is None


def test_a_share_read_is_refused_while_the_node_holds(conn, monkeypatch):
    """Rule: `Runtime.message_search` and `Runtime.answers`, which every share door goes through, ask the hold
    first. Remove it and the excluded person's thread is released until the step has run."""
    from topos.permissions_v2.runtime import Runtime

    an_upgraded_home(conn, "1.4.4")
    conn.commit()
    path = conn.execute("PRAGMA database_list").fetchone()[2]
    node = object.__new__(Runtime)                                        # no runtime is loaded: the hold comes first
    node.protocol = SimpleNamespace(canonical_database=Path(path))
    for read in (node.message_search, node.answers):
        with pytest.raises(PolicyError) as refused:
            read()
        assert refused.value.code == OWED
    runner.run_pending_upgrades(conn, executors=recording([]), only_first=True)
    contact_excludes.forget_hold()
    node.hold_for_the_exclude_carry()                                     # nothing held now
    with pytest.raises(Exception) as other:                               # past the hold: this bare object has no more
        node.message_search()
    assert getattr(other.value, "code", None) not in (OWED, FAILED)


def test_what_an_owner_sees_when_the_step_fails_for_good(conn, monkeypatch):
    """One contact that can never be carried: the others are, the step stays `failed` and runs at every start, the
    node holds sharing with a code, and the owner has one notice in his Off-limits list saying so."""
    an_upgraded_home(conn, "1.4.4")
    contact(conn, cid("0b"), "Brisa Vantongeren")
    real = contact_excludes._carry_one

    def carry_one(c, store, contact_id, entry):
        if contact_id == cid("0b"):
            raise RuntimeError("never")
        return real(c, store, contact_id, entry)

    monkeypatch.setattr(contact_excludes, "_carry_one", carry_one)
    for _start in range(2):
        runner.run_pending_upgrades(conn)
    row = [r for r in runner.ledger_rows(conn) if r["step_id"] == STEP_ID][-1]
    assert row["status"] == "failed" and row["detail"]["error"].startswith("1 of 2 explicit excludes could not be carried")
    assert owed(conn) == FAILED
    notices = {n["kind"]: n["message"] for n in BlackholeStore(conn).notifications(state="open")}
    assert notices["carry_failed"] == contact_excludes.NOTICE_FAILED and "carried_over" in notices
    assert len(BlackholeStore(conn).notifications(state="open")) == 2     # one of each, not one per start
    status = runner.runner_status(conn)
    assert STEP_ID in status["pending_steps"] and status["baseline"] == "1.4.4"
