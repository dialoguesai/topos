"""NR183 R10: the home chat's schema step takes the write gate only when something is missing.

Every home-chat store call (the session list and one session's read included) ran ``ensure_home_chat_schema``, and
that took the process-wide write gate to run ``CREATE ... IF NOT EXISTS`` statements that changed nothing. On the
owner's node a share-index rebuild held the gate for 62.9 s, and every home-chat read and save waited behind it.
The step now reads ``sqlite_master`` first, with no gate (a read takes no SQLite write lock), and runs the gated DDL
only when an object or the added column is missing. It is read every time, never remembered, so a database file
made again, a dropped table or index, or an older table shape is put right at once.

Pinned here: the second call takes no gate, and a read is served while another thread holds the gate; a database
made again at the same path, a dropped table, a dropped index and an older table shape each take the gated path
and end complete; an error while checking takes the gated path. All rows are invented.
"""
from __future__ import annotations

import contextlib
import sqlite3
import threading

import pytest

from topos.home_chat import schema, store
from topos.storage.db.write_gate import with_db_write

USER, ENGINE = "user-nr183", "engine-nr183"
SESSION = "00000000-0000-4000-8000-000000000183"


@pytest.fixture
def gate_entries(monkeypatch):
    """How many times the schema step entered the write gate."""
    entered = []
    real = schema.with_db_write

    @contextlib.contextmanager
    def counting():
        entered.append(1)
        with real():
            yield
    monkeypatch.setattr(schema, "with_db_write", counting)
    return entered


def connect(path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def complete(conn) -> bool:
    names = {(row[0], row[1]) for row in conn.execute("SELECT type, name FROM sqlite_master")}
    columns = {row[1] for row in conn.execute("PRAGMA table_info(home_chat_sessions)")}
    return schema.SCHEMA_OBJECTS <= names and "participants_json" in columns


def test_the_first_call_creates_the_schema_and_the_second_takes_no_gate(tmp_path, gate_entries):
    conn = connect(tmp_path / "database.db")
    schema.ensure_home_chat_schema(conn)
    assert len(gate_entries) == 1 and complete(conn)
    schema.ensure_home_chat_schema(conn)
    assert len(gate_entries) == 1
    assert store.list_sessions(conn, user_id=USER, engine_id=ENGINE) == []
    assert store.get_session(conn, user_id=USER, session_id="no-such-session") is None
    assert len(gate_entries) == 1                       # the reads took no gate at all


def test_a_read_is_served_while_another_thread_holds_the_gate(tmp_path):
    conn = connect(tmp_path / "database.db")
    schema.ensure_home_chat_schema(conn)
    holding, release = threading.Event(), threading.Event()

    def holder():
        with with_db_write():
            holding.set()
            release.wait(10)
    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert holding.wait(5)
    served = []
    reader = threading.Thread(target=lambda: served.append(store.list_sessions(conn, user_id=USER, engine_id=ENGINE)),
                              daemon=True)
    try:
        reader.start()
        reader.join(2.0)
        assert served == [[]], "the home-chat read waited for the write gate"
    finally:
        release.set()
        thread.join(5)
        reader.join(5)


def test_a_database_made_again_at_the_same_path_is_ensured_again(tmp_path, gate_entries):
    path = tmp_path / "database.db"
    first = connect(path)
    schema.ensure_home_chat_schema(first)
    first.close()
    for leftover in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        leftover.unlink(missing_ok=True)
    again = connect(path)
    assert not complete(again)
    schema.ensure_home_chat_schema(again)
    assert len(gate_entries) == 2 and complete(again)
    assert store.list_sessions(again, user_id=USER, engine_id=ENGINE) == []


def test_two_databases_in_turn_are_each_ensured(tmp_path, gate_entries):
    one, two = connect(tmp_path / "one.db"), connect(tmp_path / "two.db")
    schema.ensure_home_chat_schema(one)
    schema.ensure_home_chat_schema(two)
    schema.ensure_home_chat_schema(one)
    assert len(gate_entries) == 2 and complete(one) and complete(two)


@pytest.mark.parametrize("dropped", sorted(schema.SCHEMA_OBJECTS))
def test_a_dropped_table_or_index_is_made_again(tmp_path, gate_entries, dropped):
    conn = connect(tmp_path / "database.db")
    schema.ensure_home_chat_schema(conn)
    kind, name = dropped
    conn.execute(f"DROP {kind.upper()} {name}")
    conn.commit()
    schema.ensure_home_chat_schema(conn)
    assert len(gate_entries) == 2 and complete(conn)


def test_an_older_table_without_the_participants_column_gets_it(tmp_path, gate_entries):
    conn = connect(tmp_path / "database.db")
    schema.ensure_home_chat_schema(conn)
    conn.execute("ALTER TABLE home_chat_sessions DROP COLUMN participants_json")
    conn.commit()
    assert not complete(conn)
    schema.ensure_home_chat_schema(conn)
    assert len(gate_entries) == 2 and complete(conn)


class CheckFails:
    """A connection whose schema read fails; everything else goes to the real one."""

    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, *args):
        if "sqlite_master" in sql:
            raise sqlite3.OperationalError("an invented failure")
        return self._conn.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_a_check_that_fails_takes_the_gated_path(tmp_path, gate_entries):
    conn = connect(tmp_path / "database.db")
    schema.ensure_home_chat_schema(conn)
    assert schema.schema_in_place(CheckFails(conn)) is False
    schema.ensure_home_chat_schema(CheckFails(conn))
    assert len(gate_entries) == 2 and complete(conn)


def test_the_store_still_saves_and_reads_a_session(tmp_path):
    conn = connect(tmp_path / "database.db")
    saved = store.upsert_session(conn, user_id=USER, payload={
        "sessionId": SESSION, "engineId": ENGINE, "title": "An invented chat", "history": store.empty_history()})
    assert saved["sessionId"] == SESSION
    assert [row["sessionId"] for row in store.list_sessions(conn, user_id=USER, engine_id=ENGINE)] == [SESSION]
    assert store.get_session(conn, user_id=USER, session_id=SESSION)["title"] == "An invented chat"


def test_the_set_the_check_reads_is_exactly_what_the_ddl_makes(tmp_path):
    """WS0 review: SCHEMA_OBJECTS is read by the check AND by this file's oracle, so a table or index added to the DDL
    but not to the set would be skipped on every existing database while no test noticed. Pin the set to the DDL."""
    conn = connect(tmp_path / "database.db")
    schema.ensure_home_chat_schema(conn)
    made = {(str(row[0]), str(row[1])) for row in conn.execute(
        "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'index', 'view', 'trigger')"
        " AND name NOT LIKE 'sqlite_%'")}
    assert made == set(schema.SCHEMA_OBJECTS)
