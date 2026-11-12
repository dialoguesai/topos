"""Facts a pack stored from a reaction the owner sent in Messages: counted for anyone, ended only on the owner's yes.

Skipping a reaction (`test_owner_reactions.py`) stops new facts of this kind. It leaves the ones already stored.
`features/derivation/reaction_retract.py` is the owner's way to end those, behind
`GET /v1/signal/derivation/reaction-facts` and `POST /v1/signal/derivation/reaction-facts/retract`.

protects: the count is a read and writes nothing. Ending needs the owner's confirmation on the owner socket (the
app, or a yes on a terminal) and is refused over TCP and without it. What is ended is exactly the standing pack
facts whose every source record is a reaction the owner sent, by the test the packs and the commitments lane
apply; a fact with any other source, a contact's reaction, a record that is gone, or the owner's own confirmation
stays. So does a fact any owner door made (confirm, correct, revise, promote, declare, override, an attribution
repaired in place, a promotion that landed on a fact already standing), a fact the owner marked in an app (a row
in `fact_feedback`), a fact that cites its records only for when they were sent (counted apart), and a fact the
derivation ledger shows, or cannot rule out, rests on a record that is not a reaction: one it was also stored from
(the writer can replace an event's source list when the event is told again), one that repeated it or read the
same instance and was stored nowhere (the writer's `noop`, a reading held for review), a store the ledger does not
hold at all, a row the lanes skip only because its metadata cannot be read, or a fact stored before a retention
floor deleted ledger rows. What is judged before the write gate is judged
again inside it. An ended fact keeps its row and its value (it is closed, the way a superseded fact is), no
message row changes, the write is made under the write gate and committed, a later backfill does not bring it
back, and a fact it had replaced is not put back. Nothing but pack ids and counts is answered. Every row here
is invented.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
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
#: A record this recent stores a fact the writer still treats as fresh: the same value read again from another
#: record within seven days, by the same model, is a `noop`.
RECENT = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
#: The outcomes after which the lanes put the fact's id on the ledger row (`run_pack_backfill`: `stored`).
STORED = ("written", "corroborated", "retelling_merged", "field_update", "corrected", "superseded")
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


def _plant(conn, pack: str, predicate: str, value: Any, *sources, model: str = "stub-9b",
           role: str = "authored", occurrence: str = None, outcome: str = "written", ledger: bool = True,
           event_date: str = "2026-09-20", confidence: float = 0.9) -> str:
    """One assertion read from these records, put to the packs' own writer the way the lanes put it, and the
    id of the fact it landed on. `ledger` adds the row the lanes write for an assertion that reached the writer:
    it carries the fact's id only when the outcome stored something."""
    loaded = load_packs(bundled_pack_dir())[pack]
    out = DerivationWriter(conn, model=model).assert_pack_fact(
        pack=loaded, predicate=predicate, subject_entity_id="ent_owner", value=value,
        actor_role=role, confidence=confidence, quote="Signed the offer", event_date=event_date, occurrence=occurrence,
        source_refs=[{"table": table, "record_id": rid} for table, rid in sources])
    assert out["outcome"] == outcome, out
    if ledger:
        _ledger(conn, out["object_id"] if outcome in STORED else None, *sources, pack=pack, predicate=predicate,
                value=value, occurrence=occurrence)
    return out["object_id"]


def _ledger(conn, object_id, *sources, pack: str = "work.career", predicate: str = "work.career_event",
            value: Any = None, status: str = "accepted", occurrence: str = None, stage: str = "backfill") -> None:
    """The ledger row of one judged assertion per record. `object_id` None: nothing was stored for it."""
    for table, rid in sources:
        dj._ledger_write(conn, stage=stage, pack=load_packs(bundled_pack_dir())[pack],
                         rec={"table": table, "record_id": rid, "role": "authored"},
                         a={"predicate": predicate, "value": {} if value is None else value,
                            "verifier_status": status, "occurrence": occurrence},
                         model="stub-9b", vmodel="stub-9b", context=None, written_id=object_id)
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
        "two_reactions": _employer(conn, "Northwind", words),
        "another_pack": _plant(conn, JOY, "moment.gratitude", {"person": "a friend", "what": "the lift home"}, marker),
    }
    # Read a second time in the other reaction, as a lane reads it: one assertion per record, merged in.
    assert _employer(conn, "Northwind", marker, outcome="corroborated") == ended["two_reactions"]
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
            "kept_marked": 0, "kept_other_record": 0, "kept_timing": 0}
