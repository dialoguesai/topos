"""An owner revision cites the records of the fact it revises, and a revised event keeps its date.

revise_fact read the fact's source refs from its payload, but DerivationWriter keeps them only in
the source_refs_json column, so every revision was written citing nothing. It also never passed
the fact's occurrence, so a revised event's key fell back to ':undated' and its period_start to
NULL, and an undated event matches a retelling of any date.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.derivation.packs import load_packs
from topos.features.derivation.registry import bundled_pack_dir
from topos.features.derivation.surfaces import fact_evidence, revise_fact
from topos.features.derivation.writer import DerivationWriter
from topos.storage.db.migrations import apply_all_migrations

PACKS = load_packs(bundled_pack_dir(), only=["relationships.social", "aspirations.goals"])
E1 = {"table": "journal_entries", "record_id": "e1"}
E2 = {"table": "journal_entries", "record_id": "e2"}
GOAL = {"goal": "learn spanish fluently", "domain": "learning", "horizon": "this_year", "status": "active"}


@pytest.fixture()
def conn(tmp_path):
    connection = sqlite3.connect(str(tmp_path / "revise.db"))
    apply_all_migrations(connection)
    connection.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json,"
                       " is_self) VALUES ('ent_owner', 'person', 'Owner', 'owner', '[]', 1)")
    connection.commit()
    yield connection
    connection.close()


def _write(conn, pack_id, predicate, value, refs=(E1,), occurrence=None, event_date=None):
    out = DerivationWriter(conn, model="synthetic-local-model").assert_pack_fact(
        pack=PACKS[pack_id], predicate=predicate, subject_entity_id="ent_owner", value=value,
        actor_role="authored", source_refs=list(refs), confidence=0.9, quote="", about="owner",
        occurrence=occurrence, event_date=event_date)
    assert out["outcome"] == "written", out
    return out["object_id"]


def _relationship(conn, person, role, refs=(E1,), occurrence=None, event_date=None):
    return _write(conn, "relationships.social", "rel.relationship",
                  {"person": person, "role": role, "status": "active"}, refs, occurrence, event_date)


def _milestone(milestone, kind):
    return {"goal_ref": "learn spanish fluently", "milestone": milestone, "kind": kind}


def _row(conn, object_id):
    names = ("object_key", "source_refs_json", "period_start", "valid_from", "payload_json")
    return dict(zip(names, conn.execute(f"SELECT {', '.join(names)} FROM signal_objects WHERE object_id=?",
                                        (object_id,)).fetchone()))


def _refs(conn, object_id):
    return json.loads(_row(conn, object_id)["source_refs_json"])


CLOSE_FRIEND = {"person": "Quillon", "role": "close_friend", "status": "active"}


def test_a_revision_cites_the_records_the_fact_was_derived_from(conn):
    fact = _relationship(conn, "Quillon", "friend", refs=(E1, E2))

    revised = revise_fact(conn, fact, value=CLOSE_FRIEND)

    assert revised["outcome"] == "written"
    assert _refs(conn, revised["object_id"]) == [E1, E2]  # was []
    assert _refs(conn, fact) == [E1, E2]


def test_the_owner_sees_the_records_behind_a_revised_fact(conn):
    conn.execute("INSERT INTO journal_entries (entry_id, entry_at, content, source_id)"
                 " VALUES ('e1', '2026-08-30T09:00:00', 'Went climbing with Quillon again.', 'journal')")
    fact = _relationship(conn, "Quillon", "friend")

    revised = revise_fact(conn, fact, value=CLOSE_FRIEND)

    sources = fact_evidence(conn, revised["object_id"])["sources"]
    assert [(s["table"], s["record_id"], s["text"]) for s in sources] == [
        ("journal_entries", "e1", "Went climbing with Quillon again.")]


def test_a_revised_fact_names_its_records_to_the_permissions_floors(conn):
    from topos.permissions_v2.message_evidence import facts_naming

    fact = _relationship(conn, "Quillon", "friend")

    revised = revise_fact(conn, fact, value=CLOSE_FRIEND)

    # The closed fact, then its current successor: an exclusion or opt-out of the revised fact
    # reaches the entry through the revised fact itself.
    assert [row[0] for row in facts_naming(conn, {"e1": {"journal_entries"}})] == [fact, revised["object_id"]]


def test_the_owner_can_review_a_revised_fact(conn, tmp_path):
    from topos.permissions_v2.evidence import EvidenceBinding, EvidenceResolver
    from topos.permissions_v2.protection_clock import ensure_protection_clock
    from topos.principal import OWNER_APP, Principal, reset_principal, set_principal

    conn.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO engine_config VALUES ('user_id', 'owner-1')")
    conn.execute("CREATE TABLE conversation_messages (message_id TEXT, dataset_id TEXT, source_id TEXT, content TEXT,"
                 " is_from_self INTEGER, deleted_at TEXT, owner_user_id TEXT)")
    conn.execute("INSERT INTO conversation_messages VALUES"
                 " ('message-1', 'dataset-1', 'source-1', 'Quillon helped me move.', 1, NULL, 'owner-1')")
    message = {"table": "conversation_messages", "dataset_id": "dataset-1", "source_id": "source-1",
               "record_id": "message-1"}
    fact = _relationship(conn, "Quillon", "friend", refs=(message,))
    revised = revise_fact(conn, fact, value=CLOSE_FRIEND)
    conn.commit()
    ensure_protection_clock(tmp_path / "revise.db", owner_id="owner-1")
    resolver = EvidenceResolver(tmp_path / "revise.db", binding=EvidenceBinding(
        environment_id="revise-test", node_id="node-1", resource_id="resource-1", owner_id="owner-1"))

    token = set_principal(Principal(cls=OWNER_APP, channel="uds", acting_user="owner-1"))
    try:
        snapshot = resolver.inspect_for_review(revised["object_id"])  # was refused: lineage_missing
    finally:
        reset_principal(token)

    assert [leaf.identity.record_id for leaf in snapshot.leaves] == ["message-1"]


def test_a_revision_onto_a_fact_read_from_the_same_record_is_a_correction(conn):
    # One entry, two facts; the owner says the first one meant the second person.
    fact = _relationship(conn, "Quill", "friend")
    other = _relationship(conn, "Quillon", "colleague", event_date="2026-06-01")

    revised = revise_fact(conn, fact, value={"person": "Quillon", "role": "friend", "status": "active"})

    assert revised["outcome"] == "corrected"  # was superseded: a revision citing nothing overlaps nothing
    assert json.loads(_row(conn, other)["payload_json"])["closed_reason"] == "correction"
    assert _row(conn, revised["object_id"])["valid_from"] == "2026-06-01"  # the corrected fact's belief clock


def test_a_revised_event_keeps_the_date_it_happened(conn):
    _write(conn, "aspirations.goals", "asp.goal", GOAL)
    fact = _write(conn, "aspirations.goals", "asp.milestone", _milestone("finished the first course", "progress"),
                  occurrence="2026-09-01")

    once = revise_fact(conn, fact, value=_milestone("finished the first course", "completion"))["object_id"]
    twice = revise_fact(conn, once, value=_milestone("finished the beginner course", "completion"))["object_id"]

    for revised in (once, twice):
        row = _row(conn, revised)
        assert row["object_key"].endswith(":2026-09-01"), row["object_key"]  # was ':undated'
        assert (row["period_start"], row["valid_from"]) == ("2026-09-01", "2026-09-01")


def test_a_changed_evidence_date_does_not_re_date_an_event(conn):
    # The owner's call (1 Oct 2026). The facts page prefills the date from valid_from, which facts
    # written before 26 Aug 2026 carry at extraction time; the date still closes the old row.
    _write(conn, "aspirations.goals", "asp.goal", GOAL)
    fact = _write(conn, "aspirations.goals", "asp.milestone", _milestone("finished the first course", "progress"),
                  occurrence="2026-09-01")

    revised = revise_fact(conn, fact, value=_milestone("finished the first course", "completion"),
                          evidence_date="2026-09-05")

    row = _row(conn, revised["object_id"])
    assert row["object_key"].endswith(":2026-09-01"), row["object_key"]
    assert (row["period_start"], row["valid_from"]) == ("2026-09-01", "2026-09-01")
    assert conn.execute("SELECT valid_to FROM signal_objects WHERE object_id=?", (fact,)).fetchone()[0] == "2026-09-05"


def test_a_revised_event_is_not_merged_into_the_same_event_a_year_earlier(conn):
    _write(conn, "aspirations.goals", "asp.goal", GOAL)
    earlier = _write(conn, "aspirations.goals", "asp.milestone", _milestone("passed the monthly exam", "progress"),
                     refs=(E2,), occurrence="2025-06-01")
    fact = _write(conn, "aspirations.goals", "asp.milestone", _milestone("passed the monthly exam", "setback"),
                  occurrence="2026-09-01")

    revised = revise_fact(conn, fact, value=_milestone("passed the monthly exam", "progress"))

    assert revised["outcome"] == "written"  # was retelling_merged into the 2025 exam
    current = conn.execute("SELECT period_start FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL"
                           " AND object_key LIKE 'fact:ent_owner:asp.milestone:%' ORDER BY period_start").fetchall()
    assert [row[0] for row in current] == ["2025-06-01", "2026-09-01"]
    assert _refs(conn, earlier) == [E2]  # the merge had replaced them with the revision's []


def test_an_undated_event_stays_undated_and_takes_the_owners_date(conn):
    # An occurrence is a date the record stated, never an evidence date (derivation_job). The
    # facts page sends the fact's valid_from as the evidence date on every save.
    _write(conn, "aspirations.goals", "asp.goal", GOAL)
    fact = _write(conn, "aspirations.goals", "asp.milestone", _milestone("finished the first course", "progress"))

    revised = revise_fact(conn, fact, value=_milestone("finished the first course", "completion"),
                          evidence_date="2026-09-05")

    row = _row(conn, revised["object_id"])
    assert row["object_key"].endswith(":undated") and row["period_start"] is None
    assert row["valid_from"] == "2026-09-05"


def test_a_revised_state_starts_on_the_owners_date_not_the_old_states(conn):
    # A state's occurrence is when that state began; the revised state begins when the owner says.
    fact = _relationship(conn, "Quillon", "friend", occurrence="2026-03-01")

    revised = revise_fact(conn, fact, value=CLOSE_FRIEND, evidence_date="2026-09-20")

    assert _row(conn, revised["object_id"])["valid_from"] == "2026-09-20"
