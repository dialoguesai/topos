"""Facts a pack stored from a reaction the owner sent in Messages: counted for anyone, ended only on the owner's yes.

Skipping a reaction (`test_owner_reactions.py`) stops new facts of this kind. It leaves the ones already stored.
`features/derivation/reaction_retract.py` is the owner's way to end those, behind
`GET /v1/signal/derivation/reaction-facts` and `POST /v1/signal/derivation/reaction-facts/retract`.

protects: the count is a read and writes nothing. Ending needs the owner's confirmation on the owner socket (the
app, or a yes on a terminal) and is refused over TCP and without it. What is ended is exactly the standing pack
facts whose every source record is a reaction the owner sent, by the test the packs and the commitments lane
apply; a fact with any other source, a contact's reaction, a record that is gone, or the owner's own confirmation
stays. So does a fact any owner door made (confirm, correct, revise, promote, declare, override), a fact that
cites its records only for when they were sent, and a fact the derivation ledger shows, or cannot rule out, was
also stored from a record that is not a reaction (the writer can replace an event's source list when the event
is told again). An ended fact keeps its row and its value (it is closed, the way a superseded fact is), no
message row changes, the write is made under the write gate and committed, and a later backfill does not bring
it back. Nothing but pack ids and counts is answered. Every row here is invented.
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.features.derivation.test_owner_reactions import (
    ORDINARY_METADATA, OTHER_LANGUAGE, REACTION_METADATA, THEIRS, _message, _stub_models)
from topos import owner_door_routes as census, uds
from topos.enrichment.jobs.canonical import derivation_job as dj
from topos.features.derivation import reaction_retract as retract
from topos.features.derivation.packs import load_packs
from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry
from topos.features.derivation.writer import DerivationWriter
from topos.storage.canonical.conversations_tables import ensure_all_tables
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.db.write_gate import db_write_lock

COUNT = "/v1/signal/derivation/reaction-facts"
RETRACT = COUNT + "/retract"
REFUSAL = {"error": "owner_confirmation_required", "message": "This change needs the Topos app."}
CAREER = "work.career"
JOY = "moments.joy"
MESSAGES = "conversation_messages"
#: Strings that are in the facts and the messages and must never be in an answer or on the terminal.
PRIVATE = ("Meridian", "Northwind", "Harbor", "the lift home", "Signed the offer")


@pytest.fixture
def node(tmp_path, monkeypatch):
    """A scratch node: an owner, the bundled packs, no facts and no messages yet. No model is ever loaded."""
    monkeypatch.setattr("topos.features.signal.derived_index.index_derived_objects", lambda *a, **kw: {"embedded": 0})
    path = tmp_path / "node.db"
    conn = sqlite3.connect(path, check_same_thread=False)
    ensure_all_tables(conn)
    apply_all_migrations(conn)
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, is_self)"
                 " VALUES ('ent_owner','person','Owner','owner','[]',1)")
    conn.execute("CREATE TABLE IF NOT EXISTS engine_config (key TEXT PRIMARY KEY, value TEXT)")
    seed_pack_registry(conn, bundled_pack_dir())
    conn.commit()
    yield SimpleNamespace(conn=conn, path=path)
    conn.close()


def _plant(conn, pack: str, predicate: str, value: Dict[str, Any], *sources, model: str = "stub-9b",
           role: str = "authored", occurrence: str = None, outcome: str = "written", ledger: bool = False) -> str:
    """A standing fact read from these records, stored by the packs' own writer. `ledger` adds the row the
    derivation job writes for a stored assertion."""
    loaded = load_packs(bundled_pack_dir())[pack]
    out = DerivationWriter(conn, model=model).assert_pack_fact(
        pack=loaded, predicate=predicate, subject_entity_id="ent_owner", value=value,
        actor_role=role, confidence=0.9, quote="Signed the offer", event_date="2026-09-20", occurrence=occurrence,
        source_refs=[{"table": table, "record_id": rid} for table, rid in sources])
    assert out["outcome"] == outcome, out
    if ledger:
        _ledger(conn, out["object_id"], *sources, pack=pack, predicate=predicate)
    return out["object_id"]


def _ledger(conn, object_id: str, *sources, pack: str = "work.career", predicate: str = "work.career_event") -> None:
    for table, rid in sources:
        dj._ledger_write(conn, stage="backfill", pack=load_packs(bundled_pack_dir())[pack],
                         rec={"table": table, "record_id": rid, "role": "authored"},
                         a={"predicate": predicate, "value": {}}, model="stub-9b", vmodel="stub-9b",
                         written_id=object_id)
    conn.commit()


def _employer(conn, org: str, *sources, **kw) -> str:
    return _plant(conn, CAREER, "work.employer", {"org": org}, *sources, **kw)


@pytest.fixture
def stored(node):
    """Five messages, a journal entry, and eleven facts: three a yes ends, eight it must leave."""
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”")                                   # a reaction by its words
    _message(conn, "r-marker", OTHER_LANGUAGE, metadata=REACTION_METADATA)           # a reaction by its marker
    _message(conn, "own", THEIRS, metadata=ORDINARY_METADATA)                        # what the owner typed
    _message(conn, "theirs", f"Loved “{THEIRS}”", is_self=False, metadata=REACTION_METADATA)
    conn.execute("INSERT INTO journal_entries (entry_id, entry_at, content, source_id, source_record_id)"
                 " VALUES ('j1', '2026-09-20T09:00:00Z', ?, 'grow_journal', 'r1')", (THEIRS,))
    conn.commit()
    words, marker = (MESSAGES, "r-words"), (MESSAGES, "r-marker")
    ended = {
        "one_reaction": _employer(conn, "Meridian", words),
        "two_reactions": _employer(conn, "Northwind", words, marker),
        "another_pack": _plant(conn, JOY, "moment.gratitude", {"person": "a friend", "what": "the lift home"}, marker),
    }
    left = {
        "reaction_and_own_message": _employer(conn, "Harbor", words, (MESSAGES, "own")),
        "contacts_reaction": _employer(conn, "Larch", (MESSAGES, "theirs")),
        "journal_entry": _employer(conn, "Quay", ("journal_entries", "j1")),
        "reaction_and_journal_entry": _employer(conn, "Fell", marker, ("journal_entries", "j1")),
        "record_no_longer_stored": _employer(conn, "Tarn", (MESSAGES, "gone")),
        "no_sources": _employer(conn, "Wold"),
        "confirmed_by_the_owner": _employer(conn, "Combe", words),
        "already_closed": _employer(conn, "Strand", words),
    }
    from topos.features.facts.verdicts import confirm_fact
    confirm_fact(conn, left["confirmed_by_the_owner"])
    conn.execute("UPDATE signal_objects SET valid_to='2026-09-21T00:00:00+00:00' WHERE object_id=?",
                 (left["already_closed"],))
    conn.commit()
    return SimpleNamespace(ended=ended, left=left)


def _facts(conn) -> Dict[str, tuple]:
    return {row[0]: row for row in conn.execute(
        "SELECT object_id, valid_to, payload_json, source_refs_json, updated_at, object_key FROM signal_objects"
        " WHERE object_type='fact'")}


def _messages(conn) -> List[tuple]:
    return conn.execute(f"SELECT * FROM {MESSAGES} ORDER BY message_id").fetchall()


EXPECTED = {"packs": [{"pack_id": JOY, "facts": 1}, {"pack_id": CAREER, "facts": 2}], "total": 3, "kept_confirmed": 1,
            "kept_other_record": 0}
NONE = {"packs": [], "total": 0, "kept_confirmed": 0, "kept_other_record": 0}


# ----------------------------------------------------------------------------- the count


def test_the_count_is_per_pack_and_writes_nothing(node, stored) -> None:
    before, messages, changes = _facts(node.conn), _messages(node.conn), node.conn.total_changes
    assert retract.count(node.conn) == EXPECTED
    assert {f.object_id for f in retract.find(node.conn).facts} == set(stored.ended.values())
    assert _facts(node.conn) == before and _messages(node.conn) == messages
    assert node.conn.total_changes == changes and not node.conn.in_transaction


def test_the_detection_is_the_lanes_own_not_a_copy(node, stored, monkeypatch) -> None:
    """Whatever the packs call a reaction is what is counted: switch their test off and nothing qualifies."""
    from topos.features.facts import reactions
    with monkeypatch.context() as patched:
        patched.setattr(reactions, "is_reaction", lambda row, text: False)
        assert retract.count(node.conn)["total"] == 0
    with monkeypatch.context() as patched:
        patched.setattr(dj, "_owner_reaction", lambda *a, **kw: False)
        assert retract.count(node.conn)["total"] == 0
    assert retract.count(node.conn) == EXPECTED


def test_only_the_commitments_pack_is_judged_by_the_sender_flag_alone(node, stored) -> None:
    """On a source the owner marked ambient an owner message reads as observed. The packs' rule then leaves a
    reaction in (a pack that takes observed text still reads it), so a fact another pack holds from it is not
    ended: a backfill could store it again. The commitments lane picks by the sender flag whatever the posture,
    and skips the reaction, so its pack's fact is."""
    from topos.features.derivation.commitments import PACK_ID
    promise = _plant(node.conn, PACK_ID, "commit.made",
                     {"counterparty": "a friend", "description": "send the signed copy"}, (MESSAGES, "r-marker"))
    mixed = _plant(node.conn, PACK_ID, "commit.made",
                   {"counterparty": "a friend", "description": "book the room"},
                   (MESSAGES, "r-marker"), (MESSAGES, "own"))
    found = retract.find(node.conn, posture_for=lambda row: "ambient")
    assert [f.object_id for f in found.facts] == [promise]
    usual = {f.object_id for f in retract.find(node.conn).facts}
    assert usual == set(stored.ended.values()) | {promise} and mixed not in usual