NONE = {"packs": [], "total": 0, "kept_confirmed": 0, "kept_marked": 0, "kept_other_record": 0, "kept_timing": 0}


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


def _change_after_read(monkeypatch, nth: int, change) -> None:
    """`retract` reads twice: before it asks for the write gate, and again inside it. Run `change` right after
    the `nth` of those reads."""
    real_find, reads = retract.find, []

    def find_then_change(conn, **kw):
        found = real_find(conn, **kw)
        reads.append(1)
        if len(reads) == nth:
            change(conn)
            conn.commit()
        return found

    monkeypatch.setattr(retract, "find", find_then_change)


@pytest.mark.parametrize("nth", [1, 2], ids=["while_waiting_for_the_gate", "after_the_read_inside_the_gate"])
def test_a_fact_that_changed_after_it_was_judged_is_left(node, stored, monkeypatch, nth) -> None:
    """Between a read and the write a fact can gain a second source or the owner's verdict. Whichever read it
    follows, the row is ended only as it was judged."""
    refs = json.dumps([{"table": MESSAGES, "record_id": "r-words"}, {"table": MESSAGES, "record_id": "own"}])
    _change_after_read(monkeypatch, nth, lambda conn: conn.execute(
        "UPDATE signal_objects SET source_refs_json=? WHERE object_id=?", (refs, stored.ended["one_reaction"])))
    out = retract.retract(node.conn, expected_total=3)
    assert out["retracted"] == 2 and out["changed"] == 1
    assert _facts(node.conn)[stored.ended["one_reaction"]][1] is None, "it now has a source that is not a reaction"


def _a_typed_repeat(conn, fact_id: str) -> None:
    _ledger(conn, None, (MESSAGES, "own"), predicate="work.employer", value={"org": "Meridian"})


def _a_mark_in_an_app(conn, fact_id: str) -> None:
    conn.execute("CREATE TABLE fact_feedback (fact_id TEXT PRIMARY KEY, reaction TEXT)")
    conn.execute("INSERT INTO fact_feedback VALUES (?, 'love')", (fact_id,))


def _a_reading_held_for_review(conn, fact_id: str) -> None:
    conn.execute("INSERT INTO fact_conflicts (conflict_id, subject_entity_id, predicate, incumbent_object_id,"
                 " challenger_value, challenger_confidence) VALUES ('cfl_1', 'ent_owner', 'work.employer', ?, '{}', 0.5)",
                 (fact_id,))


@pytest.mark.parametrize("arrives", [_a_typed_repeat, _a_mark_in_an_app, _a_reading_held_for_review])
def test_what_arrives_while_the_yes_waits_for_the_write_gate_keeps_the_fact(node, stored, monkeypatch, arrives) -> None:
    """Neither a repeat that was stored nowhere nor a mark made in an app changes the fact's own row, so the
    row's guard cannot see them, and the wait for the gate can be long. The facts are judged again inside it."""
    fact_id = stored.ended["one_reaction"]
    _change_after_read(monkeypatch, 1, lambda conn: arrives(conn, fact_id))
    out = retract.retract(node.conn, expected_total=3)
    assert out["retracted"] == 2 and out["changed"] == 1
    assert out["kept_marked"] + out["kept_other_record"] == 1, "and the answer says why it stands"
    assert _facts(node.conn)[fact_id][1] is None


# ----------------------------------------------------------------------------- what the owner made


def _standing(conn) -> set:
    return {oid for oid, row in _facts(conn).items() if row[1] is None}


def _left(conn, *, confirmed: int = 0, marked: int = 0, other: int = 0, timing: int = 0) -> None:
    """Nothing is listed, the count says why, and a yes ends nothing."""
    before = _facts(conn)
    assert retract.find(conn).facts == []
    assert retract.count(conn) == {**NONE, "kept_confirmed": confirmed, "kept_marked": marked,
                                   "kept_other_record": other, "kept_timing": timing}
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


