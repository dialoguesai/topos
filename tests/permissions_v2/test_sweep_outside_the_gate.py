"""The daemon sweep checks outside the write gate (1.4.4).

Until 1.4.4 the sweep held the process-wide write gate across its whole check. With a 502-member grant that was about
9 s every sweep, 39% of the gate while the index existed (owner's node, 2 Oct), and every writer waited behind it,
recipients' searches included. It now checks on its own read snapshot and enters the gate only for brief steps: the
grants' authority, the review digest, and each removal, which re-reads under the gate the identity of the file the
check read and the grant's authority. A recipient's own gated recheck before release is unchanged.
"""
import functools
import logging
import threading
import time

import pytest

from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401 (fixture)
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 (fixture)
from tests.permissions_v2.test_relationship_revision import edit, ids, indexed, kinds
from topos.permissions_v2 import search_index, search_timing
from topos.permissions_v2.search_index import SearchIndexService, index_path
from topos.storage.db import write_gate

FLAG = "TOPOS_PERMISSIONS_V2_SEARCH_TIMINGS"


def make_stale(legacy, when='2026-01-01T00:00:00+00:00'):
    """A real change to what a relationship member pins: the index the next check reads is stale."""
    edge, _node = ids(legacy)
    edit(legacy, 'UPDATE entity_edges SET valid_from=? WHERE edge_id=?', (when, edge))


def slow_check(monkeypatch, *, during=None, seconds=0.0):
    """Every member check signals `checking` and pauses; the first one runs `during()` first."""
    original = SearchIndexService._members_current
    state = {"calls": 0, "checking": threading.Event()}

    def members_current(self, *args, **kwargs):
        state["calls"] += 1
        state["checking"].set()
        if during is not None and state["calls"] == 1:
            during()
        time.sleep(seconds)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(SearchIndexService, "_members_current", members_current)
    return state


def on_another_thread(work, timeout=10):
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    thread.join(timeout)


@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
@pytest.mark.parametrize('via', ['sweep', 'timed_sweep'])
def test_the_check_runs_outside_the_write_gate(legacy, tmp_path, monkeypatch, caplog, via):
    """A writer that wants the gate while the sweep checks gets it at once, with timing on (the daemon's path on the
    owner's node) or off. The sweep's own holds are its brief steps."""
    node = indexed(legacy, tmp_path, monkeypatch)
    monkeypatch.setenv(FLAG, 'true')
    monkeypatch.setattr(node.index, 'sweep', functools.partial(SearchIndexService.sweep, node.index, now=node.now[0]))
    state = slow_check(monkeypatch, seconds=0.5)
    waits = []

    def writer():
        state["checking"].wait(5)
        began = time.monotonic()
        with write_gate.with_db_write():
            waits.append(time.monotonic() - began)
    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    with caplog.at_level(logging.INFO, logger='topos.permissions_v2.search_timing'):
        removed = node.index.sweep() if via == 'sweep' else search_timing.timed_sweep(node.index)
    thread.join(5)
    assert removed == 0 and state["calls"] == 1
    assert waits and waits[0] < 0.25                          # not the 0.5 s the check took
    holds = node.index._sweep_stats.holds
    assert len(holds) == 2                                    # the grants' authority, the review digest
    assert max(hold for _at, hold, _wait in holds) < 0.25
    if via == 'timed_sweep':
        [line] = [r.getMessage() for r in caplog.records if 'stage=sweep_hold' in r.getMessage()]
        fields = dict(pair.split('=', 1) for pair in line.split() if '=' in pair)
        assert float(fields['elapsed_ms']) < 250 and float(fields['check_ms']) >= 450 and fields['holds'] == '2'


@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
def test_a_stale_index_is_still_removed_and_under_the_gate(legacy, tmp_path, monkeypatch):
    node = indexed(legacy, tmp_path, monkeypatch)
    make_stale(legacy)
    owned = []
    shred = search_index._shred

    def recording(path):
        owned.append(write_gate.db_write_lock()._is_owned())
        shred(path)
    monkeypatch.setattr(search_index, '_shred', recording)
    assert node.index.sweep(now=node.now[0]) == 1
    assert owned == [True] and not index_path(node.index.root, 'grant-search').exists()
    assert len(node.index._sweep_stats.holds) == 3            # authority, digest, the removal


@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
def test_an_index_published_while_the_check_runs_is_not_removed(legacy, tmp_path, monkeypatch):
    """The check reads a stale index; meanwhile the owner's rebuild publishes a current one at the same path. The
    removal re-reads the file's identity under the gate and leaves the new file alone."""
    node = indexed(legacy, tmp_path, monkeypatch)
    make_stale(legacy)
    published = []

    def rebuild():
        def run():
            with owner():
                published.append(node.index.rebuild('grant-search', now=node.now[0])['state'])
        on_another_thread(run)
    slow_check(monkeypatch, during=rebuild)
    assert node.index.sweep(now=node.now[0]) == 0
    assert published == ['ready'] and index_path(node.index.root, 'grant-search').exists()
    assert node.index.sweep(now=node.now[0]) == 0             # the published index is the current one
    assert 'relationship' in kinds(node)


@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
def test_a_grant_revoked_while_the_check_runs_loses_its_key(legacy, tmp_path, monkeypatch):
    """The removal reads the grant's authority under the gate: revoked by then, its record-id key goes too."""
    node = indexed(legacy, tmp_path, monkeypatch)
    make_stale(legacy)

    def revoke():
        with owner():
            node.ledger.revoke('grant-search', expected_epoch=node.epoch(), command_id='revoke-during-check')
    slow_check(monkeypatch, during=revoke)
    assert node.index.keys.get('grant-search', create=False) is not None
    assert node.index.sweep(now=node.now[0]) == 1
    assert node.index.keys.get('grant-search', create=False) is None


@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
def test_a_check_that_cannot_run_still_removes_every_index_under_the_gate(legacy, tmp_path, monkeypatch):
    node = indexed(legacy, tmp_path, monkeypatch)
    monkeypatch.setattr(SearchIndexService, '_current', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('transient')))
    owned = []
    purge_all = search_index.purge_all
    monkeypatch.setattr(search_index, 'purge_all',
                        lambda root: owned.append(write_gate.db_write_lock()._is_owned()) or purge_all(root))
    assert node.index.sweep(now=node.now[0]) == 1
    assert owned == [True] and not list(node.index.root.glob('grant-*.db'))
