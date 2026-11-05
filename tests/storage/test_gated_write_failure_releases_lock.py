"""A gated write that raises must not leave its connection holding SQLite's write lock.

On 2026-11-04 (1.5.42, a stress run on the owner's node) one query turn's artifact
INSERT raised IntegrityError at 22:36:53.759Z: its session foreign key by elimination
(the primary key is a fresh uuid4), though the handler logged only the type. Python's legacy transaction handling
had opened a transaction for the INSERT, SQLite backed out the statement but kept the
transaction, and ``retry_stale_snapshot`` handled only OperationalError, so nothing
rolled it back. That worker thread's connection kept the WAL write lock: every other
writer took the write gate and then waited out the 30 s busy_timeout, job claims held
the gate ~156 s each, and the node dropped inbound requests for about 11.5 minutes
until something on that thread ended the transaction.

These tests use a temp file DB with the node's own tuning and a short busy_timeout on
the second connection, so a leaked lock shows up as "database is locked" in 0.2 s.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import pytest

from topos.storage.adapters.sqlite.stores import SQLiteQuerySessionStore
from topos.storage.db import write_gate
from topos.storage.db.connection_tuning import tune_connection
from topos.storage.db.write_gate import (
    commit_connection,
    retry_stale_snapshot,
    with_db_write,
)

# The node's DDL for the two tables (wiki_mvp_phase0 + the duration_ms column).
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
    created_at TEXT NOT NULL DEFAULT (datetime('now')), duration_ms INTEGER,
    FOREIGN KEY (session_id) REFERENCES query_sessions(session_id)
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
    setup.commit()
    worker, other = _open(path), _open(path)
    # On the node a connection enforces foreign keys once any schema module that
    # turns them on has run on it, so a worker thread's connection may or may not.
    worker.execute("PRAGMA foreign_keys=ON")
    other.execute("PRAGMA busy_timeout=200")
    yield worker, other
    for conn in (setup, worker, other):
        conn.close()


def _another_writer_can_commit(other: sqlite3.Connection) -> None:
    with with_db_write():
        other.execute("INSERT INTO notes(body) VALUES ('after')")
        commit_connection(other)


def test_a_failed_insert_keeps_its_transaction_and_the_write_lock(db):
    """The mechanism. If this stops blocking, SQLite or Python changed and the rollback can be revisited."""
    worker, other = db
    with pytest.raises(sqlite3.IntegrityError):
        worker.execute("INSERT INTO query_artifacts(artifact_id, session_id) VALUES ('a', 'none')")
    assert worker.in_transaction
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        _another_writer_can_commit(other)
    worker.rollback()
    _another_writer_can_commit(other)


def test_the_incident_artifact_insert_releases_the_write_lock(db, caplog):
    worker, other = db
    store = SQLiteQuerySessionStore(worker)
    with (
        caplog.at_level(logging.WARNING, logger=write_gate.logger.name),
        pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"),
    ):
        store.append_artifact("no-such-session", {"cache_key": "k", "duration_ms": 5})
    assert not worker.in_transaction
    _another_writer_can_commit(other)
    assert other.execute("SELECT COUNT(*) FROM query_artifacts").fetchone()[0] == 0
    # The log names the store's call site and the constraint, which the handler's
    # one-word "error=IntegrityError" did not.
    [line] = [r.getMessage() for r in caplog.records if "rolled back the transaction" in r.getMessage()]
    assert "stores.py" in line and "append_artifact" in line
    assert "FOREIGN KEY constraint failed" in line


def test_any_exception_after_a_write_rolls_the_write_back(db, caplog):
    """Not only sqlite errors: a write followed by a bug in the same callable."""
    worker, other = db

    def write_then_fail(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO notes(body) VALUES ('half')")
        raise ValueError("a value from a row")

    with (
        caplog.at_level(logging.WARNING, logger=write_gate.logger.name),
        pytest.raises(ValueError),
        with_db_write(),
    ):
        retry_stale_snapshot(worker, write_then_fail)
    assert not worker.in_transaction
    _another_writer_can_commit(other)
    assert other.execute("SELECT body FROM notes").fetchall() == [("after",)]
    # Only SQLite's own messages are logged; another exception's text could carry data.
    [line] = [r.getMessage() for r in caplog.records if "rolled back the transaction" in r.getMessage()]
    assert "raised ValueError;" in line and "a value from a row" not in line


def test_a_non_busy_operational_error_after_a_write_rolls_it_back(db):
    worker, other = db

    def write_then_bad_sql(conn: sqlite3.Connection) -> None:
        conn.execute("INSERT INTO notes(body) VALUES ('half')")
        conn.execute("INSERT INTO no_such_table VALUES (1)")

    with pytest.raises(sqlite3.OperationalError, match="no such table"), with_db_write():
        retry_stale_snapshot(worker, write_then_bad_sql)
    assert not worker.in_transaction
    _another_writer_can_commit(other)


def test_a_callers_own_transaction_is_left_to_the_caller(db):
    """An error inside a batch the caller opened is the caller's to commit or discard."""
    worker, _other = db
    with with_db_write():
        worker.execute("INSERT INTO notes(body) VALUES ('callers batch')")

        def fail(conn: sqlite3.Connection) -> None:
            conn.execute("INSERT INTO query_artifacts(artifact_id, session_id) VALUES ('a', 'none')")

        with pytest.raises(sqlite3.IntegrityError):
            retry_stale_snapshot(worker, fail)
        assert worker.in_transaction
        assert worker.execute("SELECT body FROM notes").fetchall() == [("callers batch",)]
        worker.rollback()


def test_a_successful_write_is_untouched(db):
    worker, other = db
    store = SQLiteQuerySessionStore(worker)
    store.put({"session_id": "s-1", "requester_id": "r", "intent_hash": "h", "envelope_json": {}})
    store.append_artifact("s-1", {"cache_key": "k"})
    assert not worker.in_transaction
    assert other.execute("SELECT session_id FROM query_artifacts").fetchall() == [("s-1",)]
