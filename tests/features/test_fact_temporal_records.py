"""Fact temporal records, and the one case in which older evidence stops a supersession.

Two things are pinned here.

The record. Every fact row FactStore inserts carries ``topos-fact-temporal/v1``:
when the node asserted it, when the stated thing applies, and the evidence time
of its source, each at its own precision and provenance. It is written once. The
shared extractors record what they can honestly say (a resume year stays a year;
a message time stays ``unverified_producer`` unless the writer marked it an
ingestion-clock substitute; a ``native_source_clock`` label is never repeated,
because nothing on that path validated it).

The guard. Re-extracting old messages after new ones used to bring an old value
back. A store built with an ``EvidenceTrust`` refuses exactly that, and only when
every side of the comparison is a trusted native time. Every other case keeps
today's belief revision, which several tests below assert on purpose.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.facts.evidence_time import older_than_incumbent, recorded_evidence
from topos.features.facts.extract import extract_facts_from_batch
from topos.features.facts.llm_extract import extract_owner_facts_llm
from topos.features.facts.store import FactStore
from topos.features.facts.verdicts import apply_fact_verdict
from topos.features.temporal.points import parse_point
from topos.features.temporal.records import FactTemporal, event_time, fact_temporal, producer_clock
from topos.storage.canonical.canonical_store import SQLiteCanonicalStore
from topos.storage.canonical.conversations_tables import ensure_all_tables
from topos.storage.db.migrations import apply_all_migrations

OWNER = "ent_self"


@pytest.fixture()
def conn(tmp_path):
    db = sqlite3.connect(str(tmp_path / "facts.db"))
    apply_all_migrations(db)
    ensure_all_tables(db)
    SQLiteCanonicalStore(db)  # the canonical write columns (ingested_at, source_record_id, ...)
    db.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self)"
               " VALUES (?, 'person', 'Owner', 'owner', 1)", (OWNER,))
    db.commit()
    yield db
    db.close()


def message(conn, message_id, event_at, *, ingested_at="2026-09-16T12:00:00+00:00", dataset="dataset-a", **extra):
    columns = {"message_id": message_id, "conversation_id": "c1", "dataset_id": dataset, "source_id": "imessage",
               "sender_type": "human", "sender_id": "self", "is_from_self": 1, "content": "synthetic",
               "event_at": event_at, "ingested_at": ingested_at, **extra}
    conn.execute(f"INSERT INTO conversation_messages ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                 tuple(columns.values()))
    return {"table": "conversation_messages", "record_id": message_id, "source_id": "imessage", "dataset_id": dataset}


class Trust:
    """Vouches for exactly the rows named, the way an attested lane would."""

    def __init__(self, *trusted):
        self.trusted = set(trusted)

    def trusted_event_point(self, conn, row):
        if row["message_id"] not in self.trusted:
            return None
        return parse_point(row["event_at"], provenance="native_source_clock")


def works_at(store, value, ref, *, confidence=0.6, asserted_by="owner"):
    return store.assert_fact(subject_entity_id=OWNER, predicate="works_at", object_value=value, confidence=confidence,
                             source_refs=[ref] if isinstance(ref, dict) else ref, asserted_by=asserted_by)


def rows(conn):
    return [dict(zip(("value", "valid_from", "valid_to"), (json.loads(p)["object_value"], f, t))) for p, f, t in
            conn.execute("SELECT payload_json, valid_from, valid_to FROM signal_objects WHERE object_type='fact' ORDER BY created_at, rowid")]


def active(conn):
    return [row["value"] for row in rows(conn) if row["valid_to"] is None]


def conflicts(conn):
    return conn.execute("SELECT COUNT(*) FROM fact_conflicts").fetchone()[0]


def temporal(conn, value):
    raw = conn.execute("SELECT temporal_json FROM signal_objects WHERE object_type='fact' AND payload_json LIKE ?",
                       (f'%"object_value": "{value}"%',)).fetchone()[0]
    return FactTemporal.from_json(raw)


# --- the record ---------------------------------------------------------------

def test_an_inserted_fact_carries_the_record_it_was_given(conn):
    given = fact_temporal(applies_start=parse_point("2019", provenance="stated_in_content"),
                          evidence=parse_point("2026-01-05T09:00:00Z", provenance="unverified_producer"))
    works_at(FactStore(conn), "Ferrograph Instruments", {"table": "profile_records", "record_id": "p1"})
    FactStore(conn).assert_fact(subject_entity_id=OWNER, predicate="lives_in", object_value="Lisbon", temporal=given)
    assert temporal(conn, "Lisbon") == given
    default = temporal(conn, "Ferrograph Instruments")
    assert default.asserted.provenance == "producer_clock" and default.asserted.precision == "instant"
    assert not default.applies_start.known and not default.applies_end.known and not default.evidence.known


def test_a_refresh_never_rewrites_the_record(conn):
    store = FactStore(conn)
    works_at(store, "Ferrograph Instruments", {"table": "profile_records", "record_id": "p1"})
    before = conn.execute("SELECT temporal_json FROM signal_objects").fetchone()[0]
    store.assert_fact(subject_entity_id=OWNER, predicate="works_at", object_value="ferrograph instruments",
                      confidence=0.9, source_refs=[{"table": "profile_records", "record_id": "p2"}],
                      temporal=fact_temporal(evidence=parse_point("2030", provenance="stated_in_content")))
    assert conn.execute("SELECT temporal_json FROM signal_objects").fetchall() == [(before,)]


def test_only_a_record_is_accepted(conn):
    with pytest.raises(ValueError):
        FactStore(conn).assert_fact(subject_entity_id=OWNER, predicate="works_at", object_value="X Co",
                                    temporal={"asserted": "now"})


def test_a_database_without_the_column_inserts_exactly_as_before(conn):
    conn.execute("ALTER TABLE signal_objects DROP COLUMN temporal_json")
    fact = works_at(FactStore(conn), "Ferrograph Instruments", {"table": "profile_records", "record_id": "p1"})
    assert fact["valid_to"] is None and active(conn) == ["Ferrograph Instruments"]


# --- the guard: what stays exactly as today -----------------------------------

def test_without_a_trust_an_older_statement_still_supersedes(conn):
    """The documented resurrection on every path that cannot prove its rows."""
    newer, older = message(conn, "m2", "2026-03-01T10:00:00Z"), message(conn, "m1", "2025-01-01T10:00:00Z")
    store = FactStore(conn)
    works_at(store, "Newer Co", newer)
    works_at(store, "Older Co", older)
    assert active(conn) == ["Older Co"]


@pytest.mark.parametrize("case", ["challenger untrusted", "incumbent untrusted", "incumbent evidence in the future",
                                  "equal instants", "same instant in two offsets", "different attribution",
                                  "incumbent row deleted", "incumbent row excluded", "ref names another dataset",
                                  "validator raises", "challenger has an untrusted second ref"])
def test_anything_short_of_two_trusted_ordered_times_supersedes_as_today(conn, case):
    newer_time = {"equal instants": "2025-01-01T10:00:00Z",
                  "same instant in two offsets": "2025-01-01T19:00:00+09:00",
                  "incumbent evidence in the future": "2099-01-01T00:00:00Z"}.get(case, "2026-03-01T10:00:00Z")
    newer = message(conn, "m2", newer_time)
    older = message(conn, "m1", "2025-01-01T10:00:00Z")
    extra = message(conn, "m0", "2024-01-01T10:00:00Z")
    trusted = {"challenger untrusted": ("m2",), "incumbent untrusted": ("m1",)}.get(case, ("m1", "m2"))
    trust = Trust(*trusted)
    if case == "validator raises":
        trust.trusted_event_point = lambda conn, row: 1 / 0
    store = FactStore(conn, evidence_trust=trust)
    works_at(store, "Newer Co", newer if case != "ref names another dataset" else {**newer, "dataset_id": "dataset-b"},
             asserted_by="contact:c1" if case == "different attribution" else "owner")
    if case == "incumbent row deleted":
        conn.execute("DELETE FROM conversation_messages WHERE message_id='m2'")
    if case == "incumbent row excluded":
        conn.execute("INSERT INTO intelligence_exclusions (artifact_type, artifact_key) VALUES ('record', 'm2')")
    challenger = [older, extra] if case == "challenger has an untrusted second ref" else older
    works_at(store, "Older Co", challenger)
    assert active(conn) == ["Older Co"], case
    assert conflicts(conn) == 0 and store.outcomes == {}


def test_a_weak_older_challenger_still_queues_exactly_one_conflict(conn):
    newer, older = message(conn, "m2", "2026-03-01T10:00:00Z"), message(conn, "m1", "2025-01-01T10:00:00Z")
    store = FactStore(conn, evidence_trust=Trust("m1", "m2"))
    works_at(store, "Newer Co", newer, confidence=0.9)
    works_at(store, "Older Co", older, confidence=0.6)
    assert active(conn) == ["Newer Co"] and conflicts(conn) == 1 and len(rows(conn)) == 1


def test_a_newer_trusted_statement_supersedes(conn):
    older, newer = message(conn, "m1", "2025-01-01T10:00:00Z"), message(conn, "m2", "2026-03-01T10:00:00Z")
    store = FactStore(conn, evidence_trust=Trust("m1", "m2"))
    works_at(store, "Older Co", older)
    works_at(store, "Newer Co", newer)
    assert active(conn) == ["Newer Co"] and store.outcomes == {}


# --- the guard: the refusal ---------------------------------------------------

def test_an_older_trusted_statement_is_kept_as_history_and_the_incumbent_is_untouched(conn):
    newer, older = message(conn, "m2", "2026-03-01T10:00:00Z"), message(conn, "m1", "2025-01-01T10:00:00Z")
    store = FactStore(conn, evidence_trust=Trust("m1", "m2"))
    incumbent = works_at(store, "Newer Co", newer)
    stamp = conn.execute("SELECT updated_at, valid_from FROM signal_objects WHERE object_id=?", (incumbent["object_id"],)).fetchone()
    returned = works_at(store, "Older Co", older)
    assert returned["object_id"] == incumbent["object_id"]
    assert active(conn) == ["Newer Co"] and conflicts(conn) == 0
    assert conn.execute("SELECT updated_at, valid_from FROM signal_objects WHERE object_id=?", (incumbent["object_id"],)).fetchone() == stamp
    historical = [row for row in rows(conn) if row["value"] == "Older Co"]
    assert len(historical) == 1 and historical[0]["valid_from"] == historical[0]["valid_to"]
    assert [f["payload"]["object_value"] for f in store.history(OWNER, "works_at")] == ["Newer Co", "Older Co"]
    for as_of in ("2025-06-01T00:00:00+00:00", historical[0]["valid_from"], "2100-01-01T00:00:00+00:00"):
        assert "Older Co" not in [f["payload"]["object_value"] for f in store.facts_for_subject(OWNER, as_of=as_of)]
    assert store.outcomes == {"older_evidence_kept_as_history": 1}


def test_replaying_the_same_older_message_adds_nothing(conn):
    newer, older = message(conn, "m2", "2026-03-01T10:00:00Z"), message(conn, "m1", "2025-01-01T10:00:00Z")
    store = FactStore(conn, evidence_trust=Trust("m1", "m2"))
    works_at(store, "Newer Co", newer)
    for _ in range(4):
        works_at(store, "Older Co", older)
    assert len(rows(conn)) == 2 and conflicts(conn) == 0
    assert store.outcomes == {"older_evidence_kept_as_history": 1, "older_evidence_already_recorded": 3}


def test_a_newest_first_reprocess_keeps_the_newest_and_all_history(conn):
    refs = [message(conn, f"m{i}", f"202{i}-01-01T10:00:00Z") for i in (5, 4, 3)]
    store = FactStore(conn, evidence_trust=Trust("m3", "m4", "m5"))
    for ref, value in zip(refs, ("Third Co", "Second Co", "First Co")):
        works_at(store, value, ref)
    assert active(conn) == ["Third Co"]
    assert sorted(row["value"] for row in rows(conn)) == ["First Co", "Second Co", "Third Co"]
    assert all(row["valid_to"] is None or row["valid_from"] <= row["valid_to"] for row in rows(conn))
    hits = {f["payload"]["object_value"] for f in store.search(["co"], include_closed=True)}
    assert hits == {"First Co", "Second Co", "Third Co"}


def test_an_oldest_first_run_is_unchanged_by_the_guard(conn):
    refs = [message(conn, f"m{i}", f"202{i}-01-01T10:00:00Z") for i in (3, 4, 5)]
    trusted = FactStore(conn, evidence_trust=Trust("m3", "m4", "m5"))
    for ref, value in zip(refs, ("First Co", "Second Co", "Third Co")):
        works_at(trusted, value, ref)
    assert active(conn) == ["Third Co"] and trusted.outcomes == {}
    assert [row["valid_to"] is None for row in rows(conn)] == [False, False, True]


def test_a_corroborated_incumbent_is_judged_by_its_latest_support_not_its_first(conn):
    """Fails on insert-time evidence: the incumbent's first ref predates the challenger."""
    first, latest = message(conn, "m1", "2025-01-01T10:00:00Z"), message(conn, "m3", "2026-06-01T10:00:00Z")
    challenger = message(conn, "m2", "2026-01-01T10:00:00Z")
    store = FactStore(conn, evidence_trust=Trust("m1", "m2", "m3"))
    works_at(store, "Current Co", first)
    works_at(store, "Current Co", latest)
    works_at(store, "Interim Co", challenger)
    assert active(conn) == ["Current Co"]


