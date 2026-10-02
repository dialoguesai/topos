"""A promotion the writer does not carry out leaves its review item pending and writes nothing.

promote_conflict (the review queue's 'Edit & add') asked DerivationWriter for the fact and then,
whatever the outcome, marked the item accepted, recorded an owner decision about a third-party
subject, and ledgered the promotion as accepted. The writer can refuse without raising
(guard_reject, quarantined, conflict_queued, schema_reject), so each refusal took the item out of
the queue with no fact written. A person the promotion created for its subject stayed behind too.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

import topos.core.handlers as hub
from topos.core.handlers.derivation import handle_promote_fact_conflict
from topos.features.derivation.packs import load_packs
from topos.features.derivation.registry import bundled_pack_dir
from topos.features.derivation.surfaces import promote_conflict
from topos.features.derivation.writer import DerivationWriter
from topos.storage.db.migrations import apply_all_migrations

PACKS = load_packs(bundled_pack_dir(), only=["relationships.social", "aspirations.goals"])
E1 = {"table": "journal_entries", "record_id": "e1"}
FRIEND = {"person": "Quillon", "role": "friend", "status": "active"}


@pytest.fixture()
def migrated(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "promote.db"))
    apply_all_migrations(conn)
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, is_self)"
                 " VALUES ('ent_owner', 'person', 'Owner', 'owner', '[]', 1)")
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, is_self)"
                 " VALUES ('ent_zorbo', 'person', 'Zorbo Unknownperson', 'zorbo unknownperson', '[]', 0)")
    conn.commit()
    yield conn
    conn.close()


def _assert(conn, pack_id, predicate, value, about):
    return DerivationWriter(conn, model="synthetic-local-model").assert_pack_fact(
        pack=PACKS[pack_id], predicate=predicate, subject_entity_id="ent_owner", value=value,
        actor_role="authored", source_refs=[E1], confidence=0.9, quote="", about=about)


def _write(conn, pack_id, predicate, value):
    out = _assert(conn, pack_id, predicate, value, "owner")
    assert out["outcome"] == "written", out
    conn.commit()
    return out["object_id"]


def _queue(conn, pack_id, predicate, value):
    """Quarantine an extraction as the derivation job does when two passes disagree on its subject."""
    assert _assert(conn, pack_id, predicate, value, "unclear")["outcome"] == "quarantined"
    conn.commit()
    (cid,), = conn.execute("SELECT conflict_id FROM fact_conflicts WHERE status='pending'").fetchall()
    return cid


def _guard_reject(conn):
    # The owner's corrected value carries an identifier, which the writer's guard refuses.
    cid = _queue(conn, "relationships.social", "rel.relationship", FRIEND)
    return cid, {"value": {**FRIEND, "status": "write to someone@example.invalid"}}


def _quarantined(conn):
    # A milestone attaches only to a stored goal, and none is stored.
    return _queue(conn, "aspirations.goals", "asp.milestone",
                  {"goal_ref": "run a marathon", "milestone": "finished the first course", "kind": "progress"}), {}


def _conflict_queued(conn):
    # relationships.social: partner and ex_partner exclude each other for one person within 7 days.
    _write(conn, "relationships.social", "rel.relationship", {**FRIEND, "role": "partner"})
    return _queue(conn, "relationships.social", "rel.relationship", {**FRIEND, "role": "ex_partner"}), {}


def _schema_reject(conn):
    # Queued under a predicate its pack has since stopped declaring.
    cid = _queue(conn, "relationships.social", "rel.relationship", FRIEND)
    conn.execute("UPDATE fact_conflicts SET predicate='rel.retired_name' WHERE conflict_id=?", (cid,))
    conn.commit()
    return cid, {}


REFUSALS = {"guard_reject": _guard_reject, "quarantined": _quarantined,
            "conflict_queued": _conflict_queued, "schema_reject": _schema_reject}
# In the writer's reason for each refusal. It gives none for conflict_queued.
REASONS = {"guard_reject": "identifier", "quarantined": "no stored goal",
           "schema_reject": "unknown predicate rel.retired_name"}
SUBJECTS = {"owner": {"to_owner": True}, "existing_person": {"subject_entity_id": "ent_zorbo"},
            "new_person": {"new_person_name": "Nora Vasquez"}}


def _assert_untouched(conn, before):
    assert not conn.in_transaction  # nothing left pending for the next commit
    conn.commit()  # what the next writer's commit on this connection persists
    # The item is still pending, and no fact, queue row, ledger row, subject decision or person was added.
    assert list(conn.iterdump()) == before


@pytest.mark.parametrize("outcome", REFUSALS)
def test_a_refused_promotion_leaves_the_item_pending(migrated, outcome):
    conn = migrated
    cid, kwargs = REFUSALS[outcome](conn)
    before = list(conn.iterdump())

    out = promote_conflict(conn, cid, to_owner=True, **kwargs)

    reason = out.pop("reason")
    assert out == {"conflict_id": cid, "outcome": outcome, "object_id": None, "subject_entity_id": "ent_owner"}
    if outcome in REASONS:
        assert REASONS[outcome] in reason
    else:
        assert reason is None
    _assert_untouched(conn, before)


@pytest.mark.parametrize("subject", ["existing_person", "new_person"])
def test_a_refused_promotion_records_no_decision_and_keeps_no_new_person(migrated, subject):
    conn = migrated
    cid, kwargs = _guard_reject(conn)
    before = list(conn.iterdump())

    out = promote_conflict(conn, cid, **SUBJECTS[subject], **kwargs)

    assert (out["outcome"], out["object_id"]) == ("guard_reject", None)
    # The new person was rolled back with the rest, so the answer names no subject.
    assert out["subject_entity_id"] == ("ent_zorbo" if subject == "existing_person" else None)
    _assert_untouched(conn, before)


def test_the_engine_handler_answers_a_refusal_with_its_outcome(migrated, monkeypatch):
    """The web client reaches promote_conflict here, through the control plane's signal proxy."""
    conn = migrated
    cid, kwargs = _guard_reject(conn)
    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)

    answer = asyncio.run(handle_promote_fact_conflict(
        {"id": "1", "payload": {"conflict_id": cid, "to_owner": True, **kwargs}}))

    assert answer["status"] == "ok"
    assert (answer["payload"]["outcome"], answer["payload"]["object_id"]) == ("guard_reject", None)
    assert "identifier" in answer["payload"]["reason"]
    assert conn.execute("SELECT status FROM fact_conflicts WHERE conflict_id=?", (cid,)).fetchone() == ("pending",)