def test_a_database_with_no_pack_facts_or_no_messages_holds_none() -> None:
    bare = sqlite3.connect(":memory:")
    assert retract.count(bare) == NONE
    assert retract.retract(bare) == {**NONE, "retracted": 0, "changed": 0}
    bare.execute("CREATE TABLE signal_objects (object_id TEXT, object_type TEXT, valid_to TEXT, ontology_id TEXT,"
                 " source_refs_json TEXT, payload_json TEXT, updated_at TEXT, created_by TEXT, extractor_version TEXT)")
    bare.execute("INSERT INTO signal_objects VALUES ('f1','fact',NULL,'work.career',?,'{}',NULL,'system','v1')",
                 (json.dumps([{"table": MESSAGES, "record_id": "m1"}]),))
    assert retract.count(bare)["total"] == 0, "no messages table: nothing can be shown to be a reaction"


# ----------------------------------------------------------------------------- the yes


def test_a_yes_ends_exactly_those_facts_and_deletes_nothing(node, stored) -> None:
    before, messages = _facts(node.conn), _messages(node.conn)
    gated: List[bool] = []
    writes: set = set()

    def trace(sql: str) -> None:
        if sql.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE", "REPLACE")):
            gated.append(db_write_lock()._is_owned())
            writes.add(sql)                        # the table's triggers report the statement again, once each

    node.conn.set_trace_callback(trace)
    out = retract.retract(node.conn, expected_total=3)
    node.conn.set_trace_callback(None)
    assert out == {**EXPECTED, "retracted": 3, "changed": 0}
    assert len(writes) == 3 and all(sql.startswith("UPDATE signal_objects SET valid_to") for sql in writes), \
        "one statement per fact, and no other write"
    assert gated and all(gated), "a fact was ended without the write gate"
    assert not node.conn.in_transaction

    other = sqlite3.connect(node.path)                    # another connection reads it: it was committed
    try:
        after = _facts(other)
    finally:
        other.close()
    assert set(after) == set(before), "no row is deleted and none is added"
    for name, object_id in stored.ended.items():
        _id, valid_to, payload_json, refs, updated_at, key = after[object_id]
        was = json.loads(before[object_id][2])
        assert valid_to is not None and valid_to == updated_at, name
        assert json.loads(payload_json) == {**was, "closed_reason": retract.CLOSED_REASON}, "the value stays, closed"
        assert refs == before[object_id][3] and key == before[object_id][5], "its sources and key are as they were"
    for name, object_id in stored.left.items():
        assert after[object_id] == before[object_id], name
    assert _messages(node.conn) == messages, "no source record is touched"
    assert retract.count(node.conn) == {**NONE, "kept_confirmed": 1}
    assert retract.retract(node.conn)["retracted"] == 0, "a second yes finds nothing"