def test_a_time_after_its_own_ingestion_cannot_lock_a_fact(conn):
    """The fabricated time is in the past by the wall clock; only its row's ingestion rules it out."""
    fabricated = message(conn, "m9", "2025-06-10T10:00:00Z", ingested_at="2025-06-01T00:00:00+00:00")
    real = message(conn, "m1", "2025-03-01T10:00:00Z", ingested_at="2025-03-02T00:00:00+00:00")
    store = FactStore(conn, evidence_trust=Trust("m1", "m9"))
    works_at(store, "Fake Future Co", fabricated)
    works_at(store, "Real Co", real)
    assert active(conn) == ["Real Co"]


@pytest.mark.parametrize("ingested_at,refuses", [
    ("2026-09-16T12:00:00+00:00", True),
    ("2026-09-16 12:00:00", True),          # SQLite datetime('now'), UTC by definition
    ("2026-09-16 12:00:00.123", True),      # the same with fractional seconds
    ("2026-09-16T12:00:00", False),         # naive: its span could end after the event
    ("yesterday", False),
    (None, False),
])
def test_only_a_usable_ingestion_time_is_a_ceiling(conn, ingested_at, refuses):
    newer = message(conn, "m2", "2026-03-01T10:00:00Z", ingested_at=ingested_at)
    older = message(conn, "m1", "2025-01-01T10:00:00Z")
    store = FactStore(conn, evidence_trust=Trust("m1", "m2"))
    works_at(store, "Newer Co", newer)
    works_at(store, "Older Co", older)
    assert active(conn) == (["Newer Co"] if refuses else ["Older Co"])