def test_a_fact_whose_attribution_the_owner_repaired_is_left(node) -> None:
    """`verdicts.edit_fact` with `asserted_by` alone rewrites the attribution in place. It writes none of the
    other marks: the row is still the pack's, by the pack's model, unconfirmed and uncorrected."""
    from topos.features.facts.verdicts import edit_fact
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    fact_id = _employer(conn, "Meridian", (MESSAGES, "r-words"))
    assert [f.object_id for f in retract.find(conn).facts] == [fact_id]
    edited = edit_fact(conn, fact_id, asserted_by="assistant")
    assert edited["object_id"] == fact_id, "repaired in place"
    row = conn.execute("SELECT created_by, extractor_version, payload_json FROM signal_objects WHERE object_id=?",
                       (fact_id,)).fetchone()
    payload = json.loads(row[2])
    assert row[0] == "system" and row[1].startswith("derivation:") and payload["extractor"]["model"] == "stub-9b"
    assert "verified_by_owner" not in payload and "corrected_from" not in payload
    assert payload["asserted_by"] == "assistant" and payload["actor_role"] == "authored"
    _left(conn, confirmed=1)
    assert _standing(conn) == {fact_id}


@pytest.mark.parametrize("pack, predicate, role", [
    pytest.param(CAREER, "work.industry", "authored", id="authored"),
    pytest.param(CAREER, "work.industry", "addressed", id="addressed"),
    pytest.param("interests.taste", "interest.interest", "observed", id="observed"),
])
def test_the_attribution_the_writer_stamps_is_not_read_as_the_owners_repair(node, pack, predicate, role) -> None:
    """Whatever role a pack reads a record under, the attribution the writer derives from it is the writer's."""
    conn = node.conn
    _message(conn, "theirs", f"Loved “{THEIRS}”", is_self=False, metadata=REACTION_METADATA)
    fact_id = _plant(conn, pack, predicate, "sailing", (MESSAGES, "theirs"), role=role)
    created_by, version, payload_json = conn.execute(
        "SELECT created_by, extractor_version, payload_json FROM signal_objects WHERE object_id=?",
        (fact_id,)).fetchone()
    assert retract._owner_made(created_by, version, json.loads(payload_json)) is False
    repaired = {**json.loads(payload_json), "asserted_by": "contact:c1"}
    assert retract._owner_made(created_by, version, repaired) is True


def test_a_promotion_that_landed_on_a_fact_already_standing_is_left(node) -> None:
    """The owner promotes a held assertion whose value a pack already stored: the writer merges it into the
    standing fact (`_corroborate`), which keeps the pack's extractor. The reading it appends is the only mark
    on the row that the owner said so."""
    from topos.features.derivation.surfaces import promote_conflict
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    _message(conn, "r-marker", OTHER_LANGUAGE, metadata=REACTION_METADATA)
    fact_id = _employer(conn, "Meridian", (MESSAGES, "r-words"))
    DerivationWriter(conn, model="stub-9b")._quarantine(
        "ent_owner", "work.employer", {"org": "Meridian"}, 0.9, reason="held_for_review",
        pack=load_packs(bundled_pack_dir())[CAREER], source_refs=[{"table": MESSAGES, "record_id": "r-marker"}],
        quote="Signed the offer", about="owner")
    conn.commit()
    (held,) = conn.execute("SELECT conflict_id FROM fact_conflicts").fetchone()
    promoted = promote_conflict(conn, held, to_owner=True)
    assert promoted["outcome"] == "corroborated" and promoted["object_id"] == fact_id, promoted
    payload = json.loads(_facts(conn)[fact_id][2])
    assert sorted(_refs(conn, fact_id)) == ["r-marker", "r-words"], "every record it names is a reaction"
    assert payload["extractor"]["model"] == "stub-9b" and "verified_by_owner" not in payload
    assert [e["model"] for e in payload["extractions"]] == ["owner-promote"]
    _left(conn, confirmed=1)


# ----------------------------------------------------------------------------- what the owner marked in an app

#: The table another change in the same release makes on its first write (`query/fact_feedback.py`, not in this
#: build): the owner's own reaction and rank on a pack fact, one row per fact.
FEEDBACK_TABLE = ("CREATE TABLE fact_feedback (fact_id TEXT PRIMARY KEY, reaction TEXT, rank INTEGER,"
                  " updated_at TEXT NOT NULL, updated_by TEXT NOT NULL)")


def _mark(conn, fact_id: str, reaction=None, rank=None) -> None:
    conn.execute("INSERT INTO fact_feedback VALUES (?, ?, ?, '2026-11-11T10:00:00+00:00', 'an-app')",
                 (fact_id, reaction, rank))
    conn.commit()


