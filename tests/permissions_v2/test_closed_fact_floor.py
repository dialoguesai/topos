"""OD-59: a fact closed by re-derivation stops withholding the record it cites; the owner's closure does not.

protects: `message_evidence._floors` walks the facts naming a direct message or a journal entry, and it
loaded each one through `EvidenceResolver._load`, which refuses a closed fact (`valid_to` set) as
`evidence_deleted`: a check that exists so fact qualification only ever uses current facts. So every
record any closed fact cited was withheld, whatever closed the fact. On the 1 Oct copy-based count
(runs/od59-count-20261001T000323Z), 76 of the 324 journal entries in a 90-day window stopped there: 75
behind the 26 Aug 2026 legacy retirement and one behind a writer correction. The owner's rule (OD-59),
pinned here on synthetic rows:
  - a closure an engine re-derivation closer stamped releases: DerivationWriter supersession and
    correction with a machine successor, its `closes` rule, FactStore supersession and history, the
    OD-46 lane's revision, and the named 26 Aug retirement;
  - everything else keeps withholding: `excluded_by_owner` (also once its tombstone is lifted), an owner
    revision, an owner-made successor, the source-deleted sweep, a raw closure with no marker, an
    unknown actor, the retirement tag on another day, another deletion column;
  - a closed fact that releases gets every check a current fact gets: the fact row's Off-limits
    boundary, its tombstone and owner-only, none of which ran on a closed fact, and the owner's opt-out
    and its disclosure, which the sibling floor always read;
  - a journal entry and a message behave the same, with the migration-78 keys and without;
  - fact qualification still refuses every closed fact.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from tests.permissions_v2.test_direct_message_evidence import setup as message_setup
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401 (fixture)
from tests.permissions_v2.test_journal_family import SOURCE, _entry, _off_limits, _resolver, node  # noqa: F401
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 (fixture)
from topos.permissions_v2 import message_evidence
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.message_evidence import _floors, closed_fact_release, snapshot_message
from topos.permissions_v2.permitted_derivation import LANE
from topos.storage.db.migrations import permissions_fact_lineage_keys_v1 as lk
from topos.storage.db.migrations.signal_objects_updated_by_v1 import apply_signal_objects_updated_by_v1_up

SINCE = "2026-09-01T09:00:00.000000+00:00"
CLOSE = "2026-09-20T10:00:00.000000+00:00"
JUST_AFTER = "2026-09-20T10:00:00.000001+00:00"
LATER = "2026-09-21T08:00:00.000000+00:00"
KEY = "fact:owner-entity:rel.relationship:quillon"
ELSEWHERE = [{"table": "conversation_messages", "record_id": "elsewhere-record", "source_id": "imessage"}]
MODEL = {"model": "synthetic-local-model", "pack_version": "1", "template": "t", "ts": CLOSE}


class Record:
    """One record facts can cite (a journal entry or a reviewed message) and its `_floors` verdict."""

    def __init__(self, conn, resolver, identity, cite, off_limits):
        self.conn, self.resolver, self.identity, self.cite, self.off_limits = conn, resolver, identity, cite, off_limits

    def code(self, opted_out=frozenset()):
        with self.resolver._read() as (conn, floor):
            try:
                snapshot, rows = snapshot_message(self.resolver, conn, floor, self.identity)
                _floors(self.resolver, conn, snapshot, rows, frozenset(opted_out))
            except PolicyError as exc:
                return exc.code
        return None

    def closure(self, object_id):
        """closed_fact_release over the stored row, read as `_floors` reads it."""
        with self.resolver._read() as (conn, _floor):
            cursor = conn.execute("SELECT * FROM signal_objects WHERE object_id=?", (object_id,))
            row = dict(zip([column[0] for column in cursor.description], cursor.fetchone()))
            return closed_fact_release(conn, row)


def fact(conn, object_id, *, refs, key=KEY, dimension="profile", valid_from=SINCE, valid_to=None, created_at=SINCE,
         updated_by=None, extractor_version=None, created_by=None, value=None, **payload):
    """One fact row as a writer leaves it; a column the schema lacks, or left None, takes its default."""
    body = {"subject_entity_id": "owner-entity", "predicate": "works_on", "object_value": value or f"Project {object_id}",
            "disclosure": "owner_only", "asserted_by": "owner", **payload}
    columns = {row[1] for row in conn.execute("PRAGMA table_info(signal_objects)")}
    values = {"object_id": object_id, "signal_dimension": dimension, "object_type": "fact", "object_key": key,
              "payload_json": json.dumps(body), "source_refs_json": json.dumps(refs), "valid_from": valid_from,
              "valid_to": valid_to, "created_at": created_at, "updated_at": valid_to or created_at,
              "extractor_version": extractor_version, "created_by": created_by, "updated_by": updated_by}
    values = {name: item for name, item in values.items() if item is not None and name in columns}
    conn.execute(f"INSERT INTO signal_objects ({','.join(values)}) VALUES ({','.join('?' * len(values))})",
                 list(values.values()))
    conn.commit()


def _message_off_limits(conn, name):
    from topos.storage.canonical.conversations_tables import (ensure_contact_identifiers_table, ensure_contacts_table,
                                                               ensure_conversation_participants_table,
                                                               ensure_conversations_table)
    for create in (ensure_contacts_table, ensure_contact_identifiers_table, ensure_conversations_table,
                   ensure_conversation_participants_table):
        create(conn)
    conversation = conn.execute("SELECT conversation_id FROM conversation_messages").fetchone()[0]
    conn.execute("INSERT INTO conversations(conversation_id,dataset_id,source_id) VALUES(?,'native-dataset','imessage')",
                 (conversation,))
    conn.execute("INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,rebuild_state) "
                 "VALUES('protected','',?,?,'complete')", (name, name.lower()))
    conn.commit()


@pytest.fixture(params=["journal", "journal_unkeyed", "message", "message_keyed"])
def record(request):
    """A journal entry (all migrations, the keys installed unless `_unkeyed`) or a reviewed native message
    (the p2c fixture's schema, keys only when `_keyed`), cleared by `_floors` before any fact names it."""
    if request.param.startswith("journal"):
        path = request.getfixturevalue("node")
        _entry(path, "e1")
        conn = sqlite3.connect(str(path))
        request.addfinalizer(conn.close)
        if request.param == "journal_unkeyed":
            conn.execute("DROP TRIGGER fact_lineage_keys_au")
            conn.commit()
        resolver = _resolver(path)
        found = Record(conn, resolver, resolver._identity("journal_entries", "e1", SOURCE),
                       {"table": "journal_entries", "record_id": "e1", "source_id": SOURCE},
                       lambda name: _off_limits(path, name))
    else:
        legacy = request.getfixturevalue("legacy")
        resolver, _reviews, identity = message_setup(legacy)
        conn = legacy[1]
        apply_signal_objects_updated_by_v1_up(conn)
        if request.param == "message_keyed":
            lk.apply_permissions_fact_lineage_keys_v1_up(conn)
            conn.commit()
        found = Record(conn, resolver, identity, {"table": "conversation_messages", "record_id": "imessage:1",
                                                  "source_id": "imessage", "dataset_id": "native-dataset"},
                       lambda name: _message_off_limits(conn, name))
    assert lk.installed(conn) is (request.param in ("journal", "message_keyed"))
    assert found.code() is None
    return found


# --- the closers, as each one leaves the closed row ------------------------------------------------

def _writer(reason, successor_model="synthetic-local-model", successor_at=JUST_AFTER, **closed):
    def write(conn, cite):
        fact(conn, "f-closed", refs=[cite], valid_to=CLOSE, extractor_version="derivation:t", closed_reason=reason,
             extractor=MODEL, **closed)
        if successor_model:
            fact(conn, "f-next", refs=ELSEWHERE, valid_from="2026-09-19", created_at=successor_at,
                 extractor_version="derivation:t", extractor={**MODEL, "model": successor_model})
    return write


def _writer_then_owner(conn, cite):
    """The writer's machine successor, then a later owner revision of THAT successor: not this closure's."""
    _writer("superseded")(conn, cite)
    conn.execute("UPDATE signal_objects SET valid_to='2026-09-19', updated_by='owner_revision' WHERE object_id='f-next'")
    fact(conn, "f-revised", refs=ELSEWHERE, valid_from="2026-09-19", created_at=LATER, extractor_version="derivation:t",
         extractor={**MODEL, "model": "owner-revise"})


def _writer_with_untimed(model, machine_successor=True):
    """A same-key fact whose creation time cannot be ordered against the close may be the successor."""
    def write(conn, cite):
        _writer("superseded", successor_model="synthetic-local-model" if machine_successor else None)(conn, cite)
        conn.execute("UPDATE signal_objects SET valid_to=? WHERE object_id='f-next'", (LATER,))
        fact(conn, "f-untimed", refs=ELSEWHERE, created_at="2026-09-20 10:00:01", extractor_version="derivation:t",
             extractor={**MODEL, "model": model})
    return write


def _store(successor=None):
    def write(conn, cite):
        fact(conn, "f-closed", refs=[cite], valid_to=CLOSE, extractor_version="fact_store_v1")
        if successor is not None:
            columns = {"refs": ELSEWHERE, "extractor_version": "fact_store_v1", "valid_from": CLOSE, **successor}
            fact(conn, "f-next", created_at=JUST_AFTER, **columns)
    return write


def _history(conn, cite, **extra):
    fact(conn, "f-closed", refs=[cite, *extra.pop("refs", [])], valid_from=CLOSE, valid_to=CLOSE, created_at=CLOSE,
         extractor_version="fact_store_v1", **extra)


def _od46(successor_at="2026-09-20T10:00:00Z"):
    def write(conn, cite):
        fact(conn, "f-closed", refs=[cite], key="od46:synthetic", valid_to="2026-09-20T10:00:00Z",
             extractor_version=LANE)
        fact(conn, "f-next", refs=[cite], key="od46:synthetic", valid_from="2026-09-20T09:00:00Z",
             created_at=successor_at, extractor_version=LANE)
    return write


def _raw(**columns):
    def write(conn, cite):
        fact(conn, "f-closed", refs=[cite], **{"valid_to": CLOSE, **columns})
    return write


def _another_deletion_column(conn, cite):
    conn.execute("ALTER TABLE signal_objects ADD COLUMN deleted_at TEXT")
    _raw(closed_reason='closed_by_rule:{"status": "ended"}')(conn, cite)
    conn.execute("UPDATE signal_objects SET deleted_at=? WHERE object_id='f-closed'", (CLOSE,))
    conn.commit()


# name -> (writes, the class closed_fact_release must give, or None when the fact keeps withholding)
CASES = {
    # Re-derivation: the record releases (and every other check still runs on the fact).
    "writer_supersession": (_writer("superseded"), "writer_supersession"),
    "writer_correction": (_writer("correction"), "writer_correction"),
    "writer_successor_later_revised_by_owner": (_writer_then_owner, "writer_supersession"),
    "writer_closes_rule": (_raw(closed_reason='closed_by_rule:{"status": "ended"}', extractor=MODEL),
                           "writer_closes_rule"),
    "fact_store_supersession": (_store(successor={}), "fact_store_supersession"),
    "fact_store_history": (_history, "fact_store_history"),
    "od46_revision": (_od46(), "od46_revision"),
    "legacy_retirement": (_raw(valid_to="2026-08-26 16:05:12", updated_by="retired_legacy_20260826"),
                          "legacy_retirement"),
    "legacy_retirement_iso": (_raw(valid_to="2026-08-27T00:05:12.000000+00:00", updated_by="retired_legacy_20260826"),
                              "legacy_retirement"),
    # The owner's closures, and every closure without a positive machine marker: the record withholds.
    "raw_closure_no_marker": (_raw(), None),
    "excluded_by_owner_tombstone_lifted": (_raw(valid_to="2026-09-20 10:00:00", excluded_by_owner=True), None),
    "excluded_by_owner_on_a_writer_supersession": (_writer("superseded", excluded_by_owner=True), None),
    "excluded_by_owner_on_a_closes_rule": (_raw(closed_reason='closed_by_rule:{"status": "ended"}',
                                                excluded_by_owner=True), None),
    "excluded_by_owner_on_the_legacy_retirement": (_raw(valid_to="2026-08-26 16:05:12", excluded_by_owner=True,
                                                        updated_by="retired_legacy_20260826"), None),
    "owner_revision": (_raw(valid_to="2026-09-01", updated_by="owner_revision"), None),
    "owner_promote_successor": (_writer("superseded", successor_model="owner-promote"), None),
    "owner_informant_successor": (_writer("correction", successor_model="owner-informant"), None),
    "writer_reason_without_a_successor": (_writer("superseded", successor_model=None), None),
    "writer_successor_before_the_close": (_writer("superseded", successor_at="2026-09-19T10:00:00.000000+00:00"), None),
    "writer_owner_fact_of_unknown_order": (_writer_with_untimed("owner-promote"), None),
    "writer_machine_fact_of_unknown_order_only": (_writer_with_untimed("synthetic-local-model", False), None),
    "owner_override_successor": (_store(successor={"extractor_version": "owner_override", "created_by": "owner"}), None),
    "verdict_edit_successor": (_store(successor={"corrected_from": "Project f-closed"}), None),
    "truth_seed_successor": (_store(successor={"refs": [{"table": "user_seed", "record_id": "fun-facts:works_on"}]}),
                             None),
    "history_from_a_truth_seed": (lambda conn, cite: _history(conn, cite, refs=[{"table": "user_seed",
                                                                                   "record_id": "fun-facts:x"}]), None),
    "fact_store_closure_without_a_successor": (_store(successor=None), None),
    # A machine successor made after the close, but no reason stamped and no FactStore close shape.
    "inferred_supersession": (_store(successor={"valid_from": "2026-09-19T08:00:00.000000+00:00"}), None),
    "source_deleted_sweep": (_raw(valid_to="2026-09-20 10:00:00"), None),
    "unknown_actor": (_raw(updated_by="quarantine_subject_absent"), None),
    "unknown_reason": (_writer("merged"), None),
    "reason_not_text": (_writer(1), None),
    "legacy_tag_on_another_day": (_raw(valid_to="2026-11-02 10:00:00", updated_by="retired_legacy_20260826"), None),
    "od46_without_its_successor": (_od46(successor_at="2026-09-20T11:00:00Z"), None),
    "another_deletion_column": (_another_deletion_column, None),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_closed_fact_withholds_what_it_cites_unless_re_derivation_closed_it(record, case):
    write, expected = CASES[case]
    write(record.conn, record.cite)
    assert record.closure("f-closed") == expected
    assert record.code() == (None if expected else "evidence_deleted")


# --- a released closed fact gets every check a current fact gets ---------------------------------

@pytest.mark.parametrize("check, code", [
    ("off_limits_name_in_the_fact", "entity_protected"),
    ("fact_tombstone", "intelligence_excluded"),
    ("fact_owner_only", "owner_only"),
    ("fact_record_tombstone", "owner_only"),
    ("owner_opted_out_of_the_fact", "owner_opted_out"),
    ("fact_disclosure_not_shareable", "owner_only"),
])
def test_a_released_closed_fact_still_gets_every_check_a_current_fact_gets(record, check, code):
    """The boundary, tombstone and owner-only checks never ran on a closed fact (`_load` refused it first);
    the opt-out and disclosure checks (the sibling floor) always did. Each one decides now."""
    value = "Lunch with Quillon Marsh" if check == "off_limits_name_in_the_fact" else None
    disclosure = {"disclosure": "private"} if check == "fact_disclosure_not_shareable" else {}
    fact(record.conn, "f-closed", refs=[record.cite], valid_to=CLOSE, closed_reason='closed_by_rule:{"status": "ended"}',
         value=value, **disclosure)
    assert record.closure("f-closed") == "writer_closes_rule"
    opted_out = frozenset()
    if check == "off_limits_name_in_the_fact":
        assert record.code() is None
        record.off_limits("Quillon Marsh")
    elif check == "fact_tombstone":
        record.conn.execute("INSERT INTO intelligence_exclusions(exclusion_id, artifact_type, artifact_key) "
                            "VALUES ('x-fact', 'fact', 'owner-entity:works_on')")
    elif check == "fact_owner_only":
        record.conn.execute("INSERT INTO owner_only_records(canonical_table, record_id) VALUES ('signal_objects', 'f-closed')")
    elif check == "fact_record_tombstone":
        record.conn.execute("INSERT INTO intelligence_exclusions(exclusion_id, artifact_type, artifact_key) "
                            "VALUES ('x-record', 'record', 'f-closed')")
    elif check == "owner_opted_out_of_the_fact":
        opted_out = frozenset({"f-closed"})
    record.conn.commit()
    assert record.code(opted_out) == code


def test_the_first_refusing_fact_still_names_the_reason(record):
    """A released closed fact walked first does not hide a later fact's refusal, and an owner's closure
    walked first still refuses as `evidence_deleted`, as before."""
    fact(record.conn, "f-a", refs=[record.cite], key="fact:a", valid_to=CLOSE, closed_reason='closed_by_rule:{"s": 1}')
    fact(record.conn, "f-b", refs=[record.cite], key="fact:b", valid_to=CLOSE, excluded_by_owner=True)
    assert record.code() == "evidence_deleted"
    record.conn.execute("DELETE FROM signal_objects WHERE object_id='f-b'")
    fact(record.conn, "f-c", refs=[record.cite], key="fact:c")
    record.conn.execute("INSERT INTO owner_only_records(canonical_table, record_id) VALUES ('signal_objects', 'f-c')")
    record.conn.commit()
    assert record.code() == "owner_only"


def test_fact_qualification_still_refuses_every_closed_fact(record):
    """OD-59 changes only the floor over a record's naming facts. A closed fact never qualifies, never
    witnesses an index member, and never enters the evidence graph (`_load`, `evidence._deleted`)."""
    from topos.permissions_v2.evidence import _deleted
    from topos.permissions_v2.search_index import _member_fingerprint
    _writer("superseded")(record.conn, record.cite)
    assert record.closure("f-closed") == "writer_supersession" and record.code() is None
    with record.resolver._read() as (conn, _floor):
        with pytest.raises(PolicyError, match="evidence_deleted"):
            record.resolver._load(conn, record.resolver._identity("signal_objects", "f-closed"))
        closed = conn.execute("SELECT * FROM signal_objects WHERE object_id='f-closed'").fetchall()
        assert _deleted(dict(closed[0])) and _member_fingerprint([closed[0]], [closed]) is None


def test_the_owner_vetoes_the_legacy_retirement_in_one_line(record, monkeypatch):
    CASES["legacy_retirement"][0](record.conn, record.cite)
    assert record.code() is None
    monkeypatch.setattr(message_evidence, "LEGACY_RETIREMENT", None)
    assert record.closure("f-closed") is None and record.code() == "evidence_deleted"


def test_with_the_keys_the_successor_read_walks_no_facts(record):
    """The successor read runs on every qualification of a record a closed fact names (index builds and the
    release re-check). With the migration-78 keys it is two index searches, never a walk over every fact
    on the node, so its cost does not grow with facts the recipient never sees."""
    if not lk.installed(record.conn):
        pytest.skip("the table walk is the fallback without the keys, as facts_naming's is")
    _writer("superseded")(record.conn, record.cite)
    for number in range(50):
        fact(record.conn, f"hidden-{number}", refs=ELSEWHERE, key=f"fact:hidden:{number}")
    with record.resolver._read() as (conn, _floor):
        row = dict(conn.execute("SELECT * FROM signal_objects WHERE object_id='f-closed'").fetchone())
        executed = []
        conn.set_trace_callback(executed.append)
        assert closed_fact_release(conn, row) == "writer_supersession"
        conn.set_trace_callback(None)
        [statement] = [line for line in executed if "signal_objects s" in line]
        steps = [step[3] for step in conn.execute("EXPLAIN QUERY PLAN " + statement)]
    assert not any(step.startswith("SCAN") for step in steps), steps
    assert any("permissions_v2_fact_key_rows" in step for step in steps), steps


def test_a_message_releases_whole_through_its_owner_review(legacy):
    """The floor is one gate: with a re-derived closed fact naming it, the reviewed message qualifies."""
    from tests.permissions_v2.test_direct_message_evidence import qualify
    resolver, reviews, identity = message_setup(legacy)
    apply_signal_objects_updated_by_v1_up(legacy[1])
    cite = {"table": "conversation_messages", "record_id": "imessage:1", "source_id": "imessage",
            "dataset_id": "native-dataset"}
    _writer("correction")(legacy[1], cite)
    assert qualify(resolver, reviews, identity).family == "owner_authored_message/v1"
    legacy[1].execute("UPDATE signal_objects SET updated_by='owner_revision' WHERE object_id='f-closed'")
    legacy[1].commit()
    with pytest.raises(PolicyError, match="evidence_deleted"):
        qualify(resolver, reviews, identity)


# --- the engine's own closers, end to end on a journal entry -------------------------------------

@pytest.fixture
def journal(node):
    """A journal entry, an owner entity and the derivation writer's schema (all migrations)."""
    _entry(node, "e1")
    conn = sqlite3.connect(str(node))
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, is_self) "
                 "VALUES ('ent_owner', 'person', 'Owner', 'owner', '[]', 1)")
    conn.commit()
    resolver = _resolver(node)
    found = Record(conn, resolver, resolver._identity("journal_entries", "e1", SOURCE),
                   {"table": "journal_entries", "record_id": "e1", "source_id": SOURCE}, lambda name: _off_limits(node, name))
    assert found.code() is None
    yield found
    conn.close()


