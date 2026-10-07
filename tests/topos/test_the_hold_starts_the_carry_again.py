"""Sixth round (third re-check, R4-M1 and the start-up race): the hold ends without a restart.

While the step that carries the older per-person excludes is owed, the node refuses every share read and every new
bind (`contact_excludes.hold`). The step ran at a start and nowhere else, so every way of missing it at one start
left a node that shared nothing until the next one, and said nothing:

  - the upgrade thread dead at start (two threads on the node's one connection: the `SystemError` of round five);
  - the thread never started (the plan read on the caller's thread raised);
  - the step cut short before it wrote its notices (ledger `failed`, no notice);
  - an exclude an older app wrote after the upgrade (the step `done`, nothing runs until a start).

protects: while the step is owed and no upgrade thread is alive, the hold's own look starts the step's pass again,
on a thread and a connection of its own (`contact_excludes._start_the_pass_again`). At most three times in a row,
not more often than once in half a minute, never beside a live pass, and only the steps declared to run first (the
carry: no model is called). After the third it writes ONE notice in plain words and tries no more until the next
start. A pass that ends the hold lets sharing through at once.
Every person, handle and id here is invented.
"""

from __future__ import annotations

import threading
import time

import pytest

from tests.topos.test_carry_step_review_r1 import cid, conn, contact  # noqa: F401 (conn: fixture)
from tests.topos.test_exclude_carry_runs_first_and_sharing_waits import (RELEASE, an_upgraded_home,  # noqa: F401
                                                                          half_a_minute_later, the_release_as_cut)
from topos.features.lifecycle import contact_excludes
from topos.features.lifecycle.blackhole import BlackholeStore
from topos.features.lifecycle.contact_excludes import FAILED, OWED, STEP_ID, hold, owed
from topos.upgrades import runner

