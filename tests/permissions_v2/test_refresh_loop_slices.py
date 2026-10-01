"""RD2 catch-up over a grant window wider than the worker's 31 days (refresh_loop.window_slices).

A pass walks the whole window of the widest knowledge grant in slices of at most 31 days, newest first, one worker
run each, under one budget and one receipt. A continuation resumes at the slice its pass stopped in. The state moves
only when the last slice ends within budget, and no restore runs between the slices of one pass.
"""
import json
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_automatic_message_review import setup
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401 -- legacy's fixture
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- fixture
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_refresh_loop import (CatchUpLoop, Clock, FakeIndex, dark_around_passes, daytime, drop,
                                                    receipts, settings, settled, stored_state)
from topos.permissions_v2.automatic_review_worker import AutomaticReviewWorker
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.message_review_contract import AutomaticReviewRequest, AutomaticReviewStatus
from topos.permissions_v2.refresh_loop import MAX_SLICE_SECONDS, STATE_FILE, RefreshLoop, window_slices

DAY = 86_400


class SliceWorker:
    """AutomaticReviewWorker's node pass, by hand. Each start_node_pass is one run: it refuses what _launch refuses
    (a window wider than 31 days, empty, or ending after `now`) and starts its status at zero. `published` counts
    what the run publishes while it runs (the restore's progress signal). `end` finishes the run: the rows inside its
    window (closed at both ends, as _page reads it) that are not current yet are assessed up to the run's budget,
    and the current ones are skipped, as durable checkpoints are."""

    def __init__(self, rows=()):
        self.rows, self.current, self.runs = dict(rows), set(), []
        self.is_running, self.published = False, 0
        self.status_value = AutomaticReviewStatus(state="idle")

    def running(self):
        return self.is_running

    def start_node_pass(self, request, *, now, ingested_after, max_assessed):
        if request.before > now or request.before <= request.after or request.before - request.after > 31 * DAY:
            raise PolicyError("message_review_window_invalid")
        self.runs.append(dict(after=request.after, before=request.before, ingested_after=ingested_after,
                              max_assessed=max_assessed))
        self.is_running, self.published = True, 0

    def status(self):
        if self.is_running:
            return AutomaticReviewStatus(state="running", assessed=self.published)
        return self.status_value

    def end(self, state="complete"):
        run, counts = self.runs[-1], dict(scanned=0, assessed=self.published, current=0)
        if state == "complete":
            for row in sorted(self.rows):
                if counts["assessed"] >= run["max_assessed"]:
                    break
                if run["after"] <= self.rows[row] <= run["before"]:
                    counts["scanned"] += 1
                    if row in self.current:
                        counts["current"] += 1
                    else:
                        self.current.add(row)
                        counts["assessed"] += 1
        self.is_running, self.status_value = False, AutomaticReviewStatus(state=state, **counts)


def aged(T, ages, per_age=1):
    """Rows by age in days, each an hour inside its day; their ids sort newest first."""
    return {f"row-{age:03d}-{n}": T - age * DAY - 3600 for age in ages for n in range(per_age)}


NEWEST, MIDDLE, OLDEST = range(0, 30), range(31, 61), range(62, 90)   # 30, 30 and 28 rows, one per slice


def windowed(tmp_path, clock, worker, window=90 * DAY, **overrides):
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    values = dict(catchup=True, full_hours=(2, 6), catchup_interval=300, max_assessed=500)
    values.update(overrides)
    return CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, window=window, **values)


def walk(loop, worker, clock, limit=10):
    """End each slice's run and tick until the pass ends: its receipt. Each slice but the last starts the next in the
    same tick."""
    for _ in range(limit):
        worker.end()
        clock.now += 5
        receipt = loop.run_catchup()
        if receipt is not None:
            return receipt
        assert worker.is_running and loop._pass is not None
    raise AssertionError("the pass never ended")


def bounds(worker):
    return [(run["after"], run["before"]) for run in worker.runs]


