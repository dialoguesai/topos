"""A backfill is a run, and it publishes what it wrote.

protects: after the owner presses Backfill, the lens card reflects that it ran
and the facts are askable in chat — not merely visible on the Facts page.

Measured live 2026-09-03 on `values.motivation`: 73 records examined, 11 facts
written, and afterwards `pack_registry.last_run_at` was still NULL and
`signal_embeddings` held ZERO rows for the pack. Both have the same cause —
`mark_pack_run` is called at the end of an INGEST batch
(`derivation_job.py:292`) and embedding runs in the batch's embedding job, and
a backfill is neither. On a quiet node (no connectors delivering) a batch may
never happen, so the card read "never run" over a pack that had just produced
facts, and home chat could not see any of them.

The extraction loop is not exercised here: with no history rows the loop body
is skipped, which isolates the tail — exactly the code under test — without
standing up a model.
"""

import sqlite3

import pytest

from topos.features.derivation import surfaces
from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry
from topos.storage.db.migrations import apply_all_migrations

PACK = "values.motivation"


@pytest.fixture
def node_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "node.db")
    apply_all_migrations(conn)
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name,"
        " normalized_name, aliases_json, is_self)"
        " VALUES ('ent_owner','person','Owner','owner','[]',1)"
    )
    seed_pack_registry(conn, bundled_pack_dir())
    conn.execute("UPDATE pack_registry SET enabled=1 WHERE pack_id=?", (PACK,))
    conn.commit()
    return conn


def _run(conn, monkeypatch):
    """Backfill with the indexer stubbed — the assertion is that it is CALLED,
    not that embeddings compute (that needs a model and is its own lane)."""
    calls = []
    import topos.features.signal.derived_index as di

    monkeypatch.setattr(
        di, "index_derived_objects",
        lambda c, **kw: calls.append(c) or {"embedded": 7},
    )
    stats = surfaces.run_pack_backfill(conn, PACK, limit=5)
    return stats, calls


def test_backfill_marks_the_pack_as_run(node_db, monkeypatch):
    before = node_db.execute(
        "SELECT last_run_at FROM pack_registry WHERE pack_id=?", (PACK,)
    ).fetchone()[0]
    assert not before, "fixture should start with no run recorded"

    _run(node_db, monkeypatch)

    after, version = node_db.execute(
        "SELECT last_run_at, last_run_version FROM pack_registry WHERE pack_id=?",
        (PACK,),
    ).fetchone()
    assert after, "a backfill that wrote nothing still LOOKED — the card must not say 'never run'"
    assert version, "last_run_version pins which declaration produced these facts"


def test_backfill_indexes_what_it_wrote(node_db, monkeypatch):
    stats, calls = _run(node_db, monkeypatch)
    assert len(calls) == 1, "backfilled facts must be embedded or chat cannot see them"
    assert stats.get("indexed") == 7


def test_indexing_failure_never_loses_the_facts(node_db, monkeypatch):
    """The facts are committed before this point. A failed embedding is a
    degraded answer, not a lost fact, so it must not propagate."""
    import topos.features.signal.derived_index as di

    def boom(conn, **kw):
        raise RuntimeError("no embedding model")

    monkeypatch.setattr(di, "index_derived_objects", boom)
    stats = surfaces.run_pack_backfill(node_db, PACK, limit=5)
    assert stats["indexed"] == -1, "the failure is reported, not raised"
    assert node_db.execute(
        "SELECT last_run_at FROM pack_registry WHERE pack_id=?", (PACK,)
    ).fetchone()[0], "the run still happened"


# --------------------------------------------------------------------------
# The tail's lock order.
#
# protects: the embedding step is entered with nothing open on the backfill's
# connection. The run stamp used to be a bare UPDATE, outside the write gate
# and never committed, so the connection held SQLite's write lock through the
# whole embedding step and then queued for the gate. A gated writer that began
# in that window held the gate while it waited on SQLite, and the backfill
# could not commit until it got the gate. Measured on a live node on 1.5.47
# (10 Nov 2026): five stalls of 312 to 343 s on 5 of 9 batch ends.
# --------------------------------------------------------------------------


