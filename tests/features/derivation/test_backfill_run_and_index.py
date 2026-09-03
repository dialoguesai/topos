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
