"""IF-6 §10: the restore queue's `facts_changed` cause.

protects: a fact the extractor writes after a grant's index was built moves no index basis and no member row, so no
drift drops the index and an inferred fact would wait for an unrelated rebuild. With the derived-facts flag on (and
the journal family it needs), `RefreshLoop.observe` keeps a digest of the facts; when it moves, the knowledge grants
that could release an inferred fact (v1: they sign `journal_entry` and `fact`) and have an index here are queued
on the restore's own queue, with its debounce, interval and backoff. Pinned here:
  - only those grants are queued, once, after the debounce; never a grant that never had an index;
  - nothing is read or queued with the flag off, or without the journal family;
  - the first observation records the digest; a restart compares with the one kept in the state file;
  - a fact written while a grant's rebuild runs is not lost when that rebuild finishes;
  - receipts carry the cause and grant ids, and stay readable by the census.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_journal_family import _journal_policy
from tests.permissions_v2.test_knowledge_search import knowledge_policy
from tests.permissions_v2.test_refresh_loop import Clock, settings
from topos.permissions_v2.evidence_families import JOURNAL_FLAG
from topos.permissions_v2.inferred_facts import FLAG
from topos.permissions_v2.refresh_loop import STATE_FILE, RefreshLoop, RefreshSettings, RestoreReceipt, fact_digest
from topos.permissions_v2.registry import parse_policy
from topos.permissions_v2.search_index import index_path

RESTORE = "TOPOS_PERMISSIONS_V2_INDEX_RESTORE_ENABLED"


def canonical(tmp_path):
    path = tmp_path / "canonical.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE signal_objects (object_id TEXT PRIMARY KEY, object_type TEXT NOT NULL, "
                     "valid_from TEXT NOT NULL, valid_to TEXT)")
        conn.execute("INSERT INTO signal_objects VALUES ('f-1','fact','2026-09-01',NULL)")
        conn.execute("INSERT INTO signal_objects VALUES ('o-1','browsing_interest','2026-09-01',NULL)")
    return path


def write(path, sql, *args):
    with sqlite3.connect(path) as conn:
        conn.execute(sql, args)


class Service:
    """The index service as the loop sees it: the canonical path, and rebuilds recorded."""

    def __init__(self, path, root, during=None):
        self.resolver = SimpleNamespace(path=path)
        self.root, self.rebuilds, self.during = root, [], during

    def rebuild(self, grant_id, *, now):
        self.rebuilds.append(grant_id)
        if self.during is not None:
            self.during(grant_id)
        return {"state": "ready", "member_count": 1}


GRANTS = {
    "g-journal": lambda: _journal_policy(),                                      # signs journal_entry and fact
    "g-unbuilt": lambda: _journal_policy(),                                      # signs them; never had an index
    "g-no-option": lambda: _journal_policy(kinds=("message", "fact", "goal", "relationship")),
    "g-no-fact": lambda: _journal_policy(kinds=("message", "journal_entry")),    # can release no fact at all
    "g-messages": lambda: knowledge_policy(),
}


class FactsLoop(RefreshLoop):
    """Scheduling with the ledger replaced: the grants above, receipts kept in memory."""

    def __init__(self, root, service, clock, **overrides):
        ledger = SimpleNamespace(identity=SimpleNamespace(owner_id="owner-1"))
        super().__init__(ledger=ledger, root=root, index=lambda: service, worker=None,
                         settings=settings(**{"facts": True, "debounce": 30, **overrides}), clock=clock)
        self.recorded = []

    def _active_grants(self, now):
        return [(grant_id, None, parse_policy(make())) for grant_id, make in GRANTS.items()]

    def _policy_hash(self, grant_id, now):
        return "a" * 64

    def _current_signals(self, service):
        return ("digest", ("clock", 1))

    def _record(self, receipt):
        self.recorded.append(receipt)


def loop_for(tmp_path, clock, *, during=None, **overrides):
    root = tmp_path / "index"
    root.mkdir(exist_ok=True)
    for grant_id in ("g-journal", "g-no-option", "g-no-fact", "g-messages"):
        index_path(root, grant_id).write_bytes(b"")
    service = Service(canonical(tmp_path), root, during)
    return FactsLoop(root, service, clock, **overrides), service


def test_new_facts_queue_one_rebuild_of_the_grants_that_could_release_them(tmp_path):
    clock = Clock(1000)
    loop, service = loop_for(tmp_path, clock)
    loop.observe(service)                                     # the first observation only records the digest
    assert loop._pending == {}
    write(service.resolver.path, "INSERT INTO signal_objects VALUES ('f-2','fact','2026-09-02',NULL)")
    loop.observe(service)
    assert set(loop._pending) == {"g-journal"} and loop._pending["g-journal"]["causes"] == {"facts_changed"}
    loop.observe(service)                                     # nothing moved since: nothing more
    clock.now = 1029
    assert loop.run_pending() is None and service.rebuilds == []     # the restore's own debounce
    clock.now = 1030
    receipt = loop.run_pending()
    assert receipt.cause_classes == ["facts_changed"] and service.rebuilds == ["g-journal"]
    assert [(g.grant_id, g.state) for g in receipt.grants] == [("g-journal", "ready")] and loop._pending == {}


@pytest.mark.parametrize("change", [
    "INSERT INTO signal_objects VALUES ('f-2','fact','2026-08-01',NULL)",                   # a new fact, older
    "UPDATE signal_objects SET valid_to='2026-09-20' WHERE object_id='f-1'",               # a closure
    "DELETE FROM signal_objects WHERE object_id='f-1'",                                     # a deletion
])
def test_the_digest_moves_with_each_fact_change(tmp_path, change):
    path = canonical(tmp_path)
    with sqlite3.connect(path) as conn:
        before = fact_digest(conn)
    write(path, change)
    with sqlite3.connect(path) as conn:
        assert fact_digest(conn) != before


def test_the_digest_ignores_other_objects(tmp_path):
    path = canonical(tmp_path)
    with sqlite3.connect(path) as conn:
        before = fact_digest(conn)
    write(path, "INSERT INTO signal_objects VALUES ('o-2','browsing_interest','2026-09-30',NULL)")
    write(path, "UPDATE signal_objects SET valid_to='2026-09-30' WHERE object_id='o-1'")
    with sqlite3.connect(path) as conn:
        assert fact_digest(conn) == before


def test_with_the_flag_off_nothing_is_read_or_queued(tmp_path, monkeypatch):
    clock = Clock(1000)
    loop, service = loop_for(tmp_path, clock, facts=False)
    monkeypatch.setattr(loop, "_fact_digest", lambda service: pytest.fail("facts read with the flag off"))
    loop.observe(service)
    write(service.resolver.path, "INSERT INTO signal_objects VALUES ('f-2','fact','2026-09-02',NULL)")
    loop.observe(service)
    clock.now = 2000
    assert loop.run_pending() is None and loop._pending == {} and service.rebuilds == []


def test_the_hook_needs_restore_the_flag_and_the_journal_family():
    on = {RESTORE: "true", FLAG: "true", JOURNAL_FLAG: "true"}
    assert RefreshSettings.from_env(on).facts is True
    assert RefreshSettings.from_env({**on, FLAG: "false"}).facts is False
    assert RefreshSettings.from_env({k: v for k, v in on.items() if k != JOURNAL_FLAG}).facts is False
    assert RefreshSettings.from_env({k: v for k, v in on.items() if k != RESTORE}).facts is False
    assert RefreshSettings.from_env({}).facts is False


def test_a_restart_compares_with_the_digest_it_kept(tmp_path):
    clock = Clock(1000)
    loop, service = loop_for(tmp_path, clock)
    loop.observe(service)
    stored = json.loads((loop.root / STATE_FILE).read_text())["fact_digest"]
    assert isinstance(stored, str)
    write(service.resolver.path, "INSERT INTO signal_objects VALUES ('f-2','fact','2026-09-02',NULL)")
    restarted = FactsLoop(loop.root, service, clock)
    restarted.observe(service)                                # a fact written across the restart is still seen
    assert set(restarted._pending) == {"g-journal"}
    again = FactsLoop(loop.root, service, clock)
    again.observe(service)                                    # nothing moved since the last one kept
    assert again._pending == {}


def test_a_fact_written_while_its_grant_rebuilds_gets_one_more_rebuild(tmp_path):
    """The rebuild's snapshot may predate a fact written while it runs; finishing must not drop that fact's turn."""
    clock = Clock(1000)
    written = []

    def during(grant_id):
        if not written:
            written.append(grant_id)
            write(loop.root.parent / "canonical.db", "INSERT INTO signal_objects VALUES ('f-3','fact','2026-09-03',NULL)")
            loop.observe(service)                             # the sweeper, while the rebuild runs
    loop, service = loop_for(tmp_path, clock, during=during, min_interval=300)
    loop.observe(service)
    write(service.resolver.path, "INSERT INTO signal_objects VALUES ('f-2','fact','2026-09-02',NULL)")
    loop.observe(service)
    clock.now = 1030
    assert loop.run_pending().cause_classes == ["facts_changed"] and service.rebuilds == ["g-journal"]
    entry = loop._pending["g-journal"]                         # kept, as a fresh entry
    assert (entry["causes"], entry["attempts"], entry.get("running"), "again" in entry) == \
        ({"facts_changed"}, 0, False, False)
    clock.now = 1060
    assert loop.run_pending() is None                          # the restore's min_interval still holds
    clock.now = 1330
    assert loop.run_pending().cause_classes == ["facts_changed"] and service.rebuilds == ["g-journal", "g-journal"]
    assert loop._pending == {}


def test_a_dropped_index_is_restored_once_as_before(tmp_path):
    """Only a fact move re-arms a finished entry: the restore's own causes finish and leave the queue as before."""
    clock = Clock(1000)
    loop, service = loop_for(tmp_path, clock, facts=False)
    path = index_path(loop.root, "g-journal")
    loop.observe(service)
    path.unlink()
    loop._dropped_grants = lambda now, names: ["g-journal"]
    loop.observe(service)
    clock.now = 1030
    assert loop.run_pending().cause_classes == ["context_changed"] and loop._pending == {}