def test_a_table_of_marks_with_none_on_these_facts_changes_nothing(node, stored) -> None:
    node.conn.execute(FEEDBACK_TABLE)
    _mark(node.conn, "some-other-fact", reaction="like")
    _mark(node.conn, stored.left["journal_entry"], rank=2)       # a mark on a fact that was never one of these
    assert retract.count(node.conn) == EXPECTED


@pytest.mark.parametrize("reaction, rank", [("love", None), (None, -2), ("sad", 3), (None, None)],
                         ids=["a_reaction", "a_rank", "both", "a_row_that_holds_neither"])
def test_a_fact_the_owner_marked_in_an_app_is_left_and_counted_apart(node, stored, reaction, rank) -> None:
    conn = node.conn
    conn.execute(FEEDBACK_TABLE)
    marked = stored.ended["one_reaction"]
    _mark(conn, marked, reaction, rank)
    assert retract.count(conn) == {**EXPECTED, "packs": [{"pack_id": JOY, "facts": 1}, {"pack_id": CAREER, "facts": 1}],
                                   "total": 2, "kept_marked": 1}
    assert marked not in {f.object_id for f in retract.find(conn).facts}
    out = retract.retract(conn, expected_total=2)
    assert out["retracted"] == 2 and out["kept_marked"] == 1 and out["changed"] == 0
    assert _facts(conn)[marked][1] is None, "the marked fact stands"
    assert conn.execute("SELECT fact_id, reaction, rank FROM fact_feedback WHERE fact_id=?",
                        (marked,)).fetchall() == [(marked, reaction, rank)], "and its mark is as it was"


def test_marks_that_cannot_be_read_keep_every_fact(node, stored) -> None:
    """A table of that name in a shape this does not know: nothing shows a fact is unmarked."""
    node.conn.execute("CREATE TABLE fact_feedback (id TEXT, reaction TEXT)")
    node.conn.commit()
    _left(node.conn, confirmed=1, marked=3)


def test_a_fact_that_cites_records_for_their_timing_is_left_and_counted_apart(node) -> None:
    """The rhythm kernel (`synthesize.py`) cites a sample of the records the owner sent for WHEN they were sent.
    A reaction was sent by the owner at that time, whatever it quotes. It is left, and the count says so."""
    conn = node.conn
    sources = []
    for n in range(3):
        _message(conn, f"r{n}", OTHER_LANGUAGE, metadata=REACTION_METADATA, event_at=f"2026-09-0{n + 1}T07:00:00+00:00")
        sources.append((MESSAGES, f"r{n}"))
    label = "Mon 06:00-08:00"
    _plant(conn, "behavior.habits", "behavior.routine_block",
           {"label": label, "time_of_week": label, "activity_kind": "authored_activity"}, *sources, role="synthesis")
    _left(conn, timing=1)


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


def test_a_state_read_again_from_a_reaction_is_ended_when_the_ledger_holds_both_readings(node) -> None:
    """A state the writer read a second time keeps the list it had (the new record is added). It is ended when
    the ledger holds a store for the first write and one for the merge, and left when it holds fewer than the
    fact's own row records: a store the ledger does not hold may have been from any record."""
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    _message(conn, "r-marker", OTHER_LANGUAGE, metadata=REACTION_METADATA)
    fact_id = _employer(conn, "Meridian", (MESSAGES, "r-words"))
    again = _employer(conn, "Meridian", (MESSAGES, "r-marker"), outcome="corroborated", ledger=False)
    assert again == fact_id and sorted(_refs(conn, fact_id)) == ["r-marker", "r-words"]
    assert len(json.loads(_facts(conn)[fact_id][2])["extractions"]) == 1
    _left(conn, other=1)
    _ledger(conn, fact_id, (MESSAGES, "r-marker"), predicate="work.employer", value={"org": "Meridian"})
    assert [f.object_id for f in retract.find(conn).facts] == [fact_id]


@pytest.mark.parametrize("lost", ["the_fact_has_no_row_in_it", "there_is_no_ledger"])
def test_a_fact_the_ledger_holds_no_store_for_is_left(node, lost) -> None:
    """Every lane that stores a pack fact from a record writes the ledger. A fact with no store there was made
    some other way, or the ledger was emptied: nothing shows a typed message did not repeat it or change it."""
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    _employer(conn, "Meridian", (MESSAGES, "r-words"), ledger=False)
    if lost == "there_is_no_ledger":
        conn.execute("DROP TABLE derivation_training_ledger")
        conn.commit()
    _left(conn, other=1)