def test_a_trust_can_vouch_for_when_its_rows_existed(conn):
    """The snapshot lane leaves ingested_at empty and supplies the attestation time instead."""
    newer = message(conn, "m2", "2026-03-01T10:00:00Z", ingested_at=None)
    older = message(conn, "m1", "2025-01-01T10:00:00Z", ingested_at=None)
    trust = Trust("m1", "m2")
    trust.existed_by = lambda c, row: parse_point("2026-04-01T00:00:00Z", provenance="native_source_clock")
    store = FactStore(conn, evidence_trust=trust)
    works_at(store, "Newer Co", newer)
    works_at(store, "Older Co", older)
    assert active(conn) == ["Newer Co"]
    trust.existed_by = lambda c, row: parse_point("2026-02-01T00:00:00Z", provenance="native_source_clock")
    works_at(store, "Older Co", message(conn, "m0", "2024-01-01T10:00:00Z", ingested_at=None))
    assert active(conn) == ["Older Co"], "m2 cannot have happened by that ceiling, so nothing is ordered"


def test_the_guard_orders_only_native_source_clock_explicit_basis_points(conn):
    naive = message(conn, "m2", "2026-03-01T10:00:00")
    older = message(conn, "m1", "2025-01-01T10:00:00Z")
    incumbent = {"payload": {"asserted_by": "owner"}, "source_refs": [naive]}
    assert older_than_incumbent(conn, Trust("m1", "m2"), challenger_refs=[older], challenger_asserted_by="owner",
                                incumbent=incumbent) is False