def _pack_fact(conn, *, model, role, refs):
    """DerivationWriter.assert_pack_fact as the derivation run (or, with an owner model, the owner) calls it."""
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.registry import bundled_pack_dir
    from topos.features.derivation.writer import DerivationWriter
    pack = load_packs(bundled_pack_dir(), only=["relationships.social"])["relationships.social"]
    return DerivationWriter(conn, model=model).assert_pack_fact(
        pack=pack, predicate="rel.relationship", subject_entity_id="ent_owner",
        value={"person": "Quillon", "role": role, "status": "active"}, actor_role="authored", source_refs=refs,
        confidence=0.9, quote="", about="owner")


def _naming(conn, cite):
    """(object_id, valid_to) of every fact naming the record, in rowid order."""
    return [(object_id, valid_to) for object_id, refs, valid_to in conn.execute(
        "SELECT object_id, source_refs_json, valid_to FROM signal_objects WHERE object_type='fact' ORDER BY rowid")
        if cite["record_id"] in refs]


@pytest.mark.parametrize("overlap, outcome, expected", [
    (False, "superseded", "writer_supersession"),     # new evidence: the world changed
    (True, "corrected", "writer_correction"),         # the same evidence read again
])
def test_a_derivation_rerun_releases_the_entry_its_old_reading_cited(journal, overlap, outcome, expected):
    first = _pack_fact(journal.conn, model="synthetic-local-model", role="friend", refs=[journal.cite])
    assert first["outcome"] == "written"
    again = _pack_fact(journal.conn, model="synthetic-local-model", role="close_friend",
                       refs=([journal.cite] if overlap else []) + ELSEWHERE)
    assert again["outcome"] == outcome
    assert journal.closure(first["object_id"]) == expected
    assert journal.code() is None