# ----------------------------------------------------------------------------- a repeat that was stored nowhere


def test_a_fact_then_read_in_a_message_the_owner_typed_is_left(node, monkeypatch) -> None:
    """The lanes' own path. A reaction stores the fact. A message the owner typed then says the same thing
    while the fact is fresh: the writer answers `noop`, adds no source and stores nothing, and the lane writes
    the ledger row with no fact id. The fact names the reaction alone and the owner typed it all the same."""
    from topos.features.derivation.surfaces import run_pack_backfill
    conn = node.conn
    _message(conn, "imessage:r1", f"Loved “{THEIRS}”", event_at=RECENT)
    calls: List[str] = []
    _stub_models(monkeypatch, calls)
    with monkeypatch.context() as before_the_fix:
        before_the_fix.setattr(dj, "_owner_reaction", lambda *a, **kw: False)
        assert run_pack_backfill(conn, CAREER, limit=5, use_prefilter=False)["written"] == 1
    ((fact_id, _refs_json),) = _standing_career_facts(conn)
    assert retract.count(conn)["total"] == 1, "read from the reaction alone so far: counted"

    _message(conn, "imessage:own", THEIRS, metadata=ORDINARY_METADATA, event_at=RECENT)
    typed = run_pack_backfill(conn, CAREER, limit=5, use_prefilter=False)
    assert typed["processed"] == 1 and typed["accepted"] == 1 and typed["written"] == 0, "read, agreed, a repeat"
    assert _refs(conn, fact_id) == ["imessage:r1"], "the source list still names the reaction alone"
    assert conn.execute("SELECT record_id, written_object_id FROM derivation_training_ledger ORDER BY rowid"
                        ).fetchall() == [("imessage:r1", fact_id), ("imessage:own", None)]
    _left(conn, other=1)
    assert _standing(conn) == {fact_id}


def _fresh_employer(conn, org: str, source, **kw) -> str:
    return _employer(conn, org, source, event_date=RECENT[:10], **kw)


@pytest.mark.parametrize("repeat_in, value, status, left", [
    pytest.param("own", {"org": "Meridian"}, "accepted", True, id="a_typed_message_says_the_same"),
    pytest.param("own", {"org": "Meridian"}, "skipped", True, id="the_same_with_the_fact_check_off"),
    pytest.param("theirs", {"org": "Meridian"}, "accepted", True, id="a_contacts_reaction_says_the_same"),
    pytest.param("gone", {"org": "Meridian"}, "accepted", True, id="a_message_no_longer_stored_said_the_same"),
    pytest.param("own", "not json", "accepted", True, id="a_typed_message_and_a_value_that_cannot_be_read"),
    pytest.param("r-marker", {"org": "Meridian"}, "accepted", False, id="another_reaction_says_the_same"),
    pytest.param("own", {"org": "Northwind"}, "accepted", False, id="a_typed_message_says_something_else"),
    pytest.param("own", {"org": "Meridian"}, "rejected", False, id="the_typed_reading_was_rejected"),
])
def test_only_a_repeat_of_the_fact_from_another_record_keeps_it(node, repeat_in, value, status, left) -> None:
    """The ledger row of a reading that reached the writer and was stored nowhere keeps the fact when it is the
    fact's value from a record that is not the owner's reaction. Another reaction, another value and a reading
    the verifier rejected (it never reached the writer) do not: the command would otherwise end nothing."""
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    _message(conn, "r-marker", OTHER_LANGUAGE, metadata=REACTION_METADATA)
    _message(conn, "own", THEIRS, metadata=ORDINARY_METADATA)
    _message(conn, "theirs", f"Loved “{THEIRS}”", is_self=False, metadata=REACTION_METADATA)
    fact_id = _fresh_employer(conn, "Meridian", (MESSAGES, "r-words"))
    _ledger(conn, None, (MESSAGES, repeat_in), predicate="work.employer", value=value, status=status)
    if value == "not json":
        conn.execute("UPDATE derivation_training_ledger SET value_json='{' WHERE written_object_id IS NULL")
        conn.commit()
    if left:
        _left(conn, other=1)
    else:
        assert [f.object_id for f in retract.find(conn).facts] == [fact_id]