def test_a_count_that_is_not_the_one_shown_ends_nothing(node, stored) -> None:
    before = _facts(node.conn)
    for shown in (0, 2, 4):
        with pytest.raises(retract.RetractRefused) as refused:
            retract.retract(node.conn, expected_total=shown)
        assert refused.value.code == "reaction_facts_changed"
    assert _facts(node.conn) == before and not node.conn.in_transaction


def test_a_fact_that_changed_after_it_was_judged_is_left(node, stored, monkeypatch) -> None:
    """Between the read and the gated write a fact can gain a second source or the owner's verdict."""
    real_find = retract.find

    def find_then_change(conn, **kw):
        found = real_find(conn, **kw)
        refs = json.dumps([{"table": MESSAGES, "record_id": "r-words"}, {"table": MESSAGES, "record_id": "own"}])
        conn.execute("UPDATE signal_objects SET source_refs_json=? WHERE object_id=?",
                     (refs, stored.ended["one_reaction"]))
        conn.commit()
        return found

    monkeypatch.setattr(retract, "find", find_then_change)
    out = retract.retract(node.conn, expected_total=3)
    assert out["retracted"] == 2 and out["changed"] == 1
    assert _facts(node.conn)[stored.ended["one_reaction"]][1] is None, "it now has a source that is not a reaction"