def test_the_owners_promote_through_the_writer_keeps_the_old_entry_withheld(journal):
    """promote_conflict and rate_person_disposition write through the same writer, under an owner model."""
    first = _pack_fact(journal.conn, model="synthetic-local-model", role="friend", refs=[journal.cite])
    assert _pack_fact(journal.conn, model="owner-promote", role="close_friend", refs=ELSEWHERE)["outcome"] == "superseded"
    assert journal.closure(first["object_id"]) is None
    assert journal.code() == "evidence_deleted"


def test_the_owners_revision_keeps_the_entry_withheld(journal):
    from topos.features.derivation.surfaces import revise_fact
    first = _pack_fact(journal.conn, model="synthetic-local-model", role="friend", refs=[journal.cite])
    revise_fact(journal.conn, first["object_id"], value={"person": "Quillon", "role": "close_friend", "status": "active"})
    assert [valid_to is not None for _id, valid_to in _naming(journal.conn, journal.cite)] == [True]
    assert journal.code() == "evidence_deleted"


def test_the_owners_exclusion_keeps_the_entry_withheld_after_the_tombstone_is_lifted(journal):
    from topos.features.lifecycle.exclusions import ExclusionStore
    _pack_fact(journal.conn, model="synthetic-local-model", role="friend", refs=[journal.cite])
    store = ExclusionStore(journal.conn)
    store.exclude_fact(subject_entity_id="ent_owner", predicate="rel.relationship", note="synthetic")
    journal.conn.commit()
    assert journal.code() == "evidence_deleted"
    assert store.remove_exclusion("fact", "ent_owner:rel.relationship")
    journal.conn.commit()
    assert journal.conn.execute("SELECT count(*) FROM intelligence_exclusions").fetchone()[0] == 0
    assert journal.code() == "evidence_deleted"