@pytest.mark.parametrize("vouch", ["stated_in_content", "unverified_producer", "ingestion_clock_substitute",
                                   "producer_clock", "different text"])
def test_the_guard_orders_nothing_the_trust_does_not_vouch_for_as_this_rows_native_clock(conn, vouch):
    newer = message(conn, "m2", "2026-03-01T10:00:00Z")
    older = message(conn, "m1", "2025-01-01T10:00:00Z")
    trust = Trust("m1", "m2")
    if vouch == "different text":
        # The same instant spelled differently is still not this row's recorded time.
        trust.trusted_event_point = lambda c, row: parse_point(row["event_at"].replace("Z", "+00:00"), provenance="native_source_clock")
    else:
        trust.trusted_event_point = lambda c, row: parse_point(row["event_at"], provenance=vouch)
    store = FactStore(conn, evidence_trust=trust)
    works_at(store, "Newer Co", newer)
    works_at(store, "Older Co", older)
    assert active(conn) == ["Older Co"] and store.outcomes == {}


def test_the_challengers_latest_ref_is_compared(conn):
    support = message(conn, "m2", "2026-03-01T10:00:00Z")
    early, late = message(conn, "m1", "2025-01-01T10:00:00Z"), message(conn, "m3", "2026-06-01T10:00:00Z")
    store = FactStore(conn, evidence_trust=Trust("m1", "m2", "m3"))
    works_at(store, "Incumbent Co", support)
    works_at(store, "Challenger Co", [early, late])
    assert active(conn) == ["Challenger Co"]