# ----------------------------------------------------------------------------- what the owner made


def _standing(conn) -> set:
    return {oid for oid, row in _facts(conn).items() if row[1] is None}


def _left(conn, *, confirmed: int = 0, other: int = 0) -> None:
    """Nothing is listed, the count says why, and a yes ends nothing."""
    before = _facts(conn)
    assert retract.find(conn).facts == []
    assert retract.count(conn) == {**NONE, "kept_confirmed": confirmed, "kept_other_record": other}
    assert retract.retract(conn, expected_total=0)["retracted"] == 0
    assert _facts(conn) == before


def test_a_fact_the_owner_revised_in_the_app_is_left(node) -> None:
    """`surfaces.revise_fact` closes the fact and stores the owner's value with the same records. The new fact
    carries neither payload key the verdict doors write: only its extractor says the owner made it."""
    from topos.features.derivation.surfaces import revise_fact
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    first = _employer(conn, "Meridian", (MESSAGES, "r-words"))
    assert [f.object_id for f in retract.find(conn).facts] == [first]
    revised = revise_fact(conn, first, value={"org": "Northwind"})
    assert revised["object_id"] and revised["object_id"] != first, revised
    _id, valid_to, payload_json, refs, *_rest = _facts(conn)[revised["object_id"]]
    payload = json.loads(payload_json)
    assert valid_to is None and [r["record_id"] for r in json.loads(refs)] == ["r-words"]
    assert payload["extractor"]["model"] == "owner-revise"
    assert "verified_by_owner" not in payload and "corrected_from" not in payload
    _left(conn, confirmed=1)
    assert _standing(conn) == {revised["object_id"]}


def test_a_fact_the_owner_promoted_from_a_held_assertion_is_left(node) -> None:
    """`surfaces.promote_conflict` stores the held assertion with its records through a writer named
    `owner-promote`, and nothing else on the row says the owner did it."""
    from topos.features.derivation.surfaces import promote_conflict
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    DerivationWriter(conn, model="stub-9b")._quarantine(
        "ent_owner", "work.employer", {"org": "Meridian"}, 0.9, reason="held_for_review",
        pack=load_packs(bundled_pack_dir())[CAREER], source_refs=[{"table": MESSAGES, "record_id": "r-words"}],
        quote="Signed the offer", about="owner")
    conn.commit()
    (held,) = conn.execute("SELECT conflict_id FROM fact_conflicts").fetchone()
    promoted = promote_conflict(conn, held, to_owner=True)
    assert promoted["object_id"], promoted
    _id, valid_to, payload_json, refs, *_rest = _facts(conn)[promoted["object_id"]]
    payload = json.loads(payload_json)
    assert valid_to is None and [r["record_id"] for r in json.loads(refs)] == ["r-words"]
    assert payload["extractor"]["model"] == "owner-promote"
    assert "verified_by_owner" not in payload and "corrected_from" not in payload
    _left(conn, confirmed=1)


@pytest.mark.parametrize("mark", [
    pytest.param("UPDATE signal_objects SET created_by='owner'", id="created_by_the_owner"),
    pytest.param("UPDATE signal_objects SET extractor_version='owner_override'", id="an_owner_extractor_version"),
    pytest.param("UPDATE signal_objects SET payload_json=json_set(payload_json, '$.extractor.model', 'owner-informant')",
                 id="the_informant_door"),
    pytest.param("UPDATE signal_objects SET payload_json=json_set(payload_json, '$.extractor.model', 7)",
                 id="an_extractor_that_is_not_a_name"),
    pytest.param("UPDATE signal_objects SET payload_json=json_set(payload_json, '$.corrected_from', 'x')",
                 id="corrected_in_the_app"),
    pytest.param("UPDATE signal_objects SET payload_json='not json'", id="a_payload_that_cannot_be_read"),
])
def test_every_mark_an_owner_door_leaves_keeps_the_fact(node, mark) -> None:
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    _employer(conn, "Meridian", (MESSAGES, "r-words"))
    assert retract.count(conn)["total"] == 1
    conn.execute(mark)
    conn.commit()
    _left(conn, confirmed=1)


