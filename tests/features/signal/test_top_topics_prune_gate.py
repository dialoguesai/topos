"""The stale top_topics prune deletes under the write gate and commits its own work.

Ungated and uncommitted, its DELETE took SQLite's write lock at execute time, and the same thread then queued for
the gate in ``put_fact``. A job claim already holding the gate sat in ``BEGIN IMMEDIATE`` busy retries against that
lock for ~160 s, twice, and every writer in the node waited behind it (a 5-minute stall under load). What these
stop: a prune that leaves its connection in a transaction, and one that holds SQLite's lock while waiting for the gate.
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from topos.features.signal import topic_clustering
from topos.features.signal.topic_clustering import _prune_stale_top_topics_facts, write_top_topics_signal_facts
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db import write_gate
from topos.storage.db.migrations import apply_all_migrations

ACTIVE = "cluster-active"
STALE = "cluster-gone"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "topics.db"
    conn = sqlite3.connect(str(path))
    apply_all_migrations(conn)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO topic_clusters (cluster_id, label, member_count, source_mix_json) VALUES (?, 'Active', 4, ?)",
        (ACTIVE, '{"browser_visits": 4}'),
    )
    for cluster_id in (ACTIVE, STALE):
        conn.execute(
            "INSERT INTO signal_facts (fact_id, dimension, source_id, record_id, payload_json) VALUES (?, 'memory', 'x', ?, '{}')",
            (topic_clustering._top_topics_fact_id(cluster_id), cluster_id),
        )
    conn.commit()
    conn.close()
    return str(path)


def _fact_ids(path: str) -> set:
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT fact_id FROM signal_facts")}
    finally:
        conn.close()


def test_the_prune_commits_and_leaves_no_open_transaction(db) -> None:
    conn = sqlite3.connect(db)
    try:
        assert _prune_stale_top_topics_facts(conn, [ACTIVE]) == 1
        assert not conn.in_transaction
    finally:
        conn.close()
    # A second connection sees the delete only once it was committed.
    assert _fact_ids(db) == {topic_clustering._top_topics_fact_id(ACTIVE)}


def test_the_prune_with_nothing_stale_opens_no_transaction(db) -> None:
    conn = sqlite3.connect(db)
    try:
        assert _prune_stale_top_topics_facts(conn, [ACTIVE, STALE]) == 0
        assert not conn.in_transaction
    finally:
        conn.close()


class _WatchedGate:
    """The real gate, plus an event set when another thread starts waiting for it once armed."""

    def __init__(self, lock) -> None:
        self._lock = lock
        self.armed = False
        self.owner = None
        self.waiting = threading.Event()

    def _is_owned(self) -> bool:
        return self._lock._is_owned()

    def acquire(self, *args, **kwargs):
        if self.armed and threading.get_ident() != self.owner:
            self.waiting.set()
        return self._lock.acquire(*args, **kwargs)

    def release(self) -> None:
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def test_a_refresh_waiting_for_the_gate_holds_no_sqlite_lock(db, monkeypatch) -> None:
    gate = _WatchedGate(write_gate._WRITE_LOCK)
    monkeypatch.setattr(write_gate, "_WRITE_LOCK", gate)
    ready, go = threading.Event(), threading.Event()
    outcome: dict = {}

    def job() -> None:
        # The job thread's own connection, as get_db_connection() gives each thread in the node.
        conn = sqlite3.connect(db, timeout=30.0)
        try:
            bundle = AdapterFactory.create("local_database", conn=conn)
            ready.set()
            go.wait(10)
            outcome["written"] = write_top_topics_signal_facts(bundle, conn)
        except Exception as exc:  # noqa: BLE001 — reported by the assertion below
            outcome["error"] = exc
        finally:
            conn.close()

    worker = threading.Thread(target=job, daemon=True)
    worker.start()
    assert ready.wait(10)
    other = sqlite3.connect(db)
    other.execute("PRAGMA busy_timeout=250")
    try:
        with write_gate.with_db_write():  # stands in for the job claim that held the gate
            gate.owner, gate.armed = threading.get_ident(), True
            go.set()
            assert gate.waiting.wait(10), "the refresh never reached the gate"
            try:
                # The claim's BEGIN IMMEDIATE: "database is locked" if the waiting thread took SQLite's lock ungated.
                other.execute("BEGIN IMMEDIATE")
                other.execute("ROLLBACK")
                locked = None
            except sqlite3.OperationalError as exc:
                locked = exc
    finally:
        other.close()
        worker.join(30)
    assert locked is None, f"the refresh held SQLite's write lock while it waited for the gate: {locked}"
    assert not worker.is_alive() and "error" not in outcome, outcome
    assert outcome["written"] == 1
    assert _fact_ids(db) == {topic_clustering._top_topics_fact_id(ACTIVE)}