def test_one_trusted_incumbent_ref_is_enough_support(conn):
    trusted, untrusted = message(conn, "m2", "2026-03-01T10:00:00Z"), message(conn, "m4", "2026-04-01T10:00:00Z")
    older = message(conn, "m1", "2025-01-01T10:00:00Z")
    store = FactStore(conn, evidence_trust=Trust("m1", "m2"))
    works_at(store, "Incumbent Co", [trusted, untrusted])
    works_at(store, "Older Co", older)
    assert active(conn) == ["Incumbent Co"]


@pytest.mark.parametrize("change", [{"source_id": "signal"}, {"dataset_id": None}, {"source_id": None}])
def test_a_ref_counts_only_when_it_names_its_rows_source_and_dataset(conn, change):
    newer = message(conn, "m2", "2026-03-01T10:00:00Z")
    ref = {key: value for key, value in {**newer, **change}.items() if value is not None}
    store = FactStore(conn, evidence_trust=Trust("m1", "m2"))
    works_at(store, "Newer Co", ref)
    works_at(store, "Older Co", message(conn, "m1", "2025-01-01T10:00:00Z"))
    assert active(conn) == ["Older Co"]


def test_a_dataset_less_ref_merged_by_a_shared_extractor_never_borrows_another_rows_time(conn):
    """A shared extractor refreshes the fact with {table, record_id, source_id} for a colliding id."""
    old = message(conn, "imessage:5", "2025-01-01T10:00:00Z")
    unrelated = message(conn, "imessage:7", "2026-06-01T10:00:00Z")
    between = message(conn, "imessage:6", "2026-03-01T10:00:00Z")
    trusted = FactStore(conn, evidence_trust=Trust("imessage:5", "imessage:6", "imessage:7"))
    works_at(trusted, "Acme Co", old)
    works_at(FactStore(conn), "Acme Co", {"table": "conversation_messages", "record_id": "imessage:7", "source_id": "imessage"})
    works_at(trusted, "Globex", between)
    assert active(conn) == ["Globex"]