pytestmark = [pytest.mark.public,
              # a planted death of the upgrade thread is this file's subject
              pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")]

THE_RACE = "<sqlite3.Connection object> returned NULL without setting an exception"


@pytest.fixture(autouse=True)
def no_thread_outlives_its_test():
    yield
    finished()


def finished(timeout=20.0):
    """Wait for a pass the hold started. Returns how many it has started for each database so far."""
    for state in list(contact_excludes._again.values()):
        thread = state.get("thread")
        if thread is not None:
            thread.join(timeout)
            assert not thread.is_alive()
    return {key: state["started"] for key, state in contact_excludes._again.items()}


def look(path):
    """One fresh look of the hold, as two seconds after the last, and whatever it started run to its end."""
    contact_excludes._hold_cache.clear()
    answer = hold(path)
    finished()
    return answer


def path_of(c):
    c.commit()
    return c.execute("PRAGMA database_list").fetchone()[2]


def the_steps_rows(c):
    return [(row["status"], (row["detail"] or {}).get("carried")) for row in runner.ledger_rows(c)
            if row["step_id"] == STEP_ID]


def notices(c):
    return [(n["kind"], n["message"]) for n in BlackholeStore(c).notifications(state="open")]


def entries(c):
    return sorted((e["canonical_name"], bool(e["carried_waiting"])) for e in BlackholeStore(c).list())


def only_on_the_shared_connection(monkeypatch, shared, name="_run_pending_upgrades"):
    """The race as round five met it: an error of the node's one shared connection. A connection of its own does
    not meet it."""
    real = getattr(runner, name)

    def raced(c, *args, **kwargs):
        if c is shared:
            raise SystemError(THE_RACE)
        return real(c, *args, **kwargs)
    monkeypatch.setattr(runner, name, raced)


# ------------------------------------------------------------------------------- the four ways in, each ended

def test_the_upgrade_thread_dead_at_start(conn, monkeypatch):  # noqa: F811
    """Both passes of the start raise on the shared connection: the thread is dead, no ledger row, no notice. The
    first look of the hold is refused and starts the pass again on a connection of its own; the next is let through."""
    an_upgraded_home(conn, "1.4.4")
    path = path_of(conn)
    only_on_the_shared_connection(monkeypatch, conn)
    ready = threading.Event()
    ready.set()
    thread = runner.start_background(conn, ready_event=ready, ready_timeout_s=1, ui_grace_s=0)
    thread.join(10)
    assert not thread.is_alive() and the_steps_rows(conn) == [] and entries(conn) == [] and notices(conn) == []
    assert hold(path) == OWED                                             # refused, and this look starts it
    assert finished() == {path: 1}
    assert the_steps_rows(conn) == [("done", 1)] and entries(conn) == [("Sam", True)]
    assert hold(path) is None                                             # at once: no wait, no restart of the node
    assert [kind for kind, _ in notices(conn)] == ["carried_over"]
    assert runner.read_baseline(conn) == RELEASE


def test_the_upgrade_thread_never_started(conn, monkeypatch):  # noqa: F811
    """The plan read on the caller's thread raises: `start_background` never makes its thread (the app logs it as
    non-fatal)."""
    an_upgraded_home(conn, "1.4.4")
    path = path_of(conn)
    only_on_the_shared_connection(monkeypatch, conn, "plan_upgrade")
    with pytest.raises(SystemError):
        runner.start_background(conn, ready_event=threading.Event(), ready_timeout_s=600, ui_grace_s=600)
    assert the_steps_rows(conn) == [] and notices(conn) == []
    assert hold(path) == OWED
    assert finished() == {path: 1}
    assert the_steps_rows(conn) == [("done", 1)] and hold(path) is None
    assert [kind for kind, _ in notices(conn)] == ["carried_over"]


def test_a_step_cut_short_before_its_notices(conn, monkeypatch):  # noqa: F811
    """The ledger says `failed`, the hold answers, and nothing was written for the owner to read. The pass the hold
    starts runs the step again: the contact is remembered, the boundary builds, the notices are written."""
    an_upgraded_home(conn, "1.4.4")
    path = path_of(conn)
    real, cut = contact_excludes._write_notices, [True]

    def once(c, out):
        if cut.pop() if cut else False:
            raise RuntimeError("cut short")
        return real(c, out)
    monkeypatch.setattr(contact_excludes, "_write_notices", once)
    runner.run_pending_upgrades(conn)                                     # the start
    assert the_steps_rows(conn)[0][0] == "failed" and notices(conn) == []
    assert hold(path) == FAILED
    assert finished() == {path: 1}
    assert the_steps_rows(conn)[0][0] == "done" and hold(path) is None
    assert [kind for kind, _ in notices(conn)] == ["carried_over"]


def test_an_exclude_written_after_the_upgrade_is_carried_with_no_restart(conn, monkeypatch):  # noqa: F811
    """An older app writes one more exclude after the step is done. For up to half a minute the hold still
    remembers "nothing owed" (that person is shared for that long, as in every minute before the exclude). The
    first look after that is refused and starts the pass; when the pass has carried them the hold is over."""
    an_upgraded_home(conn, "1.4.4")
    path = path_of(conn)
    runner.run_pending_upgrades(conn)
    assert hold(path) is None and entries(conn) == [("Sam", True)]
    contact(conn, cid("0c"), "Perrin Ashgrove")
    conn.commit()
    assert hold(path) is None                                             # inside the half minute: as remembered
    half_a_minute_later(monkeypatch)
    started = time.perf_counter()
    assert hold(path) == OWED                                             # held from here
    assert finished() == {path: 1}
    held_for = time.perf_counter() - started
    assert hold(path) is None                                             # to here
    assert entries(conn) == [("Perrin Ashgrove", True), ("Sam", True)]
    assert the_steps_rows(conn) == [("done", 1)]
    assert notices(conn) == [("carried_over", contact_excludes.NOTICE.format(count=2))]
    assert held_for < 10.0, held_for
    print(f"R6N-HELD-FOR an exclude written after the upgrade: sharing held {held_for * 1000:.0f} ms")


# ------------------------------------------------------------------------------------ bounded, and it says so

def always_cut_short(monkeypatch):
    def cut(c, out):
        raise RuntimeError("cut short at every start")
    monkeypatch.setattr(contact_excludes, "_write_notices", cut)


def test_three_tries_then_one_notice_and_no_more_until_the_next_start(conn, monkeypatch):  # noqa: F811
    """Rule: at most `_AGAIN_LIMIT` passes in a row; after the last, one notice (`NOTICE_HELD`), and nothing more
    is started in this process. Without the bound a home that cannot be carried is run at every look; without the
    notice the owner of such a home still has no word."""
    an_upgraded_home(conn, "1.4.4")
    path = path_of(conn)
    always_cut_short(monkeypatch)
    monkeypatch.setattr(contact_excludes, "_AGAIN_SECONDS", 0.0)
    runner.run_pending_upgrades(conn)                                     # the start: failed, no notice
    assert notices(conn) == []
    for expected in (1, 2):
        assert look(path) == FAILED and finished() == {path: expected}
        assert notices(conn) == []                                        # not yet: it is still trying
    assert look(path) == FAILED and finished() == {path: 3}
    assert notices(conn) == [("carry_failed", contact_excludes.NOTICE_HELD)]
    for _later in range(4):
        assert look(path) == FAILED
    assert finished() == {path: 3}                                        # no fourth
    assert notices(conn) == [("carry_failed", contact_excludes.NOTICE_HELD)]     # and still one notice
    assert contact_excludes.NOTICE_HELD == (
        "Nothing of yours is being shared, because Topos could not finish carrying over the people you had "
        "excluded from sharing in an earlier version. Start Topos again.")
    assert contact_excludes._AGAIN_LIMIT == 3


def test_a_pass_that_ends_the_hold_begins_the_count_again(conn, monkeypatch):  # noqa: F811
    """The bound is for a home that cannot be carried. One that can gets its three again each time: a fourth and
    a fifth exclude written from an older app in one run of the node are each carried with no restart."""
    an_upgraded_home(conn, "1.4.4")
    path = path_of(conn)
    monkeypatch.setattr(contact_excludes, "_AGAIN_SECONDS", 0.0)
    runner.run_pending_upgrades(conn)
    for n in range(5):
        contact(conn, cid(f"2{n}"), f"Person {n} Example")
        conn.commit()
        assert look(path) == OWED and finished() == {path: n + 1}
        assert hold(path) is None and len(entries(conn)) == n + 2
    assert notices(conn) == [("carried_over", contact_excludes.NOTICE.format(count=6))]


def test_a_notice_the_step_wrote_itself_is_left_as_it_is(conn, monkeypatch):  # noqa: F811
    """Where the step reached its end and said why (it names the entry it cannot read), three more failures add no
    second notice and do not replace its words."""
    from tests.topos.test_carry_step_names_what_it_cannot_read import odd

    an_upgraded_home(conn, "1.4.4")
    odd(conn, usernames="[null]")
    path = path_of(conn)
    monkeypatch.setattr(contact_excludes, "_AGAIN_SECONDS", 0.0)
    runner.run_pending_upgrades(conn)
    said = notices(conn)
    assert [kind for kind, _ in said] == ["carried_over", "carry_failed"] and "Brisa Vantongeren" in said[1][1]
    for _look in range(5):
        assert look(path) == FAILED
    assert finished() == {path: 3} and notices(conn) == said


def test_after_the_owner_removes_the_entry_the_notice_names_a_try_that_is_left_ends_the_hold(conn, monkeypatch):  # noqa: F811
    """The notice that names an entry says sharing comes back the next time Topos starts. That is the longest it
    takes: when the hold still has a try left, the next one finds the boundary building and ends the hold with no
    restart."""
    from tests.topos.test_carry_step_names_what_it_cannot_read import odd

    an_upgraded_home(conn, "1.4.4")
    odd(conn, usernames="[null]")
    path = path_of(conn)
    runner.run_pending_upgrades(conn)                                     # the start: failed, the entry named
    assert look(path) == FAILED and finished() == {path: 1}               # one try: the same failure
    named = [row for row in runner.ledger_rows(conn) if row["step_id"] == STEP_ID][0]["detail"]["unreadable"]
    BlackholeStore(conn).unblackhole_entity(entity_ref=named["entries"][0]["blackhole_id"])
    conn.commit()
    half_a_minute_later(monkeypatch)
    assert look(path) == FAILED and finished() == {path: 2}               # this look is refused and starts the try
    assert hold(path) is None and entries(conn) == [("Sam", True)]
    # the failure notice is taken away; what is left is the step's own and the removal's
    assert sorted(kind for kind, _ in notices(conn)) == ["carried_over", "reinclude_needed"]


def test_it_does_not_loop_hot(conn, monkeypatch):  # noqa: F811
    """Rule: not more often than once in `_AGAIN_SECONDS`, whatever the looks. The hold is asked by every share
    read; a held answer is fresh every two seconds."""
    an_upgraded_home(conn, "1.4.4")
    path = path_of(conn)
    always_cut_short(monkeypatch)
    runner.run_pending_upgrades(conn)
    for _look in range(6):
        assert look(path) == FAILED
    assert finished() == {path: 1}
    half_a_minute_later(monkeypatch)
    assert look(path) == FAILED and look(path) == FAILED
    assert finished() == {path: 2}
    assert contact_excludes._AGAIN_SECONDS == 30.0


# ------------------------------------------------------------------------------- never beside a live pass

def test_nothing_is_started_while_an_upgrade_thread_is_alive(conn):  # noqa: F811
    """The runner's own thread lives through its wait for the app and runs the step after it: starting a second
    pass beside it is what this must never do."""
    an_upgraded_home(conn, "1.4.4")
    path = path_of(conn)
    release = threading.Event()
    waiting = threading.Thread(target=release.wait, name="topos-upgrade-runner", daemon=True)
    waiting.start()
    try:
        for _look in range(3):
            assert look(path) == OWED
        assert finished() == {} and the_steps_rows(conn) == []
    finally:
        release.set()
        waiting.join(5)
    assert look(path) == OWED and finished() == {path: 1}                 # the thread is gone: now it is started
    assert hold(path) is None


def test_nothing_runs_beside_a_pass_that_is_live_on_another_thread(conn, monkeypatch):  # noqa: F811
    """A pass run from a request (the consent door) has no thread of that name. The runner counts its live passes
    and the restarted one takes its turn only when there is none (`runner.claim_the_only_pass`)."""
    an_upgraded_home(conn, "1.4.4")
    path = path_of(conn)
    monkeypatch.setattr(contact_excludes, "_AGAIN_SECONDS", 0.0)
    ran = []
    real = runner._run_pending_upgrades
    monkeypatch.setattr(runner, "_run_pending_upgrades", lambda c, *a, **k: ran.append(1) or real(c, *a, **k))
    assert runner.claim_the_only_pass() is True                           # as a pass that is running now
    try:
        assert runner.claim_the_only_pass() is False
        assert look(path) == OWED and ran == [] and the_steps_rows(conn) == []
    finally:
        runner.release_the_pass()
    assert finished() == {path: 0}                                        # that was no try
    assert look(path) == OWED and ran == [1] and finished() == {path: 1}
    assert hold(path) is None


def test_a_pass_of_the_runner_waits_for_a_restarted_one_to_finish(conn, monkeypatch):  # noqa: F811
    """The other direction: a start, or a request, that begins a pass while a restarted one is running lets it
    finish first (`runner._let_a_restarted_carry_finish`)."""
    an_upgraded_home(conn, "1.4.4")
    conn.commit()
    done = threading.Event()

    def a_restarted_pass():
        time.sleep(0.4)
        done.set()
    restarted = threading.Thread(target=a_restarted_pass, name=runner.CARRY_AGAIN_THREAD, daemon=True)
    seen = []
    real = runner._run_pending_upgrades
    monkeypatch.setattr(runner, "_run_pending_upgrades", lambda c, *a, **k: seen.append(done.is_set()) or real(c, *a, **k))
    restarted.start()
    runner.run_pending_upgrades(conn)
    restarted.join(5)
    assert seen == [True]


# ---------------------------------------------------------------------------- what it runs, and when not at all

def test_the_restarted_pass_runs_the_step_that_runs_first_and_nothing_else(conn, monkeypatch):  # noqa: F811
    """A node from 1.3.x owes every older release's steps as well, and those call models. Rule: the restarted pass
    is the first pass (`only_first`): the steps declared to run first, which is the carry and no other."""
    an_upgraded_home(conn, "1.3.5")
    path = path_of(conn)
    assert len(runner.plan_upgrade(conn)["steps"]) > 1
    called = []
    real = dict(runner.DEFAULT_EXECUTORS)

    def recorded(kind):
        def executor(step, c):
            called.append(step["id"])
            if step["id"] != STEP_ID:
                raise AssertionError("an older release's step was run by the hold")
            return real[kind](step, c)
        return executor
    monkeypatch.setattr(runner, "DEFAULT_EXECUTORS", {kind: recorded(kind) for kind in real})
    assert hold(path) == OWED
    assert finished() == {path: 1}
    assert called == [STEP_ID] and the_steps_rows(conn) == [("done", 1)]
    assert [row["step_id"] for row in runner.ledger_rows(conn)] == [STEP_ID]
    assert runner.read_baseline(conn) == "1.3.5" and hold(path) is None  # the older steps still wait for a start


def test_nothing_is_started_with_the_runner_switched_off_or_nothing_owed(conn, monkeypatch, tmp_path):  # noqa: F811
    an_upgraded_home(conn, "1.4.4")
    path = path_of(conn)
    monkeypatch.setenv("TOPOS_UPGRADE_RUNNER", "off")                     # the operator's switch: its own notice
    assert look(path) == OWED and finished() == {}
    monkeypatch.setenv("TOPOS_UPGRADE_RUNNER", "on")
    runner.run_pending_upgrades(conn)
    assert look(path) is None and finished() == {}                        # nothing owed: nothing to start
    missing = tmp_path / "no-such.db"
    assert hold(str(missing)) == FAILED and finished() == {} and not missing.exists()     # and nothing is created


def test_the_restarted_pass_uses_a_connection_of_its_own(conn, monkeypatch):  # noqa: F811
    """Not the node's shared connection (it does not have it), and not one it leaves open."""
    an_upgraded_home(conn, "1.4.4")
    path = path_of(conn)
    used = []
    real = runner._run_pending_upgrades
    monkeypatch.setattr(runner, "_run_pending_upgrades", lambda c, *a, **k: used.append((c, k.get("only_first"))) or real(c, *a, **k))
    assert hold(path) == OWED and finished() == {path: 1}
    (own, only_first), = used
    assert own is not conn and only_first is True
    with pytest.raises(Exception):
        own.execute("SELECT 1")                                           # closed when the pass was over
    assert owed(conn) is None