def test_a_fact_that_cites_records_for_their_timing_is_left(node) -> None:
    """The rhythm kernel (`synthesize.py`) cites a sample of the records the owner sent for WHEN they were sent.
    A reaction was sent by the owner at that time, whatever it quotes."""
    conn = node.conn
    sources = []
    for n in range(3):
        _message(conn, f"r{n}", OTHER_LANGUAGE, metadata=REACTION_METADATA, event_at=f"2026-09-0{n + 1}T07:00:00+00:00")
        sources.append((MESSAGES, f"r{n}"))
    label = "Mon 06:00-08:00"
    _plant(conn, "behavior.habits", "behavior.routine_block",
           {"label": label, "time_of_week": label, "activity_kind": "authored_activity"}, *sources, role="synthesis")
    _left(conn)


# ----------------------------------------------------------------------------- a source list that is not the whole story

EVENT = {"event": "hired", "org": "Meridian"}


def _event(conn, source, occurrence, **kw) -> str:
    return _plant(conn, CAREER, "work.career_event", EVENT, source, occurrence=occurrence, **kw)


def _refs(conn, object_id) -> List[str]:
    return [r["record_id"] for r in json.loads(_facts(conn)[object_id][3])]


@pytest.mark.parametrize("ledger", [True, False], ids=["the_ledger_names_the_first_record", "the_ledger_is_gone"])
def test_an_event_first_read_elsewhere_and_told_again_in_a_reaction_is_left(node, ledger) -> None:
    """The writer merges an event told again under another date into the first fact and writes the retelling's
    record over the fact's own (`_episodic_retelling`, `_corroborate`). The fact then cites only the reaction.
    The ledger still names the journal entry; without the ledger nothing shows where the event was first read,
    and the fact is left all the same."""
    conn = node.conn
    conn.execute("INSERT INTO journal_entries (entry_id, entry_at, content, source_id, source_record_id)"
                 " VALUES ('j1', '2026-09-01T09:00:00Z', ?, 'grow_journal', 'r1')", (THEIRS,))
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    first = _event(conn, ("journal_entries", "j1"), "2026-09-01", ledger=ledger)
    again = _event(conn, (MESSAGES, "r-words"), "2026-09-20", outcome="retelling_merged", ledger=ledger)
    assert again == first and _refs(conn, first) == ["r-words"], "the source list now names only the reaction"
    _left(conn, other=1)


def test_an_event_told_in_two_reactions_is_ended_when_the_ledger_accounts_for_both(node) -> None:
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    _message(conn, "r-marker", OTHER_LANGUAGE, metadata=REACTION_METADATA)
    first = _event(conn, (MESSAGES, "r-words"), "2026-09-01", ledger=True)
    _event(conn, (MESSAGES, "r-marker"), "2026-09-20", outcome="retelling_merged", ledger=True)
    assert _refs(conn, first) == ["r-marker"]
    assert retract.count(conn) == {**NONE, "packs": [{"pack_id": CAREER, "facts": 1}], "total": 1}
    assert retract.retract(conn, expected_total=1)["retracted"] == 1 and _standing(conn) == set()


@pytest.mark.parametrize("stored_from", [
    pytest.param(("conversation_messages", "own"), id="a_message_the_owner_typed"),
    pytest.param(("conversation_messages", "theirs"), id="a_contacts_reaction"),
    pytest.param(("conversation_messages", "gone"), id="a_message_no_longer_stored"),
    pytest.param(("journal_entries", "j1"), id="a_journal_entry"),
    pytest.param((None, None), id="a_row_that_names_no_record"),
])
def test_a_fact_the_ledger_says_was_also_stored_from_another_record_is_left(node, stored_from) -> None:
    """A record can change a fact's fields without joining its source list (`_update_fields`). The ledger row
    it leaves is the only trace, and it is enough to leave the fact."""
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    _message(conn, "own", THEIRS, metadata=ORDINARY_METADATA)
    _message(conn, "theirs", f"Loved “{THEIRS}”", is_self=False, metadata=REACTION_METADATA)
    fact_id = _employer(conn, "Meridian", (MESSAGES, "r-words"), ledger=True)
    assert retract.count(conn)["total"] == 1, "stored from the reaction alone: counted"
    _ledger(conn, fact_id, stored_from, predicate="work.employer")
    _left(conn, other=1)


