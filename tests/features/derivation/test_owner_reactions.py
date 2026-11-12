"""A reaction the owner sent in Messages is not text the owner wrote.

Messages stores a reaction ("tapback") as a message from whoever reacted. Its text is the
message reacted to, in curly quotes after "Loved", "Liked" and the rest, so the words belong to
the other person. The row's sender flag still says the owner, and the derivation job gave it
the role `authored`: every owner-only pack read someone else's sentence as the owner's own.

These tests pin the repair on the three paths that read a message for a pack (the ingest batch,
the history walk the catch-up and the owner's backfill share, the enable-offer trial) and on the
commitments lane, which picks its own records. Every row here is invented.
"""
import json
import sqlite3

import pytest

from topos.enrichment.jobs.canonical import derivation_job as dj
from topos.enrichment.jobs.canonical.derivation_job import _iter_history, _row_to_record, run_derivation_batch
from topos.features.derivation.registry import bundled_pack_dir
from topos.features.facts.reactions import is_reaction, reads_as_reaction
from topos.storage.canonical.conversations_tables import ensure_all_tables
from topos.storage.db.migrations import apply_all_migrations

#: What the other person wrote. As an owner message it is a career fact (`work.career`).
THEIRS = "Signed the offer — starting as Staff Engineer at Meridian in March"

REACTIONS = [
    pytest.param(f"Liked “{THEIRS}”", id="liked"),
    pytest.param(f"Loved “{THEIRS}”", id="loved"),
    pytest.param(f"Disliked “{THEIRS}”", id="disliked"),
    pytest.param(f"Laughed at “{THEIRS}”", id="laughed_at"),
    pytest.param(f"Emphasized “{THEIRS}”", id="emphasized"),
    pytest.param(f"Questioned “{THEIRS}”", id="questioned"),
    pytest.param(f"Reacted 🎉 to “{THEIRS}”", id="emoji"),
    pytest.param(f"Reacted with a sticker to “{THEIRS}”", id="sticker"),
    pytest.param(f"Removed a heart from “{THEIRS}”", id="removed_heart"),
    pytest.param(f"Removed an exclamation from “{THEIRS}”", id="removed_exclamation"),
    pytest.param(f"Removed 🎉 from “{THEIRS}”", id="removed_emoji"),
    pytest.param(f"Loved “{THEIRS}\nMore on a second line of the same message”", id="two_lines"),
    pytest.param(f"+QLoved “{THEIRS}”", id="old_decode_prefix"),
]

#: Sentences the owner typed. Each opens like a reaction and none of them is one.
OWN_WORDS = [
    pytest.param("Loved the dinner last night, the soup most of all", id="loved_no_quote"),
    pytest.param("Liked how the second draft reads, send it over", id="liked_no_quote"),
    pytest.param("Laughed at the whole thing for a solid hour", id="laughed_no_quote"),
    pytest.param("Reacted badly to the news, to be honest with you", id="reacted_no_quote"),
    pytest.param("Removed the old shelf from the hallway this morning", id="removed_no_quote"),
    pytest.param(f"Loved “{THEIRS}” as a headline, but I would cut the month", id="goes_on_after_the_quote"),
]

#: A reaction as a reader in another language writes it: only the metadata can say what it is.
OTHER_LANGUAGE = f"A aimé « {THEIRS} »"
REACTION_METADATA = {"associated_message_guid": "p:0/00000000-0000-0000-0000-000000000000",
                     "associated_message_type": 2000, "message_guid": "g-1"}
ORDINARY_METADATA = {"associated_message_type": 0, "message_guid": "g-2"}


@pytest.fixture
def node_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "node.db")
    ensure_all_tables(conn)
    apply_all_migrations(conn)
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, is_self)"
                 " VALUES ('ent_owner','person','Owner','owner','[]',1)")
    conn.commit()
    return conn


def _message(conn, message_id, content, *, is_self=True, metadata=None, event_at="2026-09-20T10:00:00+00:00"):
    conn.execute(
        "INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, event_at, sender_type,"
        " sender_id, content, source_id, is_from_self, metadata_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (message_id, "thread-1", "dataset-1", event_at, "human", "self" if is_self else "+15550000000",
         content, "imessage", 1 if is_self else 0, json.dumps(metadata) if metadata is not None else None))
    conn.commit()