def test_the_writers_own_repeat_is_the_row_that_is_read(node) -> None:
    """The same, with the writer deciding: a second reading of a fresh fact is its `noop`, and the row the
    lanes write for it has no fact id."""
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    _message(conn, "own", THEIRS, metadata=ORDINARY_METADATA)
    fact_id = _fresh_employer(conn, "Meridian", (MESSAGES, "r-words"))
    assert _fresh_employer(conn, "Meridian", (MESSAGES, "own"), outcome="noop") == fact_id
    assert _refs(conn, fact_id) == ["r-words"]
    _left(conn, other=1)


@pytest.mark.parametrize("typed_repeat", [True, False], ids=["a_typed_message_repeated_it_first", "reactions_only"])
def test_a_repeat_judged_before_a_reaction_changed_the_facts_fields_still_keeps_it(node, typed_repeat) -> None:
    """A typed message repeats the fact (`noop`), then another reaction changes one of its fields
    (`_update_fields`): the value the repeat was compared with is no longer the fact's. The ledger shows the
    change as a store beyond the first write, and then any reading of the predicate that was not stored counts."""
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    _message(conn, "r-marker", OTHER_LANGUAGE, metadata=REACTION_METADATA)
    _message(conn, "own", THEIRS, metadata=ORDINARY_METADATA)
    fact_id = _fresh_employer(conn, "Meridian", (MESSAGES, "r-words"))
    if typed_repeat:
        assert _fresh_employer(conn, "Meridian", (MESSAGES, "own"), outcome="noop") == fact_id
    changed = _plant(conn, CAREER, "work.employer", {"org": "Meridian", "since": "March"}, (MESSAGES, "r-marker"),
                     outcome="field_update", event_date=RECENT[:10])
    assert changed == fact_id and _refs(conn, fact_id) == ["r-words"]
    assert json.loads(_facts(conn)[fact_id][2])["value_struct"] == {"org": "Meridian", "since": "March"}
    if typed_repeat:
        _left(conn, other=1)
    else:
        assert [f.object_id for f in retract.find(conn).facts] == [fact_id]


# ----------------------------------------------------------------------------- a reading held, or of the same instance


def test_a_typed_message_that_restates_the_fact_with_more_at_lower_confidence_keeps_it(node) -> None:
    """The writer holds a reading that disagrees with the standing fact of the same key at lower confidence
    (`conflict_queued`): no store, a ledger row with no fact id, a row in the review queue. A typed message that
    tells the same event with one more field is such a reading. The fact still names the reaction alone."""
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    _message(conn, "own", THEIRS, metadata=ORDINARY_METADATA)
    fact_id = _event(conn, (MESSAGES, "r-words"), "2026-09-01")
    assert retract.count(conn)["total"] == 1
    held = _plant(conn, CAREER, "work.career_event", {**EVENT, "date": "2026-09-01"}, (MESSAGES, "own"),
                  occurrence="2026-09-01", confidence=0.7, outcome="conflict_queued")
    assert held == fact_id and _refs(conn, fact_id) == ["r-words"]
    assert conn.execute("SELECT record_id, written_object_id FROM derivation_training_ledger ORDER BY rowid"
                        ).fetchall() == [("r-words", fact_id), ("own", None)]
    _left(conn, other=1)


@pytest.mark.parametrize("reading, occurrence, left", [
    pytest.param({**EVENT, "date": "2026-09-01"}, "2026-09-01", True, id="the_same_event_with_one_more_field"),
    pytest.param("not json", "2026-09-01", True, id="a_value_that_cannot_be_read"),
    pytest.param({"event": "hired", "org": "Northwind"}, "2026-09-01", False, id="another_event"),
    pytest.param({**EVENT, "date": "2026-09-01"}, "2026-03-01", False, id="the_same_kind_of_event_on_another_date"),
])
def test_a_reading_of_the_same_instance_that_was_stored_nowhere_keeps_it(node, reading, occurrence, left) -> None:
    """The trial lane reads a typed message for the same event, with one more field, and stores nothing: no
    review row, only the ledger's. For a predicate whose key names the instance, a reading whose key is the
    fact's keeps it, whatever else it says. Another instance does not."""
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    _message(conn, "own", THEIRS, metadata=ORDINARY_METADATA)
    fact_id = _event(conn, (MESSAGES, "r-words"), "2026-09-01")
    _ledger(conn, None, (MESSAGES, "own"), predicate="work.career_event", value=reading, occurrence=occurrence,
            stage="trial")
    if reading == "not json":
        conn.execute("UPDATE derivation_training_ledger SET value_json='{' WHERE written_object_id IS NULL")
        conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM fact_conflicts").fetchone() == (0,)
    if left:
        _left(conn, other=1)
    else:
        assert [f.object_id for f in retract.find(conn).facts] == [fact_id]