def test_a_state_read_again_from_a_reaction_is_still_ended(node) -> None:
    """Only an event told again can lose its source list. A state the writer read a second time keeps the
    list it had (the new record is added), so its list is the whole story with or without a ledger."""
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    fact_id = _employer(conn, "Meridian", (MESSAGES, "r-words"))
    conn.execute("UPDATE signal_objects SET payload_json=json_set(payload_json, '$.extractions',"
                 " json('[{\"model\": \"stub-9b\", \"agrees\": true}]')) WHERE object_id=?", (fact_id,))
    conn.commit()
    assert [f.object_id for f in retract.find(conn).facts] == [fact_id]


# ----------------------------------------------------------------------------- a later backfill


def _standing_career_facts(conn) -> List[tuple]:
    return conn.execute("SELECT object_id, source_refs_json FROM signal_objects WHERE object_type='fact'"
                        " AND ontology_id=? AND valid_to IS NULL", (CAREER,)).fetchall()


@pytest.mark.parametrize("content, metadata", [
    pytest.param(f"Loved “{THEIRS}”", None, id="reaction_by_its_words"),
    pytest.param(OTHER_LANGUAGE, REACTION_METADATA, id="reaction_by_its_marker"),
])
def test_a_later_backfill_does_not_bring_an_ended_fact_back(node, monkeypatch, content, metadata) -> None:
    """The fact is written the way it was before the packs skipped reactions (their test switched off), ended, and
    then every mark that the record was read is forgotten, as after a pack's version changes. The backfill that
    follows walks the history with the same model that wrote the fact and stores nothing."""
    from topos.features.derivation.surfaces import run_pack_backfill
    conn = node.conn
    _message(conn, "imessage:r1", content, metadata=metadata)
    calls: List[str] = []
    _stub_models(monkeypatch, calls)
    with monkeypatch.context() as before_the_fix:
        before_the_fix.setattr(dj, "_owner_reaction", lambda *a, **kw: False)
        first = run_pack_backfill(conn, CAREER, limit=5, use_prefilter=False)
    assert first["written"] == 1 and len(calls) == 1
    ((fact_id, refs),) = _standing_career_facts(conn)
    assert [r["record_id"] for r in json.loads(refs)] == ["imessage:r1"]

    assert retract.count(conn) == {**NONE, "packs": [{"pack_id": CAREER, "facts": 1}], "total": 1}
    assert retract.retract(conn, expected_total=1)["retracted"] == 1
    assert _standing_career_facts(conn) == []

    conn.execute("DELETE FROM derivation_progress WHERE key LIKE ?", (f"{CAREER}@%",))
    conn.commit()
    calls.clear()
    again = run_pack_backfill(conn, CAREER, limit=5, use_prefilter=False)
    assert again["processed"] == 0 and again["written"] == 0 and calls == [], "the reaction is not read again"
    assert _standing_career_facts(conn) == []
    rows = conn.execute("SELECT object_id, valid_to FROM signal_objects WHERE object_type='fact' AND ontology_id=?",
                        (CAREER,)).fetchall()
    assert [r[0] for r in rows] == [fact_id] and rows[0][1] is not None, "one row, still closed"


def test_the_same_value_from_the_owners_own_message_can_be_stored_again(node, monkeypatch) -> None:
    """Ending the fact writes no tombstone: what was wrong was the source, not necessarily the value."""
    from topos.features.derivation.surfaces import run_pack_backfill
    conn = node.conn
    _message(conn, "imessage:r1", f"Loved “{THEIRS}”")
    calls: List[str] = []
    _stub_models(monkeypatch, calls)
    with monkeypatch.context() as before_the_fix:
        before_the_fix.setattr(dj, "_owner_reaction", lambda *a, **kw: False)
        run_pack_backfill(conn, CAREER, limit=5, use_prefilter=False)
    assert retract.retract(conn, expected_total=1)["retracted"] == 1
    _message(conn, "imessage:own", THEIRS, metadata=ORDINARY_METADATA, event_at="2026-09-21T10:00:00+00:00")
    out = run_pack_backfill(conn, CAREER, limit=5, use_prefilter=False)
    assert out["written"] == 1
    ((_fact_id, refs),) = _standing_career_facts(conn)
    assert [r["record_id"] for r in json.loads(refs)] == ["imessage:own"]
    assert retract.count(conn)["total"] == 0


