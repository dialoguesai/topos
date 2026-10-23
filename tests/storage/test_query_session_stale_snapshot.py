"""A half-read cursor on a worker's own connection must not cost an owner query its session row.

On 2026-10-23 a routine's owner query finished retrieval and then failed at once with
"database is locked", leaving no query_sessions or query_artifacts row, although
busy_timeout is 30s and the write gate showed no contention. Reproduced here on a temp
file DB with the node's own tuning: a connection whose read snapshot is pinned by an
unfinished statement cannot write after another gated writer commits. SQLite returns
SQLITE_BUSY_SNAPSHOT without consulting busy_timeout, and a plain retry fails the same way.
A rollback releases the snapshot only on Python 3.10, so the retry runs on a fresh connection.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from topos.storage.adapters.sqlite.stores import SQLiteQuerySessionStore
from topos.storage.db import write_gate
from topos.storage.db.connection_tuning import tune_connection
from topos.storage.db.write_gate import commit_connection, retry_stale_snapshot, with_db_write

_SCHEMA = """
CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT);
CREATE TABLE query_sessions (
    session_id TEXT PRIMARY KEY, requester_id TEXT, intent_hash TEXT,
    envelope_json TEXT NOT NULL, ttl_expires_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE query_artifacts (
    artifact_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, cache_key TEXT,
    public_result_json TEXT, retrieval_fingerprint TEXT, game_layer_strategy TEXT,
    duration_ms INTEGER, created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    tune_connection(conn)  # WAL + busy_timeout=30000, as on the node
    return conn


@pytest.fixture
def db(tmp_path: Path):
    path = tmp_path / "node.db"
    setup = _open(path)
    setup.executescript(_SCHEMA)
    setup.executemany("INSERT INTO notes(body) VALUES (?)", [(f"n{i}",) for i in range(20)])
    setup.commit()
    worker, other = _open(path), _open(path)
    yield worker, other
    for conn in (setup, worker, other):
        conn.close()


def _pin_snapshot(conn: sqlite3.Connection) -> sqlite3.Cursor:
    """A cursor left half-read: its statement keeps the connection's read snapshot open."""
    cur = conn.execute("SELECT id FROM notes")
    cur.fetchone()
    return cur


def _another_writer_commits(other: sqlite3.Connection) -> None:
    def run() -> None:
        with with_db_write():
            other.execute("INSERT INTO notes(body) VALUES ('x')")
            commit_connection(other)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()


def _other_count(db, table: str) -> int:
    return db[1].execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _session(session_id: str) -> dict:
    return {"session_id": session_id, "requester_id": "r", "intent_hash": "h", "envelope_json": {}}


def test_a_pinned_snapshot_fails_a_write_at_once_and_a_plain_retry_too(db):
    """The mechanism. If this stops raising, SQLite changed and the retry can be revisited."""
    worker, other = db
    cur = _pin_snapshot(worker)
    _another_writer_commits(other)
    for _attempt in range(2):
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            with with_db_write():
                worker.execute("INSERT INTO notes(body) VALUES ('y')")
        assert time.monotonic() - started < 1.0, "busy_timeout was never consulted"
    worker.rollback()
    del cur


def test_without_another_commit_the_pinned_snapshot_writes_fine(db):
    """So the failure needs a stale snapshot, not just an open cursor."""
    worker, _other = db
    cur = _pin_snapshot(worker)
    SQLiteQuerySessionStore(worker).put(_session("s-fresh"))
    assert _other_count(db, "query_sessions") == 1
    del cur


def test_the_incident_sequence_keeps_its_session_and_artifact(db, caplog):
    """purge_expired, put, append_artifact on one stale connection, as a query turn runs them."""
    worker, other = db
    store = SQLiteQuerySessionStore(worker)
    cur = _pin_snapshot(worker)
    _another_writer_commits(other)
    with caplog.at_level(logging.WARNING, logger=write_gate.logger.name):
        store.purge_expired()
        store.put(_session("s-1"))
        store.append_artifact("s-1", {"cache_key": "k", "public_result_json": None, "duration_ms": 5})
    # Read through another connection: from Python 3.11 the worker's own reads stay on
    # the pinned snapshot until the half-read cursor is freed.
    assert other.execute("SELECT session_id FROM query_sessions").fetchall() == [("s-1",)]
    assert other.execute("SELECT session_id FROM query_artifacts").fetchall() == [("s-1",)]
    assert not worker.in_transaction
    retried = [r for r in caplog.records if "stale read snapshot" in r.getMessage()]
    # One on 3.10, where the rollback also resets the half-read cursor; one per write
    # from 3.11, where that cursor keeps the worker's snapshot pinned until it is freed.
    assert retried and all("stores.py" in r.getMessage() for r in retried)
    del cur


def test_put_and_invalidate_each_survive_a_stale_snapshot(db):
    worker, other = db
    store = SQLiteQuerySessionStore(worker)
    cur = _pin_snapshot(worker)
    _another_writer_commits(other)
    store.put(_session("s-2"))
    store.append_artifact("s-2", {"cache_key": "k"})
    assert _other_count(db, "query_artifacts") == 1
    cur = _pin_snapshot(worker)
    _another_writer_commits(other)
    assert store.invalidate("s-2") == 1
    del cur


def test_an_in_memory_database_has_nothing_to_reopen():
    conn = sqlite3.connect(":memory:")
    calls = []

    def fn(_conn):
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        retry_stale_snapshot(conn, fn)
    assert calls == [1]


def test_no_rollback_inside_a_callers_transaction(db):
    """A caller's open batch is never discarded to make room for a retry."""
    worker, _other = db
    worker.execute("INSERT INTO notes(body) VALUES ('batch')")
    assert worker.in_transaction
    calls = []

    def fn(_conn):
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        retry_stale_snapshot(worker, fn)
    assert calls == [1]
    assert worker.in_transaction
    assert worker.execute("SELECT COUNT(*) FROM notes WHERE body='batch'").fetchone()[0] == 1
    worker.rollback()


def test_a_busy_error_that_waited_is_contention_and_is_not_retried(db, monkeypatch):
    worker, _other = db
    monkeypatch.setattr(write_gate, "_STALE_SNAPSHOT_MAX_S", 0.0)
    calls = []

    def fn(_conn):
        calls.append(1)
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        retry_stale_snapshot(worker, fn)
    assert calls == [1]


def test_other_errors_are_not_retried(db):
    worker, _other = db
    calls = []

    def fn(_conn):
        calls.append(1)
        raise sqlite3.OperationalError("no such table: nope")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        retry_stale_snapshot(worker, fn)
    assert calls == [1]
