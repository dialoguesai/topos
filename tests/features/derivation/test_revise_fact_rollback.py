"""An owner revision the writer does not carry out leaves the fact it revises current and unchanged.

revise_fact closed the live fact and committed, and only then asked DerivationWriter to write the
revised value. The writer can refuse without raising (guard_reject, conflict_queued, quarantined,
schema_reject), and it can raise. Either way the fact stayed closed (updated_by owner_revision) with
no successor. Its own pre-checks covered only a refused role and the blackhole.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.derivation.packs import load_packs
from topos.features.derivation.registry import bundled_pack_dir
from topos.features.derivation.surfaces import revise_fact
from topos.features.derivation.writer import DerivationWriter
from topos.storage.db.migrations import apply_all_migrations

PACKS = load_packs(bundled_pack_dir(), only=["relationships.social", "aspirations.goals"])
E1 = {"table": "journal_entries", "record_id": "e1"}
E2 = {"table": "journal_entries", "record_id": "e2"}


@pytest.fixture()
def migrated(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "revise.db"))
    apply_all_migrations(conn)
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, is_self)"
                 " VALUES ('ent_owner', 'person', 'Owner', 'owner', '[]', 1)")
    conn.commit()
    yield conn
    conn.close()


def _write(conn, pack_id, predicate, value, refs=(E1,), occurrence=None):
    out = DerivationWriter(conn, model="synthetic-local-model").assert_pack_fact(
        pack=PACKS[pack_id], predicate=predicate, subject_entity_id="ent_owner", value=value,
        actor_role="authored", source_refs=list(refs), confidence=0.9, quote="", about="owner",
        occurrence=occurrence)
    assert out["outcome"] == "written", out
    return out["object_id"]


def _relationship(conn, person, role, refs=(E1,)):
    return _write(conn, "relationships.social", "rel.relationship",
                  {"person": person, "role": role, "status": "active"}, refs)


def _current(conn):
    return sorted(row[0] for row in conn.execute(
        "SELECT object_id FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL"))


def _guard_reject(conn):
    fact = _relationship(conn, "Quillon", "friend")
    return fact, {"person": "Quillon", "role": "friend", "status": "write to someone@example.invalid"}


def _conflict_queued(conn):
    # relationships.social: partner and ex_partner exclude each other for one person within 7 days.
    fact = _relationship(conn, "Quill", "friend")
    _relationship(conn, "Quillon", "partner", refs=(E2,))
    return fact, {"person": "Quillon", "role": "ex_partner", "status": "active"}


def _quarantined(conn):
    _write(conn, "aspirations.goals", "asp.goal",
           {"goal": "learn spanish fluently", "domain": "learning", "horizon": "this_year", "status": "active"})
    milestone = {"goal_ref": "learn spanish fluently", "milestone": "finished the first course", "kind": "progress"}
    fact = _write(conn, "aspirations.goals", "asp.milestone", milestone, occurrence="2026-09-01")
    return fact, {**milestone, "goal_ref": "run a marathon"}


def _schema_reject(conn):
    # A fact written under a predicate its pack no longer declares.
    fact = _relationship(conn, "Quillon", "friend")
    payload = json.loads(conn.execute("SELECT payload_json FROM signal_objects WHERE object_id=?",
                                      (fact,)).fetchone()[0])
    payload["predicate"] = "rel.retired_name"
    conn.execute("UPDATE signal_objects SET payload_json=? WHERE object_id=?", (json.dumps(payload), fact))
    conn.commit()
    return fact, {"person": "Quillon", "role": "close_friend", "status": "active"}


REFUSALS = {"guard_reject": _guard_reject, "conflict_queued": _conflict_queued,
            "quarantined": _quarantined, "schema_reject": _schema_reject}


def _assert_untouched(conn, fact, before):
    assert not conn.in_transaction  # nothing left pending for the next commit
    conn.commit()  # what the next writer's commit on this connection persists
    assert fact in _current(conn)
    assert list(conn.iterdump()) == before  # no close, queue row, ledger row or subject decision


@pytest.mark.parametrize("outcome", REFUSALS)
def test_a_refused_revision_leaves_the_fact_current(migrated, outcome):
    conn = migrated
    fact, value = REFUSALS[outcome](conn)
    before = list(conn.iterdump())

    out = revise_fact(conn, fact, value=value)

    assert out["outcome"] == outcome
    _assert_untouched(conn, fact, before)
    assert out["object_id"] is None


@pytest.mark.parametrize("onto_another_fact", [False, True], ids=["own_key", "another_facts_key"])
def test_a_revision_whose_write_raises_leaves_the_fact_current(migrated, onto_another_fact):
    """On another current fact's key the writer supersedes that fact first, and its close
    commits by itself (`conn.commit()`) before the successor's INSERT runs."""
    conn = migrated
    fact = _relationship(conn, "Quill" if onto_another_fact else "Quillon", "friend")
    if onto_another_fact:
        _relationship(conn, "Quillon", "close_friend", refs=(E2,))
    conn.execute("CREATE TEMP TRIGGER refuse_successor BEFORE INSERT ON signal_objects"
                 " BEGIN SELECT RAISE(ABORT, 'successor refused'); END")
    before = list(conn.iterdump())

    with pytest.raises(sqlite3.IntegrityError, match="successor refused"):
        revise_fact(conn, fact, value={"person": "Quillon", "role": "friend" if onto_another_fact
                                       else "close_friend", "status": "active"})

    _assert_untouched(conn, fact, before)


def test_a_revision_onto_another_current_fact_is_not_carried_out(migrated):
    """Renaming 'Quill' to 'Quillon' while a 'Quillon' friend fact is current: the writer
    corroborates that fact and writes no successor for this one, so the revision rolls back.
    Before, the old fact was closed and the other one corroborated."""
    conn = migrated
    fact = _relationship(conn, "Quill", "friend")
    _relationship(conn, "Quillon", "friend", refs=(E2,))
    before = list(conn.iterdump())

    out = revise_fact(conn, fact, value={"person": "Quillon", "role": "friend", "status": "active"})

    assert out["outcome"] == "corroborated"
    _assert_untouched(conn, fact, before)
    assert out["object_id"] is None


def test_a_revision_commits_the_close_with_its_successor(migrated):
    conn = migrated
    fact = _relationship(conn, "Quillon", "friend")

    out = revise_fact(conn, fact, value={"person": "Quillon", "role": "close_friend", "status": "active"})

    assert out["outcome"] == "written"
    assert not conn.in_transaction
    assert _current(conn) == [out["object_id"]]
    # OD-59's closed_fact_release withholds for any closer other than the engine's own.
    assert conn.execute("SELECT valid_to IS NOT NULL, updated_by FROM signal_objects WHERE object_id=?",
                        (fact,)).fetchone() == (1, "owner_revision")
    assert conn.execute("SELECT COUNT(*) FROM derivation_training_ledger WHERE stage='owner_edit'"
                        " AND written_object_id=?", (out["object_id"],)).fetchone() == (1,)