def test_a_90_day_grant_is_walked_in_three_slices_newest_first(tmp_path):
    """WS0, 1 Oct: every pass covered the newest 31 days, so rows 31-90 days old that a rule change staled stayed
    unassessed for good."""
    T = daytime()
    clock, worker = Clock(T), SliceWorker(aged(T, [*NEWEST, *MIDDLE, *OLDEST]))
    loop = windowed(tmp_path, clock, worker, interests=True)
    settled(loop, T)
    loop.revisions = {"rules": "r1"}                          # an install changed a rule: every assessment is stale
    loop.run_catchup()
    receipt = walk(loop, worker, clock)

    assert bounds(worker) == [(T - 31 * DAY, T), (T - 62 * DAY, T - 31 * DAY), (T - 90 * DAY, T - 62 * DAY)]
    assert all(before - after <= MAX_SLICE_SECONDS for after, before in bounds(worker))
    assert [run["max_assessed"] for run in worker.runs] == [500, 470, 440]   # one budget: what the pass has left
    assert all(run["ingested_after"] is None for run in worker.runs)
    assert worker.current == set(worker.rows)                 # days 31-90 reach the grant too
    assert len(loop.recorded) == 1                            # one receipt per pass, its slices summed
    assert (receipt.cause_class, receipt.scope, receipt.window_after, receipt.window_before) == \
        ("revision_change", "full_window", T - 90 * DAY, T)
    assert (receipt.state, receipt.scanned, receipt.assessed, receipt.budget_exhausted) == ("complete", 88, 88, False)
    state = stored_state(tmp_path)
    assert (state["assessment_revisions"], state["ingest_high_water"], state["continuation"]) == ({"rules": "r1"}, T,
                                                                                                  None)
    assert loop._interest_after_pass == (500 - 88, True)


def test_a_20_day_grant_still_runs_one_slice(tmp_path):
    T = daytime()
    clock, worker = Clock(T), SliceWorker(aged(T, range(0, 20)))
    loop = windowed(tmp_path, clock, worker, window=20 * DAY)
    loop.run_catchup()                                        # the backlog, on a node with no state file
    receipt = walk(loop, worker, clock)
    assert bounds(worker) == [(T - 20 * DAY, T)] and worker.runs[0]["max_assessed"] == 500
    assert (receipt.cause_class, receipt.window_after, receipt.window_before, receipt.assessed) == \
        ("startup_backlog", T - 20 * DAY, T, 20)
    assert stored_state(tmp_path)["last_full_pass_at"] == T


def test_window_slices_cover_the_window_with_no_gap_and_none_wider_than_the_worker_takes():
    T = 1_790_000_000
    assert window_slices(T - 20 * DAY, T) == [[T - 20 * DAY, T]]
    assert window_slices(T - 31 * DAY, T) == [[T - 31 * DAY, T]]
    assert window_slices(T - 31 * DAY - 1, T) == [[T - 31 * DAY, T], [T - 31 * DAY - 1, T - 31 * DAY]]
    for days in (1, 30, 62, 90, 365, 3650):
        slices = window_slices(T - days * DAY, T)
        assert slices[0][1] == T and slices[-1][0] == T - days * DAY
        assert all(newer[0] == older[1] for newer, older in zip(slices, slices[1:]))   # adjacent, newest first
        assert all(0 < before - after <= MAX_SLICE_SECONDS for after, before in slices)
    assert window_slices(T, T) == []