def _row(message_id, content, *, is_self=True, **extra):
    return {"content": content, "message_id": message_id, "_table": "conversation_messages", "actor_role": None,
            "is_from_self": 1 if is_self else 0, "sender_id": "self" if is_self else "+15550000000",
            "sender_type": "human", "event_at": "2026-09-20", "source_id": "imessage", **extra}


def _mixed(row):
    return "mixed"


def _stub_models(monkeypatch, calls):
    """A model that turns any record it is shown into a career fact, and a verifier that agrees."""
    from topos.engine.backends import ollama
    extract = json.dumps({"assertions": [{
        "predicate": "work.career_event", "value": {"event": "hired", "org": "Meridian"},
        "about": "owner", "occurrence_date": None, "confidence": 0.9, "quote": "Signed the offer"}]})
    verify = json.dumps({"supported": True, "about": "owner", "fields_ok": True, "reason": "stated"})

    def fake(self, model, prompt, **kw):
        if "Respond with exactly" in prompt:
            return {"text": "ok"}
        if "strict fact-checker" in prompt:
            return {"text": verify}
        calls.append(prompt)
        return {"text": extract}
    monkeypatch.setattr(ollama.OllamaAdapter, "_generate", fake)
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model", lambda s, c: "stub-9b")


def _ingest_only(monkeypatch):
    monkeypatch.setattr(dj, "_drip_catchup", lambda *a, **kw: None)
    monkeypatch.setattr(dj, "_self_gate", lambda *a, **kw: None)


def _career_facts(conn):
    return conn.execute("SELECT COUNT(*) FROM signal_objects WHERE object_type='fact'"
                        " AND ontology_id='work.career'").fetchone()[0]


# --- the words -------------------------------------------------------------------------------

@pytest.mark.parametrize("text", REACTIONS)
def test_each_reaction_form_is_recognised_from_its_words(text):
    assert reads_as_reaction(text)


@pytest.mark.parametrize("text", OWN_WORDS)
def test_a_sentence_that_only_opens_like_a_reaction_is_not_one(text):
    assert not reads_as_reaction(text)
    assert not is_reaction({}, text)
    assert not is_reaction({"metadata_json": json.dumps(ORDINARY_METADATA)}, text)


def test_the_native_marker_is_enough_whatever_the_words_say():
    assert not reads_as_reaction(OTHER_LANGUAGE)
    assert is_reaction({"metadata_json": json.dumps(REACTION_METADATA)}, OTHER_LANGUAGE)
    assert is_reaction({"_metadata": REACTION_METADATA}, OTHER_LANGUAGE)
    assert not is_reaction({"metadata_json": json.dumps(ORDINARY_METADATA)}, OTHER_LANGUAGE)


# --- the ingest batch ------------------------------------------------------------------------

@pytest.mark.parametrize("text", REACTIONS)
def test_an_owner_reaction_is_not_a_record(text):
    assert _row_to_record(_row("imessage:1", text), posture_for=_mixed) is None


@pytest.mark.parametrize("text", REACTIONS)
def test_an_owner_reaction_with_a_stored_authored_role_is_not_a_record(text):
    row = _row("imessage:1", text, actor_role="authored")
    assert _row_to_record(row, posture_for=_mixed) is None


@pytest.mark.parametrize("text", OWN_WORDS)
def test_the_owners_own_sentence_is_still_read_as_authored(text):
    rec = _row_to_record(_row("imessage:2", text), posture_for=_mixed)
    assert rec is not None and rec["role"] == "authored" and rec["text"] == text


@pytest.mark.parametrize("text", REACTIONS)
def test_a_contacts_reaction_is_read_as_before(text):
    rec = _row_to_record(_row("imessage:3", text, is_self=False), posture_for=_mixed)
    assert rec is not None and rec["role"] == "observed" and rec["text"] == text
    marked = _row("imessage:3", text, is_self=False, metadata_json=json.dumps(REACTION_METADATA))
    assert _row_to_record(marked, posture_for=_mixed)["role"] == "observed"


