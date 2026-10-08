"""BL-148: a share's mode or scope change brings its index back promptly, through the owner hook, and across a restart.

Measured live on 8 Oct: a share switched from answers to records was empty from about 17:51Z to about 17:58Z. The
control plane signs a new `validity.starts_at` with every change, so a mode change is never a light change: the index
built under the old policy is refused at once (its basis binds the old policy hash), the next sweep drops it, and the
share is dark until the owner-change queue (index_rebuilds.py) publishes the new one. When that build did not publish
(it ended `stale`, because something it is judged by moved while it ran, or it failed), nothing tried again but the
refresh loop's restore, whose passes run at least `min_interval` (300 s) apart. And a node quit inside the window lost
the queue, which lives in memory.

The rule now (`IndexRebuilds`): a build an owner change asked for that does not publish is tried again by the queue
itself, promptly and a bounded number of times, and stays owed meanwhile (the restore leaves it to the queue); what is
owed is kept on disk and asked for again at the next start (`request_owed`). Every try is a whole `rebuild` with every
check it makes, and the old index is never served under the new policy.

Here: the N3 node with ten shares (invented people and corpus), the real signed change through the real owner hook, a
real sweep, and the refresh loop with its production interval, its clock standing still: so nothing the loop does can
bring the index back, and only the queue can.
"""
from __future__ import annotations

import json
import time

import pytest

from tests.permissions_v2.test_n3_many_shares import (SHARES, checked, identities, many, search, send_change,  # noqa: F401
                                                      share_policy, signed_change, spy_builds)
from topos.permissions_v2 import index_rebuilds as rebuilds_module
from topos.permissions_v2.index_rebuilds import OWED_FILE, IndexRebuilds
from topos.permissions_v2.refresh_loop import (RefreshLoop, RefreshSettings, protection_sync, queue_missing_indexes,
                                               restore_at_start)
from topos.permissions_v2.search_index import SearchIndexService, index_path

pytestmark = pytest.mark.public


def mode_change(node, number: int, *, generation: int, command_id: str):
    """What the app's mode switch signs: the share's answers mode, and a new `validity.starts_at`."""
    raw = share_policy(number, version=f"policy-{SHARES[number]}-{command_id}")
    raw["search"]["answers"] = "records"
    raw["validity"] = {**raw["validity"], "starts_at": raw["validity"]["starts_at"] - generation}
    return signed_change(node, SHARES[number], generation=generation, command_id=command_id, policy_raw=raw)


def production_loop(node) -> RefreshLoop:
    """The restore as it runs on a node (300 s between passes, 30 s debounce), on a clock that never moves."""
    return RefreshLoop(ledger=node.ledger, root=node.index.root, index=lambda: node.index, worker=None,
                       settings=RefreshSettings(restore=True), clock=lambda: node.now[0],
                       sync_protection=protection_sync(node.protocol), owed=node.runtime.index_rebuilds().owed)


def first_builds_end_stale(monkeypatch, count: int) -> list:
    """The first `count` builds end `stale` (as when a review or the protection clock moves under them), as a whole
    `rebuild` ends after its own tries: nothing published, the index purged."""
    from topos.permissions_v2.search_index import purge
    states = []
    real = SearchIndexService._rebuild

    def rebuild(self, grant_id, *, now=None):
        if len(states) < count:
            purge(self.root, grant_id)
            states.append("stale")
            return {"state": "stale", "member_count": 0}
        result = real(self, grant_id, now=now)
        states.append(result["state"])
        return result
    monkeypatch.setattr(SearchIndexService, "_rebuild", rebuild)
    return states


@pytest.fixture
def quick_tries(monkeypatch):
    monkeypatch.setattr(IndexRebuilds, "RETRY_DELAYS", (0.05, 0.1, 0.2))


@pytest.mark.asyncio
async def test_a_mode_change_on_a_node_with_an_index_is_ready_again_after_one_build(many, monkeypatch):
    node, rebuilds = many
    loop = production_loop(node)
    loop.observe(node.index)
    built = spy_builds(monkeypatch)
    change = mode_change(node, 3, generation=2, command_id="mode-share-03")
    assert checked(node, (await send_change(node, change))[0], change).outcome == "applied"
    started = time.monotonic()
    assert rebuilds.wait_idle(30)
    assert built == [SHARES[3]] and rebuilds.results[-1][0] == "ready"      # one build, no restamp
    assert time.monotonic() - started < 30                                   # never the 300 s interval
    node.index.sweep(now=node.now[0])
    loop.observe(node.index)
    assert loop.run_pending() is None and SHARES[3] not in loop._pending    # nothing left for the restore
    output, refused = search(node, 3)
    assert refused is None and output["records"]


@pytest.mark.asyncio
async def test_a_build_that_did_not_publish_is_tried_again_by_the_queue_not_left_to_the_restore(many, monkeypatch,
                                                                                               quick_tries):
    """Rule: `IndexRebuilds._run` queues a further try of a build an owner change asked for that ended neither ready
    nor removed. Take it out and the share stays dark: the restore's clock never reaches its interval here."""
    node, rebuilds = many
    loop = production_loop(node)
    loop.observe(node.index)
    states = first_builds_end_stale(monkeypatch, 2)
    change = mode_change(node, 5, generation=2, command_id="mode-share-05")
    assert checked(node, (await send_change(node, change))[0], change).outcome == "applied"
    node.index.sweep(now=node.now[0])                                       # the old index goes at the next sweep
    loop.observe(node.index)
    assert SHARES[5] in loop._pending and loop.run_pending() is None        # the restore waits (and the queue owes)
    assert rebuilds.wait_idle(30)
    assert states == ["stale", "stale", "ready"]
    assert index_path(node.index.root, SHARES[5]).exists()
    output, refused = search(node, 5)
    assert refused is None and output["records"]
    loop.observe(node.index)
    assert SHARES[5] not in loop._pending                                   # the publish settled the restore