def test_the_slices_of_a_pass_share_one_budget_and_its_continuation_resumes_at_the_slice_it_stopped_in(tmp_path):
    T = daytime()
    clock, worker = Clock(T), SliceWorker(aged(T, [*NEWEST, *MIDDLE, *OLDEST]))
    loop = windowed(tmp_path, clock, worker, max_assessed=50)
    settled(loop, T)
    loop.revisions = {"rules": "r1"}
    loop.run_catchup()
    first = walk(loop, worker, clock)

    assert [run["max_assessed"] for run in worker.runs] == [50, 20]   # the second slice gets what the first left
    assert bounds(worker) == [(T - 31 * DAY, T), (T - 62 * DAY, T - 31 * DAY)]   # stopped there: no third run
    assert (first.assessed, first.budget_exhausted, first.window_after, first.window_before) == (50, True,
                                                                                                T - 90 * DAY, T)
    owed = stored_state(tmp_path)["continuation"]
    assert owed["slices"] == [[T - 62 * DAY, T - 31 * DAY], [T - 90 * DAY, T - 62 * DAY]] and owed["high_water"] == T
    assert (owed["origin"], owed["revisions"]) == ("revision_change", {"rules": "r1"})
    assert stored_state(tmp_path)["assessment_revisions"] == {"rules": "r0"}   # not done yet

    clock.now += 299
    loop.run_catchup()
    assert len(worker.runs) == 2                              # one interval of rest: the restore serves meanwhile
    restarted = windowed(tmp_path, clock, worker, max_assessed=50, revisions={"rules": "r1"})
    clock.now += 1
    restarted.run_catchup()                                   # owed across a restart, from the slice it stopped in
    assert bounds(worker)[2] == (T - 62 * DAY, T - 31 * DAY) and worker.runs[2]["max_assessed"] == 50
    last = walk(restarted, worker, clock)

    assert bounds(worker)[3] == (T - 90 * DAY, T - 62 * DAY) and worker.runs[3]["max_assessed"] == 40
    assert (last.cause_class, last.window_after, last.window_before) == ("budget_continuation", T - 90 * DAY,
                                                                         T - 31 * DAY)
    assert (last.scanned, last.assessed, last.current, last.budget_exhausted) == (58, 38, 20, False)
    assert worker.current == set(worker.rows)
    state = stored_state(tmp_path)
    assert (state["continuation"], state["assessment_revisions"]) == (None, {"rules": "r1"})
    # The newest slice was walked at T: rows ingested since then are new_ingest's, not skipped.
    assert state["ingest_high_water"] == T and state["last_full_pass_at"] == T - 3600


def test_a_pass_that_stops_in_the_newest_slice_walks_the_whole_window_again(tmp_path):
    T = daytime()
    clock, worker = Clock(T), SliceWorker(aged(T, NEWEST, per_age=2) | aged(T, MIDDLE))
    loop = windowed(tmp_path, clock, worker, max_assessed=50)
    settled(loop, T)
    loop.proof = "proof-1"
    loop.run_catchup()
    assert walk(loop, worker, clock).budget_exhausted and len(worker.runs) == 1
    owed = stored_state(tmp_path)["continuation"]
    assert (owed["slices"], owed["high_water"]) == (None, None)
    clock.now += 600
    T2 = clock.now
    loop.run_catchup()
    assert bounds(worker)[1] == (T2 - 31 * DAY, T2)           # planned at its own time, as before slices
    receipt = walk(loop, worker, clock)
    assert (receipt.cause_class, receipt.assessed, receipt.budget_exhausted) == ("budget_continuation", 40, False)
    assert len(worker.runs) == 4 and stored_state(tmp_path)["ingest_high_water"] == T2