def test_a_promotion_whose_write_raises_keeps_no_new_person(migrated):
    conn = migrated
    cid = _queue(conn, "relationships.social", "rel.relationship", FRIEND)
    conn.execute("CREATE TEMP TRIGGER refuse_fact BEFORE INSERT ON signal_objects"
                 " BEGIN SELECT RAISE(ABORT, 'fact refused'); END")
    before = list(conn.iterdump())

    with pytest.raises(sqlite3.IntegrityError, match="fact refused"):
        promote_conflict(conn, cid, **SUBJECTS["new_person"])

    _assert_untouched(conn, before)


def _current(conn):
    return sorted(row[0] for row in conn.execute(
        "SELECT object_id FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL"))


def _promoted(conn, cid, object_id):
    assert not conn.in_transaction
    assert conn.execute("SELECT status FROM fact_conflicts WHERE conflict_id=?", (cid,)).fetchone() == ("accepted",)
    assert conn.execute("SELECT vstatus, written_object_id FROM derivation_training_ledger"
                        " WHERE stage='owner_promote'").fetchall() == [("accepted", object_id)]


@pytest.mark.parametrize("subject", SUBJECTS)
def test_a_promotion_commits_the_fact_with_the_resolved_item(migrated, subject):
    conn = migrated
    cid = _queue(conn, "relationships.social", "rel.relationship", FRIEND)

    out = promote_conflict(conn, cid, **SUBJECTS[subject])

    assert out["outcome"] == "written"
    assert _current(conn) == [out["object_id"]]
    _promoted(conn, cid, out["object_id"])
    key, = conn.execute("SELECT object_key FROM signal_objects WHERE object_id=?", (out["object_id"],)).fetchone()
    assert key.startswith(f"fact:{out['subject_entity_id']}:")
    decided = conn.execute("SELECT subject_entity_id, policy FROM net_subject_policy").fetchall()
    if subject == "owner":
        assert decided == []
    else:
        assert decided == [(out["subject_entity_id"], "allow")]
    if subject == "new_person":
        assert conn.execute("SELECT canonical_name, is_self FROM entities WHERE entity_id=?",
                            (out["subject_entity_id"],)).fetchone() == ("Nora Vasquez", 0)


def test_a_promotion_of_a_value_already_current_resolves_the_item(migrated):
    """The writer corroborates the current fact that already carries the value; nothing new is
    written, and the item is resolved onto that fact, as before."""
    conn = migrated
    fact = _write(conn, "relationships.social", "rel.relationship", FRIEND)
    cid = _queue(conn, "relationships.social", "rel.relationship", FRIEND)

    out = promote_conflict(conn, cid, to_owner=True)

    assert out == {"conflict_id": cid, "outcome": "corroborated", "object_id": fact,
                   "subject_entity_id": "ent_owner"}
    assert _current(conn) == [fact]
    _promoted(conn, cid, fact)