# ----------------------------------------------------------------------------- the two routes


def _mounted(node, monkeypatch) -> FastAPI:
    """The signal router as the node mounts it, over the scratch node."""
    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "1")
    for name in ("TOPOS_OWNER_DOOR_TEAM_IDS", "TOPOS_UDS_TEAM_IDS", "TOPOS_DEV_PROXY_ALLOWED"):
        monkeypatch.delenv(name, raising=False)
    from topos.api import signal

    monkeypatch.setattr(signal, "_entities_conn", lambda: node.conn)
    app = FastAPI()
    app.include_router(signal.router, prefix="/v1")
    return app


@pytest.fixture
def door(node, tmp_path, monkeypatch):
    """The mounted routes behind the owner socket's wrapper, in process."""
    monkeypatch.setenv("TOPOS_UDS_PATH", str(tmp_path / "engine.sock"))
    app = _mounted(node, monkeypatch)
    return SimpleNamespace(app=app, socket=lambda: TestClient(uds.UDSChannelApp(app)))


def _yes() -> Dict[str, str]:
    return {"X-Topos-Owner-Confirm": uds.write_owner_confirmation()}


def _no_private_text(text: str) -> None:
    for private in PRIVATE:
        assert private not in text, "a fact's value or a message's text was answered"


def test_ending_is_in_the_census_as_the_owners_and_counting_is_a_read() -> None:
    assert census.lookup("POST", RETRACT).verdict == census.DANGEROUS
    assert census.lookup("GET", COUNT).verdict == census.READ
    for method, path in (("POST", RETRACT), ("GET", COUNT)):
        assert not census.owner_agent_route_allowed(method, path), "a coding agent's label does not reach it"
        assert not census.app_agent_route_allowed(method, path), "nor does an app's"


def test_counting_needs_no_yes_and_ending_needs_one(door, node, stored) -> None:
    before = _facts(node.conn)
    with door.socket() as client:
        read = client.get(COUNT)
        assert read.status_code == 200 and read.json() == EXPECTED
        _no_private_text(read.text)
        refused = client.post(RETRACT, json={"expected_total": 3})
        wrong = client.post(RETRACT, json={"expected_total": 3}, headers={"X-Topos-Owner-Confirm": "a" * 64})
        for response in (refused, wrong):
            assert response.status_code == 403 and response.json() == REFUSAL
        assert _facts(node.conn) == before, "nothing ends without the yes"

        header = _yes()
        done = client.post(RETRACT, json={"expected_total": 3}, headers=header)
        assert done.status_code == 200, done.text
        assert done.json() == {**EXPECTED, "retracted": 3, "changed": 0}
        _no_private_text(done.text)
        assert not uds.owner_confirmation_path().exists(), "the confirmation is consumed by the change"
        again = client.post(RETRACT, json={"expected_total": 0}, headers=header)
        assert again.status_code == 403 and again.json() == REFUSAL, "one yes, one change"
        assert client.get(COUNT).json() == {**NONE, "kept_confirmed": 1}
    standing = {oid for oid, row in _facts(node.conn).items() if row[1] is None}
    assert standing == set(stored.left.values()) - {stored.left["already_closed"]}


def test_a_body_that_is_not_the_count_shown_is_refused_and_ends_nothing(door, node, stored) -> None:
    before = _facts(node.conn)
    with door.socket() as client:
        for body in ({}, {"expected_total": "3"}, {"expected_total": True}, {"expected_total": -1},
                     {"expected_total": 3, "pack_ids": [CAREER]}, {"total": 3}):
            response = client.post(RETRACT, json=body, headers=_yes())
            assert response.status_code == 400 and response.json()["error"] == "reaction_facts_invalid", body
        stale = client.post(RETRACT, json={"expected_total": 2}, headers=_yes())
        assert stale.status_code == 409 and stale.json()["error"] == "reaction_facts_changed"
        _no_private_text(stale.text)
    assert _facts(node.conn) == before


