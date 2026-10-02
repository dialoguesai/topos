"""The restore sees every drop of an index it, or the owner, published (1.4.4).

Live on 2 Oct (14:32Z): the restore published a rebuilt index while a graph rebuild was writing; the daemon sweep, which
had waited 2.6 s on the publish's gate, found it stale a second later and dropped it. The loop's observation runs
after each sweep and compared the files it saw with the ones it saw 10 s before, when there was none, so the index
had never been seen and its drop was invisible. The restore had already counted the grant as done: no rebuild came
until the owner's own 55 minutes later. The index service now reports every publish (`take_published`), and an
index published since the last observation is a published index, gone or not.
"""
import json
from pathlib import Path

import pytest

from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401 (fixture)
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 (fixture)
from tests.permissions_v2.test_refresh_loop import (Clock, FakeIndex, ScheduledLoop, loop_for, owner_only, reassess,
                                                    receipts, restore_loop)
from tests.permissions_v2.test_relationship_revision import (GOAL, LABELS, edit, goal_graph, ids, indexed, kinds,
                                                             materialize)
from topos.permissions_v2.refresh_loop import STATE_FILE, protection_sync
from topos.permissions_v2.search_index import index_path


def move_validity(legacy, when):
    """A real change to what a relationship pins (its validity), as a goal recurring earlier would make it."""
    edge, _node = ids(legacy)
    edit(legacy, 'UPDATE entity_edges SET valid_from=? WHERE edge_id=?', (when, edge))


@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
def test_a_projection_change_is_rebuilt_with_no_owner_call(legacy, tmp_path, monkeypatch):
    node = indexed(legacy, tmp_path, monkeypatch)
    loop = loop_for(node, Clock(node.now[0]))
    loop.observe(node.index)
    move_validity(legacy, '2026-01-01T00:00:00+00:00')
    assert node.index.sweep(now=node.now[0]) == 1            # stale (projection): dropped, as before
    loop.observe(node.index)
    receipt = loop.run_pending()
    assert receipt.cause_classes == ['context_changed']
    assert [(g.grant_id, g.state) for g in receipt.grants] == [('grant-search', 'ready')]
    assert kinds(node) == {'message', 'goal', 'relationship'}
    assert [r['action'] for r in receipts(node)] == ['search_index_restore']


@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
def test_a_restored_index_dropped_before_any_observation_is_restored_again(legacy, tmp_path, monkeypatch):
    """2 Oct 14:32Z: the drop of a restore's own index, between its publish and the next observation."""
    node = indexed(legacy, tmp_path, monkeypatch)
    loop = loop_for(node, Clock(node.now[0]))
    loop.observe(node.index)
    move_validity(legacy, '2026-01-01T00:00:00+00:00')
    node.index.sweep(now=node.now[0])
    loop.observe(node.index)                                  # this observation sees no file
    assert loop.run_pending().grants[0].state == 'ready'      # the restore publishes...
    move_validity(legacy, '2025-12-01T00:00:00+00:00')
    assert node.index.sweep(now=node.now[0]) == 1            # ...and the next sweep drops it, unobserved

    loop.observe(node.index)
    receipt = loop.run_pending()
    assert receipt is not None and receipt.grants[0].state == 'ready'
    assert kinds(node) == {'message', 'goal', 'relationship'}


@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
def test_an_owner_build_dropped_before_any_observation_is_restored(legacy, tmp_path, monkeypatch):
    goal_graph(legacy)
    node, _identity = node_for(legacy, tmp_path, monkeypatch, labels=LABELS)
    loop = loop_for(node, Clock(node.now[0]))
    loop.observe(node.index)                                  # no index yet
    with owner():
        assert node.index.rebuild('grant-search', now=node.now[0])['state'] == 'ready'
    move_validity(legacy, '2026-01-01T00:00:00+00:00')
    assert node.index.sweep(now=node.now[0]) == 1
    loop.observe(node.index)
    receipt = loop.run_pending()
    assert receipt is not None and receipt.grants[0].state == 'ready'


@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
def test_a_restart_right_after_a_restore_still_sees_its_drop(legacy, tmp_path, monkeypatch):
    """The published set is in memory; the state file keeps what a restore published, gone or not."""
    node = indexed(legacy, tmp_path, monkeypatch)
    loop = loop_for(node, Clock(node.now[0]))
    loop.observe(node.index)
    move_validity(legacy, '2026-01-01T00:00:00+00:00')
    node.index.sweep(now=node.now[0])
    loop.observe(node.index)

    class Dropping:
        """The sweep that waited on the publish's gate: it drops the new index before the restore's pass ends."""
        def __init__(self, service):
            self.service = service

        def rebuild(self, grant_id, *, now):
            result = self.service.rebuild(grant_id, now=now)
            move_validity(legacy, '2025-12-01T00:00:00+00:00')
            assert self.service.sweep(now=now) == 1
            return result

    loop._index = lambda: Dropping(node.index)
    assert loop.run_pending().grants[0].state == 'ready'
    assert json.loads((node.index.root / STATE_FILE).read_text())['names'] == [index_path(node.index.root, 'grant-search').name]

    node.index.take_published()                               # the restart: a new process, no publishes in memory
    restarted = loop_for(node, Clock(node.now[0]))
    restarted.observe(node.index)
    receipt = restarted.run_pending()
    assert receipt.cause_classes == ['restart_gap'] and receipt.grants[0].state == 'ready'


