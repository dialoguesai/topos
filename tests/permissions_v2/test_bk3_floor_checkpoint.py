"""R4/W5: the rollback floor folds only the log's tail from a verified checkpoint.

  C1  after a full fold, a read folds no prefix: the cost no longer grows with history
  C2  a rewritten row at a boundary position is refused at the very next read
  C3  a rewrite behind the boundary is refused at the next full fold, within
      FULL_FOLD_SECONDS: the named residual, pinned so it cannot widen silently
  C4  a restore that lowers the sequence is refused at the next read, checkpoint or not
  C5  every consent publish folds the whole prefix
"""
from __future__ import annotations

import shutil
import sqlite3

import pytest

from tests.permissions_v2 import production_corpus as pc
from topos.permissions_v2 import canonical_floor
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.canonical_floor import CanonicalFloorStore
from topos.permissions_v2.protection_clock import EVENTS


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def node(tmp_path, monkeypatch):
    corpus = pc.build(tmp_path / "node", seed=12, positives=1, protection_events=400)
    store = CanonicalFloorStore(tmp_path / "floor.json", owner_id=pc.OWNER_ID, node_id="node-1", resource_id="resource-1")
    clock = Clock()
    store._monotonic = clock
    with read(corpus.path) as conn:
        store.install(conn)
    folds = []
    real = canonical_floor.event_chain

    def counted(conn, *, through=None, start=None):
        sequence, chain = real(conn, through=through, start=start)
        folds.append("full" if start is None else "tail")
        return sequence, chain
    monkeypatch.setattr(canonical_floor, "event_chain", counted)
    return corpus, store, clock, folds


class read:
    def __init__(self, path):
        self.conn = sqlite3.connect(path)

    def __enter__(self):
        self.conn.execute("BEGIN")
        return self.conn

    def __exit__(self, *_):
        self.conn.close()


def rewrite_event(path, sequence):
    """An in-place edit of one logged event with the append-only triggers put back exactly:
    what a copy-over restore leaves, and what the triggers alone cannot see."""
    with sqlite3.connect(path) as conn:
        triggers = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name=?", (EVENTS,)).fetchall()
        for name, _sql in triggers:
            conn.execute(f"DROP TRIGGER {name}")
        conn.execute(f"UPDATE {EVENTS} SET artifact_key=artifact_key||'x' WHERE sequence=?", (sequence,))
        for _name, sql in triggers:
            conn.execute(sql)


def test_C1_a_read_after_a_verified_fold_folds_only_the_tail(node):
    corpus, store, clock, folds = node
    with read(corpus.path) as conn:
        store.check(conn)  # first read in this process: full
    folds.clear()
    for _ in range(3):
        with read(corpus.path) as conn:
            store.check(conn)
    assert "full" not in folds


def test_C1_growth_is_adopted_and_stays_verified(node):
    corpus, store, clock, folds = node
    with read(corpus.path) as conn:
        store.check(conn)
    with sqlite3.connect(corpus.path) as conn:
        conn.execute("INSERT INTO owner_only_records(canonical_table, record_id) VALUES('conversation_messages','imessage:9')")
    folds.clear()
    for _ in range(2):
        with read(corpus.path) as conn:
            store.check(conn)
    assert "full" not in folds


@pytest.mark.parametrize("offset", canonical_floor.BOUNDARY_OFFSETS)
def test_C2_a_rewritten_boundary_row_is_refused_at_the_next_read(node, offset):
    corpus, store, clock, folds = node
    with read(corpus.path) as conn:
        store.check(conn)
        last = conn.execute(f"SELECT max(sequence) FROM {EVENTS}").fetchone()[0]
    rewrite_event(corpus.path, last - offset)
    with read(corpus.path) as conn, pytest.raises(PolicyError, match="canonical_floor_rollback"):
        store.check(conn)


def test_C3_a_rewrite_behind_the_boundary_waits_for_the_next_full_fold(node):
    corpus, store, clock, folds = node
    with read(corpus.path) as conn:
        store.check(conn)
    rewrite_event(corpus.path, 10)
    with read(corpus.path) as conn:
        store.check(conn)  # the named residual: not yet seen
    clock.now += canonical_floor.FULL_FOLD_SECONDS
    with read(corpus.path) as conn, pytest.raises(PolicyError, match="canonical_floor_rollback"):
        store.check(conn)


def test_C4_a_restore_of_an_older_file_is_refused_at_the_next_read(node, tmp_path):
    corpus, store, clock, folds = node
    backup = tmp_path / "older.db"
    shutil.copyfile(corpus.path, backup)
    with sqlite3.connect(corpus.path) as conn:
        conn.execute("INSERT INTO owner_only_records(canonical_table, record_id) VALUES('conversation_messages','imessage:9')")
    with read(corpus.path) as conn:
        store.check(conn)
    shutil.copyfile(backup, corpus.path)  # in place: same inode
    with read(corpus.path) as conn, pytest.raises(PolicyError, match="canonical_floor_rollback"):
        store.check(conn)


def test_C5_every_consent_publish_folds_the_whole_prefix(node):
    corpus, store, clock, folds = node
    with read(corpus.path) as conn:
        store.check(conn)
    folds.clear()
    with read(corpus.path) as conn:
        store.publish_pending(conn)
    assert "full" in folds
    folds.clear()
    with read(corpus.path) as conn:
        store.abort_pending(conn)
    assert "full" in folds