def test_a_journal_entry_shaped_like_a_reaction_is_the_owners():
    text = f"Loved “{THEIRS}”"
    rec = _row_to_record({"content": text, "entry_id": "j1", "_table": "journal_entries"}, posture_for=_mixed)
    assert rec is not None and rec["role"] == "authored"


@pytest.mark.parametrize("metadata, written", [
    pytest.param(REACTION_METADATA, 0, id="stored_as_a_reaction"),
    pytest.param(ORDINARY_METADATA, 1, id="stored_as_an_ordinary_message"),
])
def test_a_batch_row_without_metadata_is_judged_by_the_stored_row(node_db, monkeypatch, metadata, written):
    """The lanes that feed a batch hand on text and sender only. The marker is on the stored row."""
    _message(node_db, "imessage:4", OTHER_LANGUAGE, metadata=metadata)
    row = _row("imessage:4", OTHER_LANGUAGE)
    calls = []
    _stub_models(monkeypatch, calls)
    _ingest_only(monkeypatch)
    assert run_derivation_batch(node_db, [row], stats={}) == written
    assert bool(calls) is bool(written)
    assert "metadata_json" not in row, "the batch's own rows are other jobs' too"


def test_a_batch_row_carrying_the_marker_needs_no_stored_row(node_db, monkeypatch):
    row = _row("imessage:5", OTHER_LANGUAGE, metadata_json=json.dumps(REACTION_METADATA))
    calls = []
    _stub_models(monkeypatch, calls)
    _ingest_only(monkeypatch)
    assert run_derivation_batch(node_db, [row], stats={}) == 0
    assert not calls


# --- the history walk: the catch-up and the owner's backfill ----------------------------------

def test_the_history_walk_leaves_out_the_owners_reactions_only(node_db):
    _message(node_db, "words", f"Loved “{THEIRS}”")
    _message(node_db, "marker", OTHER_LANGUAGE, metadata=REACTION_METADATA)
    _message(node_db, "own", "Loved the dinner last night, the soup most of all", metadata=ORDINARY_METADATA)
    _message(node_db, "own-no-metadata", "Liked how the second draft reads, send it over")
    _message(node_db, "theirs", f"Loved “{THEIRS}”", is_self=False, metadata=REACTION_METADATA)
    roles = {r["record_id"]: r["role"] for r in _iter_history(node_db, posture_for=_mixed)}
    assert roles == {"own": "authored", "own-no-metadata": "authored", "theirs": "observed"}