def test_a_merged_ref_from_someone_elses_message_is_not_owner_support(conn):
    owner_old = message(conn, "m1", "2025-01-01T10:00:00Z")
    contact_later = message(conn, "m3", "2026-06-01T10:00:00Z", is_from_self=0, sender_id="+15555550123")
    owner_newer = message(conn, "m2", "2026-01-01T10:00:00Z")
    trusted = FactStore(conn, evidence_trust=Trust("m1", "m2", "m3"))
    works_at(trusted, "Acme Co", owner_old)
    works_at(FactStore(conn), "Acme Co", contact_later, asserted_by="contact:+15555550123")
    works_at(trusted, "Globex", owner_newer)
    assert active(conn) == ["Globex"]


def test_only_owner_statements_are_ordered(conn):
    newer, older = message(conn, "m2", "2026-03-01T10:00:00Z"), message(conn, "m1", "2025-01-01T10:00:00Z")
    store = FactStore(conn, evidence_trust=Trust("m1", "m2"))
    works_at(store, "Newer Co", newer, asserted_by="assistant")
    works_at(store, "Older Co", older, asserted_by="assistant")
    assert active(conn) == ["Older Co"] and store.outcomes == {}


def test_a_multi_valued_prefix_collision_is_never_ordered(conn):
    prefix = "distributed systems engineering for very large scale "
    newer, older = message(conn, "m2", "2026-03-01T10:00:00Z"), message(conn, "m1", "2025-01-01T10:00:00Z")
    store = FactStore(conn, evidence_trust=Trust("m1", "m2"))
    store.assert_fact(subject_entity_id=OWNER, predicate="skilled_in", object_value=prefix + "data", source_refs=[newer])
    store.assert_fact(subject_entity_id=OWNER, predicate="skilled_in", object_value=prefix + "robotics", source_refs=[older])
    assert store.outcomes == {}


def test_older_restatements_of_a_superseded_value_fold_into_one_history_row(conn):
    first = message(conn, "r1", "2026-08-01T10:00:00Z")
    second = message(conn, "r2", "2026-08-02T10:00:00Z")
    store = FactStore(conn, evidence_trust=Trust("r1", "r2", *[f"o{i:02d}" for i in range(1, 13)]))
    store.assert_fact(subject_entity_id=OWNER, predicate="lives_in", object_value="Porto", source_refs=[first])
    store.assert_fact(subject_entity_id=OWNER, predicate="lives_in", object_value="Lisbon", source_refs=[second])
    for i in range(1, 13):
        ref = message(conn, f"o{i:02d}", f"2025-{i:02d}-01T10:00:00Z")
        store.assert_fact(subject_entity_id=OWNER, predicate="lives_in", object_value="Porto", source_refs=[ref])
    porto = [f for f in store.history(OWNER, "lives_in") if f["payload"]["object_value"] == "Porto"]
    assert len(porto) == 1 and len(porto[0]["source_refs"]) == 13
    assert [f["payload"]["object_value"] for f in store.facts_for_subject(OWNER)] == ["Lisbon"]
    assert {f["payload"]["object_value"] for f in store.search(["porto", "lisbon"], include_closed=True)} == {"Porto", "Lisbon"}
    assert store.outcomes == {"older_evidence_corroborated": 12}


# --- producers ------------------------------------------------------------------