def test_a_reading_held_against_a_single_valued_fact_keeps_it_and_a_key_alone_does_not(node) -> None:
    """A single-valued predicate's key is the predicate itself, so the key says nothing about the reading:
    a typed reading of another value stored nowhere does not keep the fact. One the writer held for review
    against it does, pending or after the owner dismissed it: the queue does not say where it was read."""
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    _message(conn, "own", THEIRS, metadata=ORDINARY_METADATA)
    fact_id = _plant(conn, "life.events", "life.stage", "working", (MESSAGES, "r-words"))
    _ledger(conn, None, (MESSAGES, "own"), pack="life.events", predicate="life.stage", value="studying",
            stage="trial")
    assert [f.object_id for f in retract.find(conn).facts] == [fact_id], "a key alone does not keep it"
    held = _plant(conn, "life.events", "life.stage", "working full time", (MESSAGES, "own"), confidence=0.7,
                  outcome="conflict_queued")
    assert held == fact_id
    _left(conn, other=1)
    conn.execute("UPDATE fact_conflicts SET status='dismissed'")
    conn.commit()
    _left(conn, other=1)


# ----------------------------------------------------------------------------- what the ledger cannot show


@pytest.mark.parametrize("content, ended", [
    pytest.param(THEIRS, False, id="ordinary_words"),
    pytest.param(f"Loved “{THEIRS}”", True, id="the_reaction_text_form"),
])
def test_a_row_the_lanes_skip_on_unreadable_metadata_alone_does_not_end_its_fact(node, content, ended) -> None:
    """The lanes skip a row whose metadata cannot be read (it cannot show the row is ordinary). Ending needs a
    positive sign: a marker that can be read, or the text form. Unreadable metadata and ordinary words show
    neither, so the fact is left and counted."""
    conn = node.conn
    _message(conn, "unreadable", content)
    conn.execute("UPDATE conversation_messages SET metadata_json='{not json' WHERE message_id='unreadable'")
    conn.commit()
    judged = retract._owner_reactions(conn, ["unreadable"])["unreadable"]
    assert judged.by_role and judged.by_flag, "the lanes skip it"
    fact_id = _employer(conn, "Meridian", (MESSAGES, "unreadable"))
    if ended:
        assert [f.object_id for f in retract.find(conn).facts] == [fact_id]
    else:
        _left(conn, other=1)


def test_a_typed_repeat_a_retention_floor_deleted_still_keeps_the_fact(node) -> None:
    """A reaction stores the fact, a message the owner typed repeats it (`noop`), and a retention floor then
    removes that message: it deletes the message's ledger rows with it, so the ledger no longer shows the
    repeat. A fact stored before a floor was applied is left."""
    from topos.sources.retention import apply_retention
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”", event_at=RECENT)
    _message(conn, "own", THEIRS, metadata=ORDINARY_METADATA, event_at="2026-01-10T10:00:00+00:00")
    fact_id = _fresh_employer(conn, "Meridian", (MESSAGES, "r-words"))
    assert _fresh_employer(conn, "Meridian", (MESSAGES, "own"), outcome="noop") == fact_id
    _left(conn, other=1)
    report = apply_retention(conn, "imessage", "2026-06-01")
    assert report["rows_removed"] == 1 and report["tables"].get("derivation_training_ledger") == 1, report
    assert [r[0] for r in conn.execute("SELECT record_id FROM derivation_training_ledger")] == ["r-words"]
    assert _refs(conn, fact_id) == ["r-words"]
    _left(conn, other=1)


@pytest.mark.parametrize("state, applied_at, left", [
    pytest.param("done", "now", True, id="applied_after_the_fact_was_stored"),
    pytest.param("removing", None, True, id="still_removing"),
    pytest.param("done", "not a time", True, id="a_stamp_that_cannot_be_read"),
    pytest.param("done", "2026-01-01T00:00:00+00:00", False, id="applied_before_the_fact_was_stored"),
])
def test_a_retention_floor_leaves_the_facts_stored_before_it_was_applied(node, state, applied_at, left) -> None:
    from topos.sources.retention import set_retention_floor
    conn = node.conn
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    fact_id = _employer(conn, "Meridian", (MESSAGES, "r-words"))
    set_retention_floor(conn, "imessage", "2026-01-01T00:00:00+00:00")
    stamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat() if applied_at == "now" else applied_at
    conn.execute("UPDATE source_retention_floors SET state=?, applied_at=?", (state, stamp))
    conn.commit()
    if left:
        _left(conn, other=1)
    else:
        assert [f.object_id for f in retract.find(conn).facts] == [fact_id]


