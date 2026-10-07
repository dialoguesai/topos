"""A node's first pass does not wait five minutes for a share that was made seconds after sharing came on.

protects: the catch-up looks for work at most once per `catchup_interval` (300 s). A node's first look runs the
moment sharing comes on, before its owner has made any share, finds nothing shared and so nothing to assess for,
and used to spend the whole interval on that. The owner's first share, made seconds later, then waited up to five
minutes for the node's first pass: on the test bed the pass started 296 and 297 s after sharing came on, and a
recipient who asked in that time was told five times that the Topos "has no answer to that".

A look that found nothing shared now looks again after `no_share_recheck` (30 s). What must stay as it was:

- with a share active, the ledger is read once per interval, not per tick (`test_quiet_ticks_...` in
  test_refresh_loop.py, unchanged);
- with nothing shared, the ledger is still not read per tick: once per `no_share_recheck`;
- the pass that then starts is the same one, with the same cause, window and model budget.
"""
from __future__ import annotations

from types import SimpleNamespace

from tests.permissions_v2.test_refresh_loop import CatchUpLoop, Clock, FakeWorker, finish

T = 1_790_000_000
MONTH = 30 * 86_400


def _loop(tmp_path, clock, worker, *, shared_from: int | None, **settings):
    """A loop whose node has nothing shared until `shared_from` (None: never). Returns it and its ledger reads."""
    index = SimpleNamespace(root=tmp_path, resolver=SimpleNamespace(path=tmp_path / "canonical.db"))
    loop = CatchUpLoop(tmp_path, index, clock, worker=lambda: worker, catchup=True, catchup_interval=300,
                       max_assessed=50, **settings)
    reads = []

    def window(now):
        reads.append(now)
        return MONTH if shared_from is not None and now >= shared_from else None
    loop._window_seconds = window
    return loop, reads


def test_the_first_share_is_assessed_within_the_recheck_not_a_whole_interval_later(tmp_path):
    clock, worker = Clock(T), FakeWorker()
    loop, reads = _loop(tmp_path, clock, worker, shared_from=T + 4)   # sharing on at T, the first share 4 s later
    for offset in range(0, 31, 5):                                    # the loop's own 5 s ticks
        clock.now = T + offset
        loop.run_catchup()
    assert worker.started == [dict(after=T + 30 - MONTH, before=T + 30, ingested_after=None, max_assessed=50)]
    assert reads == [T, T + 30], "one look when sharing came on, one after the recheck; never one per tick"
    finish(worker, scanned=2, assessed=1, withheld=1)
    clock.now = T + 45
    receipt = loop.run_catchup()
    assert (receipt.cause_class, receipt.scope, receipt.assessed) == ("startup_backlog", "full_window", 1)


def test_with_nothing_shared_the_ledger_is_read_once_per_recheck_and_no_pass_starts(tmp_path):
    clock, worker = Clock(T), FakeWorker()
    loop, reads = _loop(tmp_path, clock, worker, shared_from=None)
    for offset in range(0, 121, 5):
        clock.now = T + offset
        loop.run_catchup()
    assert reads == [T, T + 30, T + 60, T + 90, T + 120]
    assert worker.started == []


def test_once_something_is_shared_the_interval_is_what_it_was(tmp_path):
    clock, worker = Clock(T), FakeWorker()
    loop, reads = _loop(tmp_path, clock, worker, shared_from=T)
    loop.run_catchup()
    finish(worker, scanned=1, assessed=1)
    for offset in (5, 30, 60, 299):
        clock.now = T + offset
        loop.run_catchup()
    assert reads == [T], "a look that found a share spends the whole interval, as before"
    clock.now = T + 310
    loop.run_catchup()
    assert reads == [T, T + 310]


def test_a_recheck_longer_than_the_interval_is_the_interval(tmp_path):
    clock, worker = Clock(T), FakeWorker()
    loop, reads = _loop(tmp_path, clock, worker, shared_from=None, no_share_recheck=900)
    for offset in (0, 30, 299, 300, 600):
        clock.now = T + offset
        loop.run_catchup()
    assert reads == [T, T + 300, T + 600]