@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
def test_a_graph_refresh_that_changes_nothing_drops_nothing_and_rebuilds_nothing(legacy, tmp_path, monkeypatch):
    node = indexed(legacy, tmp_path, monkeypatch)
    loop = loop_for(node, Clock(node.now[0]))
    loop.observe(node.index)
    materialize(legacy)
    assert node.index.sweep(now=node.now[0]) == 0
    loop.observe(node.index)
    assert loop.run_pending() is None and receipts(node) == []


@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
def test_a_published_index_is_the_baseline_a_later_drop_is_explained_against(legacy, tmp_path, monkeypatch):
    """An index that appeared since the last observation resets the signals a drop's cause is read against, whether
    the observation learnt of it from the listing or from the service: here the review changed before the build,
    and only the protection change after it dropped the index."""
    goal_graph(legacy)
    node, identity = node_for(legacy, tmp_path, monkeypatch, labels=LABELS)
    loop = restore_loop(node, sync_protection=protection_sync(node.protocol))
    loop.observe(node.index)                                  # no index yet
    reassess(node, identity)                                  # the review digest moves...
    node.rebuild()                                            # ...before the owner's build
    loop.observe(node.index)
    owner_only(legacy)
    assert node.index.sweep(now=node.now[0]) == 1
    loop.observe(node.index)
    assert loop.run_pending().cause_classes == ['protection_changed']


class PublishingIndex(FakeIndex):
    """A service whose publishes the observation learns of; `during_take` publishes while it is asked."""

    def __init__(self, root, results, *, during_take=None):
        super().__init__(root, results)
        self.published, self.during_take = set(), during_take

    def rebuild(self, grant_id, *, now):
        result = super().rebuild(grant_id, now=now)
        if result['state'] == 'ready':
            self.published.add(f'grant-{grant_id}.db')
        return result

    def take_published(self):
        if self.during_take:
            self.published.add(self.during_take())
        taken, self.published = self.published, set()
        return taken


def test_a_drop_of_an_index_published_between_observations_is_queued(tmp_path):
    clock = Clock(1000)
    index = PublishingIndex(tmp_path, ['ready', 'ready'])
    loop = ScheduledLoop(tmp_path, index, clock)
    loop.observe(index)                                       # nothing published yet
    index.rebuild('g1', now=1000)                              # the owner's build...
    (tmp_path / 'grant-g1.db').unlink()                        # ...dropped before any observation
    loop.observe(index)
    receipt = loop.run_pending()
    assert [g.grant_id for g in receipt.grants] == ['g1'] and receipt.grants[0].state == 'ready'
    assert len(index.calls) == 2


def test_an_index_published_while_the_files_are_listed_is_not_a_drop(tmp_path):
    """The service is asked before the directory is listed: a publish that lands in between is seen in the listing,
    never taken for a drop."""
    def publish():
        (tmp_path / 'grant-g1.db').write_bytes(b'')
        return 'grant-g1.db'

    clock = Clock(1000)
    index = PublishingIndex(tmp_path, [], during_take=publish)
    loop = ScheduledLoop(tmp_path, index, clock)
    loop.observe(index)
    assert loop._pending == {} and loop.run_pending() is None


def test_a_service_that_reports_no_publishes_is_observed_as_before(tmp_path):
    clock = Clock(1000)
    index = FakeIndex(tmp_path, ['ready'])
    loop = ScheduledLoop(tmp_path, index, clock)
    path = Path(tmp_path) / 'grant-g1.db'
    path.write_bytes(b'')
    loop.observe(index)
    path.unlink()
    loop.observe(index)
    assert loop.run_pending().grants[0].state == 'ready'


def test_a_given_up_restore_is_not_queued_again_by_an_old_publish(tmp_path):
    """A publish is reported once: after the restore gives up, the grant stays dark until the owner acts, as before."""
    clock = Clock(1000)
    index = PublishingIndex(tmp_path, ['ready', 'failed'])
    loop = ScheduledLoop(tmp_path, index, clock, max_attempts=1)
    loop.observe(index)
    index.rebuild('g1', now=1000)
    (tmp_path / 'grant-g1.db').unlink()
    loop.observe(index)
    assert loop.run_pending().grants[0].state == 'failed'      # given up after one attempt
    loop.observe(index)
    assert loop._pending == {} and loop.run_pending() is None


@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
def test_the_service_reports_each_publish_once(legacy, tmp_path, monkeypatch):
    node = indexed(legacy, tmp_path, monkeypatch)
    assert node.index.take_published() == {index_path(node.index.root, 'grant-search').name}
    assert node.index.take_published() == set()
    node.index.forget('grant-search')                          # a removal is not a publish
    assert node.index.take_published() == set()