def test_a_repeat_judged_before_an_entity_merge_still_keeps_the_fact(node) -> None:
    """The writer binds the person a value names to an entity id (`_bind_entity_refs`), and the repeat's ledger
    row carries that id. An entity merge then rewrites the id in the fact's payload, not in the ledger. The
    comparison leaves the person ids out, so the repeat still matches the fact."""
    from topos.features.derivation.writer import _canon_value
    from topos.features.entities.resolver import _remap_derivation_corpus
    conn = node.conn
    for entity_id, name in (("ent_sam_a", "Sam Lee"), ("ent_sam_b", "Samuel Lee")):
        conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json)"
                     " VALUES (?, 'person', ?, ?, '[]')", (entity_id, name, name.lower()))
    _message(conn, "r-marker", OTHER_LANGUAGE, metadata=REACTION_METADATA)
    _message(conn, "own", THEIRS, metadata=ORDINARY_METADATA)
    gratitude = {"person": "Sam Lee", "what": "the lift home"}
    fact_id = _plant(conn, JOY, "moment.gratitude", dict(gratitude), (MESSAGES, "r-marker"), event_date=RECENT[:10])
    assert _plant(conn, JOY, "moment.gratitude", dict(gratitude), (MESSAGES, "own"), outcome="noop",
                  event_date=RECENT[:10]) == fact_id
    (value_json,) = conn.execute("SELECT value_json FROM derivation_training_ledger WHERE record_id='own'").fetchone()
    assert json.loads(value_json)["person_entity_id"] == "ent_sam_a", "the repeat's row carries the bound id"
    _left(conn, other=1)

    _remap_derivation_corpus(conn, keep_id="ent_sam_b", absorb_id="ent_sam_a")
    conn.commit()
    fact_value = json.loads(_facts(conn)[fact_id][2])["value_struct"]
    assert fact_value["person_entity_id"] == "ent_sam_b", "the merge rewrote the fact's id"
    assert _canon_value(json.loads(value_json)) != _canon_value(fact_value), "the writer's equality no longer holds"
    assert retract._same_value(value_json, retract._comparable(fact_value)), "the person ids are left out"
    _left(conn, other=1)


# ----------------------------------------------------------------------------- what ending does not put back


def test_a_fact_a_reaction_replaced_is_not_put_back(node) -> None:
    """A limit, pinned so it is not mistaken for a repair: the reaction's value superseded what the owner had
    typed. Ending the reaction's fact leaves the typed one closed, and nothing stands for the predicate."""
    conn = node.conn
    _message(conn, "own", THEIRS, metadata=ORDINARY_METADATA)
    _message(conn, "r-words", f"Loved “{THEIRS}”")
    typed = _plant(conn, "life.events", "life.stage", "studying", (MESSAGES, "own"))
    from_reaction = _plant(conn, "life.events", "life.stage", "working", (MESSAGES, "r-words"), outcome="superseded")
    assert from_reaction != typed and _standing(conn) == {from_reaction}
    assert retract.retract(conn, expected_total=1)["retracted"] == 1
    assert _standing(conn) == set()
    assert json.loads(_facts(conn)[typed][2])["closed_reason"] == "superseded", "as the writer left it"


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
    for asked in (no, yes):
        assert "Ending puts nothing back." in asked.output, "said before the question"
    assert "Ending puts nothing back." not in counted.output
    assert f"  {JOY}      1" in yes.output and f"  {CAREER}      2" in yes.output
    assert "3 facts in 2 packs ended." in yes.output
    assert "1 more of the same kind is left as it is: you confirmed, corrected or added it in the app." in yes.output
    assert "stored" not in yes.output.split("ended.")[1], "no line about other records when there are none"
    assert "0 facts in 0 packs standing." in after.output and "Nothing to end." in after.output
    standing = {oid for oid, row in _facts(node.conn).items() if row[1] is None}
    assert standing == set(stored.left.values()) - {stored.left["already_closed"]}