def test_the_history_walk_reads_a_table_that_has_no_metadata_column():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE conversation_messages (message_id TEXT PRIMARY KEY, conversation_id TEXT,"
                 " content TEXT, event_at TEXT, actor_role TEXT, is_from_self INTEGER, sender_id TEXT,"
                 " sender_type TEXT, source_id TEXT, dataset_id TEXT)")
    for mid, text in (("words", f"Loved “{THEIRS}”"), ("own", "Loved the dinner last night, the soup most of all")):
        conn.execute("INSERT INTO conversation_messages VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (mid, "c1", text, "2026-09-20T10:00:00+00:00", None, 1, "self", "human", "imessage", "d"))
    roles = {r["record_id"]: r["role"] for r in _iter_history(conn, posture_for=_mixed)}
    assert roles == {"own": "authored"}


@pytest.mark.parametrize("content, metadata, facts", [
    pytest.param(f"Loved “{THEIRS}”", None, 0, id="reaction_by_its_words"),
    pytest.param(OTHER_LANGUAGE, REACTION_METADATA, 0, id="reaction_by_its_marker"),
    pytest.param(THEIRS, ORDINARY_METADATA, 1, id="the_owners_own_message"),
])
def test_the_catch_up_never_shows_a_pack_the_owners_reaction(node_db, monkeypatch, content, metadata, facts):
    _message(node_db, "imessage:6", content, metadata=metadata)
    calls = []
    _stub_models(monkeypatch, calls)
    run_derivation_batch(node_db, [], stats={})
    assert _career_facts(node_db) == facts
    assert bool([c for c in calls if "Meridian" in c]) is bool(facts)


@pytest.mark.parametrize("content, metadata, processed", [
    pytest.param(f"Loved “{THEIRS}”", None, 0, id="reaction_by_its_words"),
    pytest.param(OTHER_LANGUAGE, REACTION_METADATA, 0, id="reaction_by_its_marker"),
    pytest.param(THEIRS, ORDINARY_METADATA, 1, id="the_owners_own_message"),
])
def test_the_owners_backfill_never_shows_a_pack_the_owners_reaction(node_db, monkeypatch, content, metadata,
                                                                    processed):
    from topos.features.derivation.registry import seed_pack_registry
    from topos.features.derivation.surfaces import run_pack_backfill
    seed_pack_registry(node_db, bundled_pack_dir())
    _message(node_db, "imessage:7", content, metadata=metadata)
    calls = []
    _stub_models(monkeypatch, calls)
    out = run_pack_backfill(node_db, "work.career", limit=5, use_prefilter=False)
    assert out["processed"] == processed and out["written"] == processed
    assert bool(calls) is bool(processed)


def test_the_backfill_plan_does_not_count_a_reaction_as_waiting(node_db):
    from topos.features.derivation.backfill_runs import backfill_plan
    from topos.features.derivation.registry import seed_pack_registry
    seed_pack_registry(node_db, bundled_pack_dir())
    node_db.execute("UPDATE pack_registry SET enabled=1 WHERE pack_id='work.career'")
    _message(node_db, "words", f"Loved “{THEIRS}”")
    _message(node_db, "marker", OTHER_LANGUAGE, metadata=REACTION_METADATA)
    _message(node_db, "own", THEIRS, metadata=ORDINARY_METADATA)
    plan = backfill_plan(node_db, ["work.career"])
    (career,) = [p for p in plan["packs"] if p["pack_id"] == "work.career"]
    assert career["waiting"] == 1


# --- the enable-offer trial ------------------------------------------------------------------

GOAL = "My goal this year is to run a marathon and finish under four hours"


@pytest.mark.parametrize("content, sampled", [
    pytest.param(GOAL, True, id="the_owners_own_messages"),
    pytest.param(f"Loved “{GOAL}”", False, id="the_owners_reactions"),
])
def test_the_enable_trial_does_not_sample_the_owners_reactions(node_db, monkeypatch, content, sampled):
    node_db.execute("INSERT OR IGNORE INTO pack_yield (pack_id, day, prefilter_hits)"
                    " VALUES ('aspirations.goals', date('now'), 40)")
    now = node_db.execute("SELECT datetime('now')").fetchone()[0]
    for i in range(6):
        _message(node_db, f"imessage:g{i}", content, event_at=now)
    calls = []
    _stub_models(monkeypatch, calls)
    stats = {}
    run_derivation_batch(node_db, [], stats=stats)
    assert ("trial_aspirations.goals" in stats) is sampled


# --- the commitments lane picks its own records ----------------------------------------------

THEIR_PROMISE = "I'll send you the signed copy tomorrow morning"


@pytest.mark.parametrize("with_metadata_column", [True, False])
def test_the_commitments_lane_does_not_pick_a_reaction_to_someone_elses_promise(with_metadata_column):
    from topos.features.derivation import commitments as C
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE conversation_messages (message_id TEXT, content TEXT, event_at TEXT,"
                 " is_from_self INTEGER, conversation_id TEXT, sender_id TEXT"
                 + (", metadata_json TEXT" if with_metadata_column else "") + ")")
    conn.execute("CREATE TABLE journal_entries (entry_id TEXT, content TEXT, entry_at TEXT)")
    rows = [("own", THEIR_PROMISE, None), ("words", f"Loved “{THEIR_PROMISE}”", None),
            ("marker", f"A aimé « {THEIR_PROMISE} »", json.dumps(REACTION_METADATA))]
    for mid, text, meta in rows:
        values = (mid, text, "2026-08-02T09:00:00Z", 1, "c1", "self") + ((meta,) if with_metadata_column else ())
        conn.execute(f"INSERT INTO conversation_messages VALUES ({','.join('?' for _ in values)})", values)
    assert C.is_promise_shaped(f"Loved “{THEIR_PROMISE}”"), "the reaction is promise-shaped: only the rule drops it"
    picked = {r["record_id"] for r in C.promise_shaped_records(conn)}
    assert picked == ({"own"} if with_metadata_column else {"own", "marker"})