def test_a_backlog_resumes_at_its_slice_under_the_same_rules_and_walks_it_all_again_under_new_ones(tmp_path):
    T = daytime()
    clock, worker = Clock(T), SliceWorker(aged(T, [*NEWEST, *MIDDLE, *OLDEST]))
    loop = windowed(tmp_path, clock, worker, max_assessed=50)
    loop.run_catchup()                                        # no state file: the backlog
    assert walk(loop, worker, clock).cause_class == "startup_backlog"
    assert stored_state(tmp_path)["continuation"]["slices"][0] == [T - 62 * DAY, T - 31 * DAY]
    clock.now += 300
    loop.run_catchup()
    assert bounds(worker)[2] == (T - 62 * DAY, T - 31 * DAY)   # the same rules: from the slice it stopped in
    receipt = walk(loop, worker, clock)
    assert (receipt.cause_class, receipt.budget_exhausted) == ("budget_continuation", False)
    state = stored_state(tmp_path)
    assert (state["last_full_pass_at"], state["ingest_high_water"]) == (clock.now - 10, T)

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    clock.now = T
    worker = SliceWorker(aged(T, [*NEWEST, *MIDDLE, *OLDEST]))
    loop = windowed(fresh, clock, worker, max_assessed=50)
    loop.run_catchup()
    walk(loop, worker, clock)
    loop.revisions = {"rules": "r1"}                          # a rule changed before the backlog's continuation
    clock.now += 300
    loop.run_catchup()
    assert bounds(worker)[2] == (clock.now - 31 * DAY, clock.now)   # its done slices are stale now: all again


def test_new_rows_recheck_their_conversations_over_the_whole_window_in_slices(tmp_path):
    """The changed_conversations scope had the same 31-day cut: an old conversation's neighbours were never
    re-checked when a row arrived."""
    T = daytime()
    clock, worker = Clock(T), SliceWorker(aged(T, [5, 45, 80]))
    loop = windowed(tmp_path, clock, worker, new_rows=True)
    settled(loop, T)
    loop.run_catchup()
    receipt = walk(loop, worker, clock)
    assert bounds(worker) == [(T - 31 * DAY, T), (T - 62 * DAY, T - 31 * DAY), (T - 90 * DAY, T - 62 * DAY)]
    assert {run["ingested_after"] for run in worker.runs} == {T - 3600}
    assert (receipt.cause_class, receipt.scope, receipt.window_after, receipt.assessed) == \
        ("new_ingest", "changed_conversations", T - 90 * DAY, 3)
    assert stored_state(tmp_path)["ingest_high_water"] == T


def test_the_state_moves_only_after_the_last_slice_and_a_restart_mid_pass_plans_again(tmp_path):
    T = daytime()
    clock, worker = Clock(T), SliceWorker(aged(T, [*NEWEST, *MIDDLE, *OLDEST]))
    loop = windowed(tmp_path, clock, worker, interests=True)
    settled(loop, T)
    loop._save_state()
    before = stored_state(tmp_path)
    loop.revisions = {"rules": "r1"}
    loop.run_catchup()
    for _ in range(2):                                        # two slices end, the third starts
        worker.end()
        clock.now += 5
        assert loop.run_catchup() is None
        assert stored_state(tmp_path) == before and loop._interest_after_pass is None and loop.recorded == []

    restarted = windowed(tmp_path, clock, worker, revisions={"rules": "r1"})   # the node restarts in the last slice
    assert restarted.run_catchup() is None and len(worker.runs) == 3   # it never touches a run it did not start
    worker.end()
    clock.now += 300
    restarted.run_catchup()                                   # nothing was marked done: the same cause, all again
    assert restarted._pass["cause"] == "revision_change" and bounds(worker)[3] == (clock.now - 31 * DAY, clock.now)
    receipt = walk(restarted, worker, clock)
    assert (receipt.state, receipt.assessed, receipt.current) == ("complete", 0, 88)   # done slices are current
    assert stored_state(tmp_path)["assessment_revisions"] == {"rules": "r1"}


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_a_slice_that_does_not_complete_ends_the_pass_with_nothing_marked_done(tmp_path, state):
    T = daytime()
    clock, worker = Clock(T), SliceWorker(aged(T, [*NEWEST, *MIDDLE, *OLDEST]))
    loop = windowed(tmp_path, clock, worker)
    settled(loop, T)
    loop.revisions = {"rules": "r1"}
    loop.run_catchup()
    worker.end()
    clock.now += 5
    assert loop.run_catchup() is None
    worker.end(state=state)
    clock.now += 5
    receipt = loop.run_catchup()
    assert (receipt.state, receipt.assessed, receipt.budget_exhausted, len(worker.runs)) == (state, 30, False, 2)
    assert not (tmp_path / STATE_FILE).exists() and loop._load_state()["assessment_revisions"] == {"rules": "r0"}
    clock.now += 300
    loop.run_catchup()
    assert loop._pass["cause"] == "revision_change" and bounds(worker)[2] == (clock.now - 31 * DAY, clock.now)