def owner_row(conn, message_id, event_at, *, text="I work at Ferrograph Instruments", **record):
    SQLiteCanonicalStore(conn).upsert("conversation_messages", {
        "message_id": message_id, "conversation_id": "c1", "dataset_id": "dataset-a", "source_id": "imessage",
        "sender_type": "human", "sender_id": "self", "is_from_self": 1, "content": text, "event_at": event_at, **record})
    row = dict(zip(*(lambda cur: ([d[0] for d in cur.description], cur.fetchone()))(
        conn.execute("SELECT * FROM conversation_messages WHERE message_id=?", (message_id,)))))
    return {**{k: v for k, v in row.items() if k != "event_time_json"}, "_table": "conversation_messages"}


def test_the_rules_extractor_records_an_unverified_message_time(conn):
    row = owner_row(conn, "imessage:1", "2026-02-01T10:00:00+00:00")
    assert extract_facts_from_batch(conn, [row]) == 1
    evidence = temporal(conn, "Ferrograph Instruments").evidence
    assert (evidence.text, evidence.provenance) == ("2026-02-01T10:00:00+00:00", "unverified_producer")


def test_the_rules_extractor_keeps_an_ingestion_clock_mark(conn):
    row = owner_row(conn, "imessage:1", None)
    extract_facts_from_batch(conn, [row])
    evidence = temporal(conn, "Ferrograph Instruments").evidence
    assert evidence.provenance == "ingestion_clock_substitute" and evidence.text == row["event_at"]


def test_an_unvalidated_native_label_is_never_repeated_in_a_fact_record(conn):
    row = owner_row(conn, "imessage:1", "2026-02-01T10:00:00+00:00")
    conn.execute("UPDATE conversation_messages SET event_time_json=? WHERE message_id='imessage:1'",
                 (event_time(row["event_at"], provenance="native_source_clock").to_json(),))
    extract_facts_from_batch(conn, [row])
    assert temporal(conn, "Ferrograph Instruments").evidence.provenance == "unverified_producer"


@pytest.mark.parametrize("foreign", [{"dataset_id": "dataset-b", "source_id": "imessage"},
                                     {"dataset_id": "dataset-a", "source_id": "signal"},
                                     {"source_id": "imessage"}, {"dataset_id": "dataset-a"}, {}])
def test_another_rows_record_is_never_borrowed(conn, foreign):
    """Same id and the same time text: only the dataset and source can tell the rows apart."""
    stored = owner_row(conn, "imessage:1", None)
    row = {"message_id": "imessage:1", "event_at": stored["event_at"], **foreign}
    point = recorded_evidence(conn, "conversation_messages", row)
    assert (point.text, point.provenance) == (stored["event_at"], "unverified_producer")
    own = {"message_id": "imessage:1", "event_at": stored["event_at"], "dataset_id": "dataset-a", "source_id": "imessage"}
    assert recorded_evidence(conn, "conversation_messages", own).provenance == "ingestion_clock_substitute"


def test_every_fact_from_one_resume_row_records_its_stated_years(conn):
    extract_facts_from_batch(conn, [{"_table": "profile_records", "record_id": "prof-2", "record_type": "experience",
                                     "title": "Staff Engineer", "organization": "Lumon Industries",
                                     "description": "2019 - present, lead the platform team"}])
    for value in ("Lumon Industries", "Staff Engineer"):
        record = temporal(conn, value)
        assert (record.applies_start.text, record.applies_start.precision) == ("2019", "year"), value
        assert not record.applies_end.known
    payload = json.loads(conn.execute("SELECT payload_json FROM signal_objects WHERE payload_json LIKE '%Staff Engineer%'").fetchone()[0])
    assert "period_start" not in payload  # the legacy payload is unchanged