def test_the_receipt_with_the_new_cause_reads_back():
    receipt = RestoreReceipt(version="topos-node-system-action/v1", action="search_index_restore",
                             actor="node_system", cause_classes=["facts_changed"], first_drop_at=1, started_at=2,
                             finished_at=3, protection_synced=False, grants=[])
    assert RestoreReceipt.model_validate(json.loads(json.dumps(receipt.model_dump()))) == receipt


def test_a_failing_fact_check_never_stops_the_drop_check_and_is_seen_again(tmp_path, monkeypatch):
    clock = Clock(1000)
    loop, service = loop_for(tmp_path, clock)
    loop.observe(service)
    write(service.resolver.path, "INSERT INTO signal_objects VALUES ('f-2','fact','2026-09-02',NULL)")

    def unavailable(now):
        raise RuntimeError("ledger unavailable")
    monkeypatch.setattr(loop, "_fact_grants", unavailable)
    index_path(loop.root, "g-journal").unlink()
    loop._dropped_grants = lambda now, names: ["g-journal"]
    loop.observe(service)                                     # never raises; the drop is still queued
    assert loop._pending["g-journal"]["causes"] == {"context_changed"}
    monkeypatch.undo()
    loop.observe(service)                                     # the same fact move, seen on the next sweep
    assert loop._pending["g-journal"]["causes"] == {"context_changed", "facts_changed"}
