"""A correction or supersession closes the incumbent and writes its successor in ONE transaction.

DerivationWriter._close committed the close before _insert built the successor, so anything
that raised in _insert left the fact closed (closed_reason superseded or correction) with no
successor, durably: a refused INSERT, `database is locked` when another writer took the lock
between the two commits, or an error while building the successor's payload.
"""
from __future__ import annotations

import sqlite3

import pytest

from topos.features.derivation import writer as writer_module
from topos.features.derivation.packs import load_packs
from topos.features.derivation.registry import bundled_pack_dir
from topos.features.derivation.writer import DerivationWriter
from topos.storage.db.migrations import apply_all_migrations

E1 = {"table": "journal_entries", "record_id": "e1"}
E2 = {"table": "journal_entries", "record_id": "e2"}
# The same evidence read again is a correction; new evidence is a supersession.
REVISIONS = {"correction": [E1, E2], "supersession": [E2]}


@pytest.fixture()
def migrated(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "derivation.db"))
    apply_all_migrations(conn)
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, is_self)"
                 " VALUES ('ent_owner', 'person', 'Owner', 'owner', '[]', 1)")
    conn.commit()
    yield conn
    conn.close()


def _assert(conn, role, refs):
    pack = load_packs(bundled_pack_dir(), only=["relationships.social"])["relationships.social"]
    return DerivationWriter(conn, model="synthetic-local-model").assert_pack_fact(
        pack=pack, predicate="rel.relationship", subject_entity_id="ent_owner",
        value={"person": "Quillon", "role": role, "status": "active"}, actor_role="authored",
        source_refs=refs, confidence=0.9, quote="", about="owner")


def _current(conn):
    return [row[0] for row in conn.execute(
        "SELECT object_id FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL")]


@pytest.mark.parametrize("refs", REVISIONS.values(), ids=REVISIONS)
def test_a_revision_commits_the_close_and_its_successor_together(migrated, refs):
    conn = migrated
    incumbent = _assert(conn, "friend", [E1])["object_id"]

    successor = _assert(conn, "close_friend", refs)["object_id"]

    assert not conn.in_transaction
    assert _current(conn) == [successor]
    assert conn.execute("SELECT valid_to IS NOT NULL FROM signal_objects WHERE object_id=?",
                        (incumbent,)).fetchone() == (1,)


@pytest.mark.parametrize("refs", REVISIONS.values(), ids=REVISIONS)
def test_a_refused_successor_leaves_the_incumbent_current(migrated, refs):
    conn = migrated
    incumbent = _assert(conn, "friend", [E1])["object_id"]
    conn.execute("CREATE TEMP TRIGGER refuse_successor BEFORE INSERT ON signal_objects"
                 " BEGIN SELECT RAISE(ABORT, 'successor refused'); END")
    before = list(conn.iterdump())

    with pytest.raises(sqlite3.IntegrityError, match="successor refused"):
        _assert(conn, "close_friend", refs)

    assert not conn.in_transaction  # nothing left pending for the next commit
    conn.commit()  # what the next writer's commit on this connection persists
    assert list(conn.iterdump()) == before
    assert _current(conn) == [incumbent]


def test_a_successor_that_fails_to_build_leaves_the_incumbent_current(migrated, monkeypatch):
    conn = migrated
    incumbent = _assert(conn, "friend", [E1])["object_id"]
    before = list(conn.iterdump())

    def refuse(value):
        raise RuntimeError("payload refused")

    monkeypatch.setattr(writer_module, "_display_value", refuse)
    with pytest.raises(RuntimeError, match="payload refused"):
        _assert(conn, "close_friend", [E2])

    assert not conn.in_transaction
    assert list(conn.iterdump()) == before
    assert _current(conn) == [incumbent]