@pytest.mark.asyncio
async def test_the_tries_are_bounded_and_then_the_restore_has_it(many, monkeypatch, quick_tries):
    node, rebuilds = many
    states = first_builds_end_stale(monkeypatch, 10)
    change = mode_change(node, 6, generation=2, command_id="mode-share-06")
    assert checked(node, (await send_change(node, change))[0], change).outcome == "applied"
    assert rebuilds.wait_idle(30)
    assert states == ["stale"] * (1 + len(IndexRebuilds.RETRY_DELAYS))
    assert SHARES[6] not in rebuilds.owed()                                 # the restore's to build now
    output, refused = search(node, 6)
    assert output is None and refused is not None                          # dark, never the old index


@pytest.mark.asyncio
async def test_the_old_index_is_never_served_under_the_new_policy_between_tries(many, monkeypatch):
    node, rebuilds = many
    monkeypatch.setattr(IndexRebuilds, "RETRY_DELAYS", (1.0,))
    first_builds_end_stale(monkeypatch, 1)
    before = identities(node)[SHARES[2]]
    change = mode_change(node, 2, generation=2, command_id="mode-share-02")
    assert checked(node, (await send_change(node, change))[0], change).outcome == "applied"
    deadline = time.monotonic() + 10
    while rebuilds.results == type(rebuilds.results)() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert SHARES[2] in rebuilds.owed()                                     # waiting for its next try
    output, refused = search(node, 2)
    assert output is None and refused is not None
    assert rebuilds.wait_idle(30) and identities(node)[SHARES[2]] != before
    output, refused = search(node, 2)
    assert refused is None and output["records"]


@pytest.mark.asyncio
async def test_a_quit_inside_the_window_rebuilds_at_the_next_start(many, monkeypatch):
    """Rule: `request_owed`, from `queue_missing_indexes` and the start-up restore. The node acknowledged the change
    and quit before its build: the older index file is still there (stale), so "missing" alone does not ask for it."""
    node, rebuilds = many
    rebuilds.close()                                                         # this run builds nothing more
    stopped = IndexRebuilds(ledger=node.ledger, root=node.index.root, index=lambda: node.index)
    stopped._closed = False

    def start_no_thread():                                                  # acknowledged; the build never starts
        stopped._thread = object()
    start_no_thread()
    node.runtime.index_rebuilds = lambda: stopped
    change = mode_change(node, 8, generation=2, command_id="mode-share-08")
    assert checked(node, (await send_change(node, change))[0], change).outcome == "applied"
    assert json.loads((node.index.root / OWED_FILE).read_text("utf-8")) == [SHARES[8]]
    assert index_path(node.index.root, SHARES[8]).exists()                  # the old one, under the old policy
    stopped.close()                                                          # the quit

    restarted = IndexRebuilds(ledger=node.ledger, root=node.index.root, index=lambda: node.index,
                              sync_protection=protection_sync(node.protocol))
    node.runtime.index_rebuilds = lambda: restarted
    try:
        assert queue_missing_indexes(node.runtime) == [SHARES[8]]
        assert restarted.wait_idle(30) and restarted.results[-1][0] == "ready"
        output, refused = search(node, 8)
        assert refused is None and output["records"]
        assert json.loads((node.index.root / OWED_FILE).read_text("utf-8")) == []
        assert queue_missing_indexes(node.runtime) == []                    # nothing owed, nothing missing
    finally:
        restarted.close()


def test_the_start_up_restore_asks_for_what_was_owed_once(many, monkeypatch):
    node, rebuilds = many
    (node.index.root / OWED_FILE).write_text(json.dumps([SHARES[1], "grant-that-is-gone"]), "utf-8")
    asked = []
    monkeypatch.setattr(rebuilds, "request", lambda grant_ids=None: asked.append(list(grant_ids)) or list(grant_ids))
    monkeypatch.setattr(node.runtime, "message_search_index", lambda: node.index, raising=False)
    state: dict = {}
    restore_at_start(node.runtime, state=state)
    restore_at_start(node.runtime, state=state)
    assert asked[0] == [SHARES[1]] and [SHARES[1]] not in asked[1:]


def test_an_unreadable_owed_file_asks_for_nothing(many):
    node, rebuilds = many
    for text in ("not json", json.dumps({"grant": SHARES[1]}), json.dumps([7])):
        (node.index.root / OWED_FILE).write_text(text, "utf-8")
        assert rebuilds.request_owed() == []


def test_the_retry_delays_are_short_and_few():
    """Promptly, and bounded: the whole schedule is well under the restore's 300 s interval."""
    assert 0 < len(rebuilds_module.IndexRebuilds.RETRY_DELAYS) <= 4
    assert sum(rebuilds_module.IndexRebuilds.RETRY_DELAYS) < RefreshSettings().min_interval / 2