def test_a_resume_year_stays_a_year(conn):
    extract_facts_from_batch(conn, [{"_table": "profile_records", "record_id": "prof-1", "record_type": "experience",
                                     "title": "Engineer", "organization": "Lumon Industries",
                                     "description": "2019 - 2024 built things"}])
    record = temporal(conn, "Lumon Industries")
    assert (record.applies_start.text, record.applies_start.precision, record.applies_start.provenance) == ("2019", "year", "stated_in_content")
    assert (record.applies_end.text, record.applies_end.precision) == ("2024", "year")
    assert not record.evidence.known
    assert conn.execute("SELECT valid_from FROM signal_objects").fetchone()[0] == "2019-01-01T00:00:00+00:00"  # legacy, unchanged


def test_a_journal_practice_records_its_entry_time(conn):
    extract_facts_from_batch(conn, [{"_table": "journal_entries", "entry_id": "j1", "entry_at": "2026-04-02",
                                     "category": "yoga", "content": "morning session"}])
    evidence = temporal(conn, "yoga").evidence
    assert (evidence.text, evidence.precision, evidence.provenance) == ("2026-04-02", "day", "unverified_producer")


def test_a_model_period_is_recorded_as_unverified_at_its_own_precision(conn):
    row = owner_row(conn, "imessage:2", "2026-02-01T10:00:00+00:00", text="I have preferred cold brew since 2019")
    stub = lambda prompt, r: [{"predicate": "prefers", "object": "cold brew", "period_start": "2019"}]
    assert extract_owner_facts_llm(conn, [row], extractor=stub) == 1
    record = temporal(conn, "cold brew")
    assert (record.applies_start.precision, record.applies_start.provenance) == ("year", "unverified_producer")
    assert record.evidence.provenance == "unverified_producer"


def test_an_owner_correction_is_an_owner_edit_carrying_the_evidence_forward(conn):
    evidence = parse_point("2026-02-01T10:00:00+00:00", provenance="unverified_producer")
    start, end = parse_point("2019", provenance="stated_in_content"), parse_point("2024-06", provenance="stated_in_content")
    fact = FactStore(conn).assert_fact(subject_entity_id=OWNER, predicate="lives_in", object_value="Lisbn",
                                       temporal=fact_temporal(evidence=evidence, applies_start=start, applies_end=end),
                                       asserted_by="assistant")
    out = apply_fact_verdict(conn, object_id=fact["object_id"], action="edit", object_value="Lisbon")
    record = temporal(conn, "Lisbon")
    assert record.asserted.provenance == "owner_edit" and record.evidence == evidence
    assert (record.applies_start, record.applies_end) == (start, end)
    assert out["payload"]["asserted_by"] == "assistant"


@pytest.mark.parametrize("other", ["assistant", "contact:c1"])
def test_a_correction_landing_on_someone_elses_row_keeps_the_corrected_facts_attribution(conn, other):
    store = FactStore(conn)
    store.assert_fact(subject_entity_id=OWNER, predicate="skilled_in", object_value="Rust", asserted_by=other)
    wrong = store.assert_fact(subject_entity_id=OWNER, predicate="skilled_in", object_value="Rsut", asserted_by="owner")
    out = apply_fact_verdict(conn, object_id=wrong["object_id"], action="edit", object_value="Rust")
    assert out["payload"]["asserted_by"] == "owner" and out["payload"]["verified_by_owner"] is True


def test_an_explicit_attribution_wins_even_when_the_correction_lands_on_an_existing_row(conn):
    store = FactStore(conn)
    store.assert_fact(subject_entity_id=OWNER, predicate="skilled_in", object_value="Rust", asserted_by="assistant")
    wrong = store.assert_fact(subject_entity_id=OWNER, predicate="skilled_in", object_value="Rsut", asserted_by="assistant")
    out = apply_fact_verdict(conn, object_id=wrong["object_id"], action="edit", object_value="Rust", asserted_by="owner")
    assert out["payload"]["asserted_by"] == "owner"


def test_an_asserting_clock_is_a_producer_or_an_owner(conn):
    assert producer_clock(provenance="owner_edit").provenance == "owner_edit"
    with pytest.raises(ValueError):
        producer_clock(provenance="native_source_clock")