def test_a_slice_that_cannot_start_ends_the_pass_failed(tmp_path):
    T = daytime()
    clock, worker = Clock(T), SliceWorker()
    loop = windowed(tmp_path, clock, worker)
    loop.run_catchup()
    refuse = worker.start_node_pass

    def refused(request, **kwargs):
        raise PolicyError("owner_authority_required")

    worker.start_node_pass = refused
    worker.end()
    clock.now += 5
    receipt = loop.run_catchup()
    assert (receipt.state, receipt.cause_class) == ("failed", "startup_backlog") and loop._pass is None
    assert loop._load_state()["last_full_pass_at"] is None
    worker.start_node_pass = refuse


def test_no_restore_runs_between_the_slices_of_one_pass(tmp_path):
    """Each of its assessments moves the review digest, so a rebuild between two slices could only end `stale`. The
    pass holds the restore until its last slice ends, far past max_defer from the drop, then restores at once."""
    T = 1_790_000_000
    clock, worker = Clock(T), SliceWorker()
    index = FakeIndex(tmp_path, ["ready"])
    index.resolver = SimpleNamespace(path=tmp_path / "canonical.db")
    loop = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, window=90 * DAY, debounce=30,
                       max_defer=600)
    loop.step()                                               # the first slice starts
    drop(tmp_path, loop, index)                               # its first assessment dropped the index
    for n in range(3):
        for _ in range(100):                                  # 500 s of each slice publishing
            clock.now += 5
            worker.published += 1
            loop.step()
        worker.end()
        clock.now += 5
        loop.step()                                           # the next slice starts in this tick, or the pass ends
        if n < 2:
            assert loop._pass is not None and loop._pass["slice"] == n + 1 and index.calls == []
    assert len(worker.runs) == 3 and [r.assessed for r in loop.recorded if r.action != "search_index_restore"] == [300]
    assert index.calls == [clock.now]                         # one rebuild, when the pass ended


class NinetyDayLoop(CatchUpLoop):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, window=90 * DAY, **kwargs)


def test_a_sliced_daytime_pass_darkens_the_grant_for_the_pass_plus_one_rebuild(tmp_path):
    """The production timings of test_a_daytime_pass_darkens_the_grant_for_the_pass_plus_one_rebuild, for a pass
    of three slices: one dark stretch, no flicker or wasted rebuild between slices."""
    calls, per_call = [40, 15, 7], 4
    seen = dark_around_passes(tmp_path, calls=calls, per_call=per_call, loop_class=NinetyDayLoop)
    assert seen["passes"] == 3 and seen["rebuilds_stale"] == 0 and seen["rebuilds_ready"] == 1
    assert 0 < seen["back_after_last_pass"] <= 5 + 37
    assert seen["dark_span"] == seen["dark_seconds"] <= sum(calls) * per_call + len(calls) * 6 + 5 + 37


def test_a_real_90_day_knowledge_grant_is_walked_whole(legacy, tmp_path, monkeypatch):
    """The ledger's own p2c-v3 grant (the corpus's 90-day window), which the loop used to cut to its newest 31 days."""
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    now, worker = node.now[0], SliceWorker()
    loop = RefreshLoop(ledger=node.ledger, root=node.index.root, index=lambda: node.index, worker=lambda: worker,
                       settings=settings(catchup=True), clock=Clock(now))
    assert loop._window_seconds(now) == 90 * DAY
    loop.run_catchup()
    receipt = walk(loop, worker, loop.clock)
    assert bounds(worker) == [tuple(pair) for pair in window_slices(now - 90 * DAY, now)] and len(worker.runs) == 3
    assert (receipt.cause_class, receipt.window_after, receipt.window_before) == ("startup_backlog", now - 90 * DAY,
                                                                                  now)
    stored = [r for r in receipts(node) if r["action"] == "message_assessment_catchup"]
    assert [(r["window_after"], r["window_before"], r["state"]) for r in stored] == [(now - 90 * DAY, now, "complete")]