def _tail_probe(db_path, seen):
    """A stand-in for the indexer that records what it finds on entry."""

    def probe(conn, **kw):
        seen["in_transaction"] = conn.in_transaction
        other = sqlite3.connect(db_path, timeout=0.2)
        try:
            seen["stamp_another_connection_reads"] = other.execute(
                "SELECT last_run_at FROM pack_registry WHERE pack_id=?", (PACK,)
            ).fetchone()[0]
            try:
                other.execute("BEGIN IMMEDIATE")
                other.rollback()
                seen["write_lock_free"] = True
            except sqlite3.OperationalError:
                seen["write_lock_free"] = False
        finally:
            other.close()
        return {"embedded": 0}

    return probe


@pytest.mark.parametrize("stopped", [False, True], ids=["batch_ran_out", "stop_requested"])
def test_the_embedding_step_starts_with_the_run_stamp_committed(node_db, tmp_path, monkeypatch, stopped):
    import topos.features.signal.derived_index as di

    node_db.execute("PRAGMA journal_mode=WAL")  # as the node runs
    seen = {}
    monkeypatch.setattr(di, "index_derived_objects", _tail_probe(tmp_path / "node.db", seen))
    kwargs = {}
    if stopped:
        # One fed record and a stop before it: the loop ends at once, the tail still runs.
        kwargs = {"records": [{"table": "journal_entries", "record_id": "r1", "text": "placeholder",
                               "date": "2026-10-01", "role": "authored"}],
                  "should_stop": lambda: True}

    stats = surfaces.run_pack_backfill(node_db, PACK, limit=5, **kwargs)

    assert bool(stats.get("stopped")) is stopped
    assert seen, "the tail runs after a stop too"
    assert seen["in_transaction"] is False, (
        "the indexer was entered inside the backfill's open write transaction")
    assert seen["stamp_another_connection_reads"], "the run stamp was not committed before the indexer"
    assert seen["write_lock_free"], "another connection could not take SQLite's write lock"


def test_the_run_stamp_is_written_under_the_write_gate(node_db, monkeypatch):
    """Gate first, then SQLite's lock: the order every other gated writer uses."""
    from topos.features.derivation import registry
    from topos.storage.db.write_gate import db_write_lock

    real = registry.mark_pack_run
    entered = []

    def stamp(conn, pack_id, version):
        entered.append({"gate_held": bool(db_write_lock()._is_owned()),
                        "in_transaction": conn.in_transaction})
        return real(conn, pack_id, version)

    monkeypatch.setattr(registry, "mark_pack_run", stamp)
    _run(node_db, monkeypatch)

    assert entered == [{"gate_held": True, "in_transaction": False}]
    assert not db_write_lock()._is_owned(), "the gate is released before the indexer"


def test_a_stamp_that_fails_leaves_no_transaction_open(node_db, monkeypatch):
    """A failed stamp still ends the batch with its error, as before; what it may not do is
    keep SQLite's write lock on the connection the next batch reuses."""
    from topos.features.derivation import registry

    real = registry.mark_pack_run

    def stamp(conn, pack_id, version):
        real(conn, pack_id, version)
        raise sqlite3.OperationalError("stamp failed after its statement ran")

    monkeypatch.setattr(registry, "mark_pack_run", stamp)
    with pytest.raises(sqlite3.OperationalError, match="stamp failed"):
        _run(node_db, monkeypatch)

    assert node_db.in_transaction is False
    assert not node_db.execute(
        "SELECT last_run_at FROM pack_registry WHERE pack_id=?", (PACK,)
    ).fetchone()[0], "the half-written stamp was rolled back"