def test_a_fact_store_supersession_releases_and_the_owners_verdict_edit_does_not(journal):
    from topos.features.facts.store import FactStore
    from topos.features.facts.verdicts import edit_fact
    store = FactStore(journal.conn)

    def assert_fact(predicate, value, refs):
        return store.assert_fact(subject_entity_id="ent_owner", predicate=predicate, object_value=value, source_refs=refs,
                                 disclosure="owner_only", asserted_by="owner", confidence=0.8)["object_id"]

    first = assert_fact("works_at", "Acme Synthetic", [journal.cite])
    assert_fact("works_at", "Globex Synthetic", ELSEWHERE)
    assert journal.closure(first) == "fact_store_supersession"
    assert journal.code() is None
    studied = assert_fact("studied_at", "Synthetic College", [journal.cite])
    edit_fact(journal.conn, studied, object_value="Synthetic University")
    assert journal.closure(studied) is None
    assert journal.code() == "evidence_deleted"


def test_the_od46_lane_revising_its_own_fact_releases_the_entry(journal):
    from topos.permissions_v2.permitted_derivation import Spec, write_fact
    identity = journal.identity
    spec = Spec(kind="fact", predicate="works_at", value="Acme Synthetic")
    assert write_fact(journal.conn, subject="ent_owner", identity=identity, row={"content": "first words"}, spec=spec,
                      now=1_790_000_000) == "written"
    assert write_fact(journal.conn, subject="ent_owner", identity=identity, row={"content": "edited words"}, spec=spec,
                      now=1_790_000_100) == "superseded"
    journal.conn.commit()
    (closed, _), (current, open_) = _naming(journal.conn, journal.cite)
    assert open_ is None and journal.closure(closed) == "od46_revision"
    assert journal.code() is None


def test_the_source_deleted_sweep_keeps_a_returned_entry_withheld(journal):
    """The owner deleted the source data; the sweep closed the facts it backed; the row came back."""
    from topos.features.lifecycle.derived_scrub import close_dangling_facts
    fact(journal.conn, "f-legacy", refs=[{"table": "journal_entries", "record_id": "e1"}])
    journal.conn.execute("DELETE FROM journal_entries WHERE entry_id='e1'")
    journal.conn.commit()
    assert close_dangling_facts(journal.conn) == 1
    journal.conn.commit()
    _entry(journal.resolver.path, "e1")
    assert journal.closure("f-legacy") is None
    assert journal.code() == "evidence_deleted"
