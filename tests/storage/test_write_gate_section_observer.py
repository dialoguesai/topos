"""The write gate's section observer (NR166 R0): off by default, sees every way into the gate, changes none of them.

The slow-section line names a gate call site only when a section ran long, and names a shared helper
(`EvidenceResolver._read`, the ledger's `_transaction`) once for all of its callers. The observer is handed
every finished section with its site, the site's caller, its wait, its hold and whether it was nested.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager

import pytest

from topos.storage.db import write_gate
from topos.storage.db.write_gate import batched_writes, commit_connection, with_db_write

pytestmark = pytest.mark.public
HERE = "test_write_gate_section_observer.py"


@pytest.fixture
def seen():
    sections = []
    assert write_gate._section_observer is None, "another test left an observer installed"
    write_gate.set_section_observer(lambda *section: sections.append(section))
    yield sections
    write_gate.set_section_observer(None)


@contextmanager
def helper():
    """A helper that takes the gate on its callers' behalf, as `EvidenceResolver._read` does."""
    with with_db_write():
        yield


def test_no_observer_is_installed_unless_something_installs_one():
    assert write_gate._section_observer is None
    with with_db_write():   # and a section runs as it always did
        assert write_gate._WRITE_LOCK._is_owned()
    assert not write_gate._WRITE_LOCK._is_owned()


def test_a_section_is_reported_once_with_its_site_and_the_sites_caller(seen):
    def asks_the_helper():
        with helper():
            time.sleep(0.02)
    asks_the_helper()
    [(site, caller, waited, held, nested)] = seen
    assert site.startswith(HERE + ":") and site.endswith(" in helper")
    assert caller.startswith(HERE + ":") and caller.endswith(" in asks_the_helper")
    assert 0 <= waited < 0.02 <= held < 1 and nested is False


def test_a_nested_section_is_marked_and_ends_before_its_outer_one(seen):
    with with_db_write():
        with helper():
            pass
    inner, outer = seen
    assert inner[0].endswith(" in helper") and inner[4] is True
    assert outer[0].endswith(" in test_a_nested_section_is_marked_and_ends_before_its_outer_one") and outer[4] is False
    assert outer[3] >= inner[3]


def test_the_wait_is_the_time_another_thread_held_the_gate(seen):
    holding, release = threading.Event(), threading.Event()

    def holder():
        with with_db_write():
            holding.set()
            release.wait(5)
    thread = threading.Thread(target=holder)
    thread.start()
    assert holding.wait(5)
    threading.Timer(0.1, release.set).start()
    with with_db_write():
        pass
    thread.join()
    waits = {site.rsplit(" in ", 1)[1]: waited for site, _caller, waited, _held, _nested in seen}
    assert waits["test_the_wait_is_the_time_another_thread_held_the_gate"] >= 0.08
    assert waits["holder"] < 0.08


def test_commits_and_batches_are_sections_too(seen, tmp_path):
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    conn.execute("CREATE TABLE t(n INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    commit_connection(conn)
    with batched_writes(conn):
        conn.execute("INSERT INTO t VALUES (2)")
        with batched_writes(conn):   # nested on the same connection: the outer batch owns the commit
            conn.execute("INSERT INTO t VALUES (3)")
        commit_connection(conn)      # deferred inside a batch: no section of its own
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 3 and not conn.in_transaction
    name = " in test_commits_and_batches_are_sections_too"
    assert [(site.endswith(name), nested) for site, _caller, _waited, _held, nested in seen] == \
        [(True, False), (True, True), (True, False)]
    conn.close()


def test_a_batch_that_fails_still_rolls_back_and_is_still_reported(seen, tmp_path):
    conn = sqlite3.connect(tmp_path / "db.sqlite")
    conn.execute("CREATE TABLE t(n INTEGER)")
    conn.commit()
    with pytest.raises(RuntimeError):
        with batched_writes(conn):
            conn.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("the unit failed")
    assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 0
    assert len(seen) == 1 and not write_gate._WRITE_LOCK._is_owned()
    conn.close()


def test_an_observer_that_raises_breaks_no_section(tmp_path):
    def broken(*_section):
        raise RuntimeError("a diagnostic's own bug")
    write_gate.set_section_observer(broken)
    try:
        conn = sqlite3.connect(tmp_path / "db.sqlite")
        conn.execute("CREATE TABLE t(n INTEGER)")
        with with_db_write():
            conn.execute("INSERT INTO t VALUES (1)")
        commit_connection(conn)
        with batched_writes(conn):
            conn.execute("INSERT INTO t VALUES (2)")
        assert conn.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2
        conn.close()
    finally:
        write_gate.set_section_observer(None)
    assert not write_gate._WRITE_LOCK._is_owned()