def test_ending_is_refused_over_tcp_and_with_the_socket_gate_switched_off(door, node, stored, monkeypatch) -> None:
    from topos.auth import require_api_key, resolve_request_principal
    from topos.principal import OWNER_APP, Principal

    before = _facts(node.conn)
    # Over TCP the socket gate never runs; the owner's key alone must not end a fact.
    door.app.dependency_overrides[require_api_key] = lambda: "key"
    door.app.dependency_overrides[resolve_request_principal] = lambda: Principal(OWNER_APP, "local_http", acting_user="o")
    try:
        with TestClient(door.app) as tcp:
            _yes()                                   # a live token on disk changes nothing for a TCP caller
            token = json.loads(uds.owner_confirmation_path().read_text())["token"]
            response = tcp.post(RETRACT, json={"expected_total": 3}, headers={"X-Topos-Owner-Confirm": token})
            assert response.status_code == 403 and response.json() == REFUSAL
            assert tcp.get(COUNT).json() == EXPECTED
    finally:
        door.app.dependency_overrides.clear()
    uds.clear_owner_confirmation()

    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "0")
    with door.socket() as client:
        bare = client.post(RETRACT, json={"expected_total": 3})
        assert bare.status_code == 403 and bare.json() == REFUSAL, "the route asks for the yes itself"
        assert _facts(node.conn) == before
        confirmed = client.post(RETRACT, json={"expected_total": 3}, headers=_yes())
        assert confirmed.status_code == 200 and confirmed.json()["retracted"] == 3


# ----------------------------------------------------------------------------- the command, over a real socket


def test_the_command_end_to_end_over_a_real_owner_socket(node, stored, monkeypatch) -> None:
    """`topos-node packs retract-reactions` against the mounted routes behind the socket wrapper on a real unix
    socket: the real client, the real confirmation file, the real gate. Only the person (a patched terminal and
    prompt) is a stand-in."""
    import sys

    import click
    from click.testing import CliRunner

    from tests.topos.fake_node_socket import serve_fake_node
    from topos.cli import packs_cmd
    from topos.mcp_server import NodeRefused, NodeSocket

    app = _mounted(node, monkeypatch)
    uds.clear_owner_confirmation()
    prompts: List[str] = []
    answers = [False, True]
    terminal = SimpleNamespace(isatty=lambda: True)
    pipe = SimpleNamespace(isatty=lambda: False)
    monkeypatch.setattr(click, "confirm", lambda text, **kw: prompts.append(text) or answers.pop(0))
    before = _facts(node.conn)

    def run(*args: str):
        result = CliRunner().invoke(packs_cmd.packs_group, ["retract-reactions", *args], catch_exceptions=False)
        _no_private_text(result.output)
        return result

    with serve_fake_node(SimpleNamespace(app=uds.UDSChannelApp(app)), uds.socket_path()):
        # What a coding agent with a shell can do: call the route, count, or ask to end without a terminal.
        with pytest.raises(NodeRefused) as refused:
            NodeSocket().request_sync("POST", RETRACT, json_body={"expected_total": 3})
        assert (refused.value.status, refused.value.code) == (403, "owner_confirmation_required")
        monkeypatch.setattr(packs_cmd, "sys", SimpleNamespace(stdin=pipe, stdout=pipe, exit=sys.exit))
        counted = run()
        assert counted.exit_code == packs_cmd.EXIT_OK and "3 facts in 2 packs standing." in counted.output
        assert "Nothing changed." in counted.output
        piped = run("--apply")
        assert piped.exit_code == packs_cmd.EXIT_NEEDS_HUMAN and "your own terminal" in piped.output
        assert prompts == [] and not uds.owner_confirmation_path().exists() and _facts(node.conn) == before

        # The owner, on a terminal: a no, then a yes.
        monkeypatch.setattr(packs_cmd, "sys", SimpleNamespace(stdin=terminal, stdout=terminal, exit=sys.exit))
        no = run("--apply")
        assert no.exit_code == packs_cmd.EXIT_OK and "Nothing changed." in no.output
        assert not uds.owner_confirmation_path().exists() and _facts(node.conn) == before
        yes = run("--apply")
        assert yes.exit_code == packs_cmd.EXIT_OK, yes.output
        assert not uds.owner_confirmation_path().exists(), "the one confirmation was spent on the one change"
        after = run()

    assert prompts == ["End them?", "End them?"]
    assert f"  {JOY}      1" in yes.output and f"  {CAREER}      2" in yes.output
    assert "3 facts in 2 packs ended." in yes.output
    assert "1 more of the same kind is left as it is: you confirmed, corrected or added it in the app." in yes.output
    assert "stored" not in yes.output.split("ended.")[1], "no line about other records when there are none"
    assert "0 facts in 0 packs standing." in after.output and "Nothing to end." in after.output
    standing = {oid for oid, row in _facts(node.conn).items() if row[1] is None}
    assert standing == set(stored.left.values()) - {stored.left["already_closed"]}