def test_the_real_worker_takes_each_slice_and_still_refuses_the_whole_window(legacy, tmp_path, monkeypatch):
    resolver, reviews, _, _ = setup(legacy)
    worker = AutomaticReviewWorker(resolver, reviews)
    seen = []

    async def process(request, **options):
        seen.append((request.after, request.before, options["ingested_after"], options["max_assessed"]))

    monkeypatch.setattr(worker, "_process", process)
    T = daytime()
    clock = Clock(T)
    loop = windowed(tmp_path, clock, worker)                  # owner-1, the fixture's owner
    loop.run_catchup()
    receipt = None
    for _ in range(3):
        worker._thread.join(5)
        clock.now += 5
        receipt = loop.run_catchup()
    assert seen == [(after, before, None, 500) for after, before in window_slices(T - 90 * DAY, T)]
    assert (receipt.state, receipt.window_after, receipt.window_before) == ("complete", T - 90 * DAY, T)
    with owner(), pytest.raises(PolicyError, match="message_review_window_invalid"):
        worker.start_node_pass(AutomaticReviewRequest(after=T - 90 * DAY, before=T), now=T)   # its guard stands


@pytest.mark.parametrize("slices,high_water", [
    ([[0, 31 * DAY + 1]], 31 * DAY + 1),                      # wider than the worker takes
    ([[5, 5]], 10),                                           # empty
    ([[20, 30], [0, 10]], 30),                                # a gap between slices
    ([[0, 10], [10, 20]], 20),                                # oldest first
    ([[0, 10]], 5),                                           # newer than the high-water mark it leaves
    ([[0, 10]], None), (None, 10), ([], 10), ("x", 10), ([[0, "10"]], 10), ([[0.0, 10]], 10), ([[True, 10]], 10),
    ([[0, 10, 20]], 30), ([[-5, 10]], 10)])
def test_a_continuation_with_malformed_slices_is_dropped(tmp_path, slices, high_water):
    """A start_node_pass that refuses its own arguments would fail every interval for good."""
    owed = {"scope": "full_window", "ingested_after": None, "origin": "revision_change", "revisions": {"rules": "r0"},
            "proof": "proof-0", "slices": slices, "high_water": high_water}
    (tmp_path / STATE_FILE).write_text(json.dumps({"version": "topos-search-refresh-state/v1", "names": [],
                                                   "ingest_high_water": 1, "last_full_pass_at": 1,
                                                   "continuation": owed}))
    assert CatchUpLoop(tmp_path, SimpleNamespace(), Clock(1))._load_state()["continuation"] is None


@pytest.mark.parametrize("extra", [{}, {"slices": [[40 * DAY, 70 * DAY], [10 * DAY, 40 * DAY]],
                                        "high_water": 100 * DAY}])
def test_a_continuation_with_no_slices_or_well_formed_ones_is_kept(tmp_path, extra):
    """A file written before slices owes the whole window again; a newer one owes the slices it names."""
    owed = {"scope": "changed_conversations", "ingested_after": 7, "origin": "new_ingest", "revisions": None,
            "proof": None, **extra}
    (tmp_path / STATE_FILE).write_text(json.dumps({"version": "topos-search-refresh-state/v1", "names": [],
                                                   "ingest_high_water": 7, "last_full_pass_at": 1,
                                                   "continuation": owed}))
    assert CatchUpLoop(tmp_path, SimpleNamespace(), Clock(1))._load_state()["continuation"] == owed
