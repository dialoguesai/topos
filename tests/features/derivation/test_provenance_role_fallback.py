"""An owner message whose actor_role column is NULL is read by the role its provenance proves.

The legacy conversation upsert never writes ``actor_role``, so every iMessage synced after
actor_role_v1's one backfill stores NULL, and the derivation job read NULL as ``observed``.
No owner-only pack (work.career, values.motivation, obligations.commitments,
relationships.social, aspirations.goals) could ever read the owner's own texts. These tests
pin the repair: the stored role wins, NULL on a message table falls back to ``record_role``
under the source's effective posture, and nobody else's message is ever promoted.
"""
import json
import sqlite3

import pytest

from topos.storage.canonical.conversations_tables import ensure_all_tables
from topos.storage.db.migrations import apply_all_migrations
from topos.enrichment.jobs.canonical import derivation_job as dj
from topos.enrichment.jobs.canonical.derivation_job import (
    ROLE_SKIP_RETIREMENT_MARKER, _iter_history, _row_to_record, retire_role_skipped_progress,
    run_derivation_batch)
from topos.features.derivation.packs import load_packs
from topos.features.derivation.registry import bundled_pack_dir

TEXT = "Signed the offer — starting as Staff Engineer at Meridian in March"


@pytest.fixture
def node_db(tmp_path):
    conn = sqlite3.connect(tmp_path / "node.db")
    ensure_all_tables(conn)
    apply_all_migrations(conn)   # actor_role_v1 adds the column and backfills the empty table once
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, is_self)"
                 " VALUES ('ent_owner','person','Owner','owner','[]',1)")
    conn.commit()
    return conn


def _message(conn, message_id, *, is_self, actor_role=None, sender_id=None, source_id="imessage",
             content=TEXT, event_at="2026-09-20T10:00:00+00:00"):
    conn.execute(
        "INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, event_at, sender_type,"
        " sender_id, content, source_id, is_from_self, actor_role) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (message_id, "thread-1", "dataset-1", event_at, "human",
         sender_id or ("self" if is_self else "+15550000000"), content, source_id, 1 if is_self else 0,
         actor_role))
    conn.commit()


def _mixed(row):
    return "mixed"


def _ambient(row):
    return "ambient"


def _stub_models(monkeypatch, calls):
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


def _career_facts(conn):
    return conn.execute("SELECT COUNT(*) FROM signal_objects WHERE object_type='fact'"
                        " AND ontology_id='work.career'").fetchone()[0]


# --- the reproduction -----------------------------------------------------------------------

def test_owner_imessage_with_null_role_reaches_an_owner_only_pack(node_db, monkeypatch):
    """The bug: the history walk read the owner's own text as observed and skipped it."""
    _message(node_db, "imessage:1", is_self=True)
    calls = []
    _stub_models(monkeypatch, calls)
    stats = {}
    run_derivation_batch(node_db, [], stats=stats)
    assert stats.get("catchup_written") == 1
    assert _career_facts(node_db) == 1
    assert calls, "the pack's extractor never saw the owner's message"


def test_someone_elses_null_role_message_stays_observed_and_unread(node_db, monkeypatch):
    _message(node_db, "imessage:2", is_self=False)
    calls = []
    _stub_models(monkeypatch, calls)
    run_derivation_batch(node_db, [], stats={})
    assert _career_facts(node_db) == 0
    assert not [c for c in calls if "Meridian" in c]


def test_batch_row_with_null_role_uses_the_owner_flag(node_db, monkeypatch):
    row = {"content": TEXT, "message_id": "imessage:3", "_table": "conversation_messages", "actor_role": None,
           "is_from_self": 1, "sender_id": "self", "sender_type": "human", "event_at": "2026-09-20",
           "source_id": "imessage"}
    calls = []
    _stub_models(monkeypatch, calls)
    assert run_derivation_batch(node_db, [row], stats={}) == 1


# --- the role rule --------------------------------------------------------------------------

@pytest.mark.parametrize("fields, expected", [
    pytest.param({"is_from_self": 1, "sender_id": "self"}, "authored", id="owner_flag"),
    pytest.param({"is_from_self": 0, "sender_id": "+15550000000"}, "observed", id="correspondent"),
    pytest.param({"is_from_self": 0, "sender_id": "+15550000000", "sender_type": "human"}, "observed",
                 id="correspondent_human_sender_type"),
    pytest.param({"is_from_self": "1", "sender_id": "+15550000000"}, "observed", id="text_flag_is_not_the_owner"),
    pytest.param({}, "observed", id="no_provenance_fields"),
])
def test_null_role_on_a_conversation_row(fields, expected):
    row = {"content": TEXT, "message_id": "m", "_table": "conversation_messages", "actor_role": None, **fields}
    assert _row_to_record(row, posture_for=_mixed)["role"] == expected


def test_stored_role_wins_over_provenance():
    row = {"content": TEXT, "message_id": "m", "_table": "conversation_messages", "actor_role": "observed",
           "is_from_self": 1, "sender_id": "self"}
    assert _row_to_record(row, posture_for=_mixed)["role"] == "observed"


def test_ambient_posture_caps_an_owner_message_at_observed():
    """The owner's per-connector background-noise toggle still strips belief eligibility."""
    row = {"content": TEXT, "message_id": "m", "_table": "conversation_messages", "actor_role": None,
           "is_from_self": 1, "sender_id": "self", "source_id": "imessage"}
    assert _row_to_record(row, posture_for=_ambient)["role"] == "observed"


def test_ambient_posture_caps_the_history_walk(node_db, monkeypatch):
    _message(node_db, "imessage:4", is_self=True)
    monkeypatch.setattr("topos.features.provenance.posture._resolve", lambda s, d, c: "ambient")
    roles = {r["record_id"]: r["role"] for r in _iter_history(node_db)}
    assert roles["imessage:4"] == "observed"


def test_history_walk_roles(node_db):
    _message(node_db, "imessage:5", is_self=True)
    _message(node_db, "imessage:6", is_self=False)
    _message(node_db, "imessage:7", is_self=False, actor_role="authored")
    roles = {r["record_id"]: r["role"] for r in _iter_history(node_db, posture_for=_mixed)}
    assert roles == {"imessage:5": "authored", "imessage:6": "observed", "imessage:7": "authored"}


@pytest.mark.parametrize("sender_type, expected", [("human", "authored"), ("assistant", "addressed"),
                                                   ("system", "ambient")])
def test_null_role_on_an_ai_chat_row(sender_type, expected):
    row = {"content": TEXT, "message_id": "m", "_table": "ai_chat_messages", "actor_role": None,
           "sender_type": sender_type}
    assert _row_to_record(row, posture_for=_mixed)["role"] == expected


def test_tables_without_a_role_column_keep_the_old_default():
    row = {"content": TEXT, "segment_id": "s", "record_id": "s", "_table": "transcript_segments",
           "actor_role": None, "is_from_self": 1, "sender_id": "self"}
    assert _row_to_record(row, posture_for=_mixed)["role"] == "observed"
    row = {"content": TEXT, "entry_id": "j", "_table": "journal_entries", "actor_role": None}
    assert _row_to_record(row, posture_for=_mixed)["role"] == "authored"


# --- keys a walk wrote when it skipped the owner's message ----------------------------------

def _key(pack_id, message_id):
    pack = load_packs(bundled_pack_dir(), only=[pack_id])[pack_id]
    return f"{pack_id}@{pack.version}:conversation_messages:{message_id}"


def _keys(conn):
    return {r[0] for r in conn.execute("SELECT key FROM derivation_progress")}


def test_role_skip_keys_are_retired_once(node_db):
    _message(node_db, "imessage:10", is_self=True)
    _message(node_db, "imessage:11", is_self=False)
    _message(node_db, "imessage:12", is_self=True, actor_role="authored")
    _message(node_db, "imessage:13", is_self=True)
    skipped = _key("work.career", "imessage:10")
    other = _key("work.career", "imessage:11")
    stored = _key("work.career", "imessage:12")
    judged = _key("work.career", "imessage:13")
    open_pack = _key("net.character", "imessage:10")
    for key in (skipped, other, stored, judged, open_pack):
        node_db.execute("INSERT INTO derivation_progress (key) VALUES (?)", (key,))
    pack = load_packs(bundled_pack_dir(), only=["work.career"])["work.career"]
    node_db.execute(
        "INSERT INTO derivation_training_ledger (ledger_id, stage, pack_id, pack_version, template_version,"
        " extract_model, verifier_model, source_table, record_id, actor_role, predicate, value_json)"
        " VALUES ('dtl_1','catchup','work.career',?,'t','m','v','conversation_messages','imessage:13',"
        " 'authored','work.career_event','{}')", (pack.version,))
    all_packs = load_packs(bundled_pack_dir())
    assert "observed" in all_packs["net.character"].allowed_roles()
    assert retire_role_skipped_progress(node_db, all_packs, _mixed) == 1
    keys = _keys(node_db)
    assert skipped not in keys
    assert {other, stored, judged, open_pack, ROLE_SKIP_RETIREMENT_MARKER} <= keys
    # Once only: a key the fixed walk writes afterwards is a real decision and stays.
    node_db.execute("INSERT INTO derivation_progress (key) VALUES (?)", (skipped,))
    assert retire_role_skipped_progress(node_db, all_packs, _mixed) == 0
    assert skipped in _keys(node_db)


def test_role_skip_retirement_respects_the_ambient_cap(node_db):
    _message(node_db, "imessage:20", is_self=True)
    skipped = _key("work.career", "imessage:20")
    node_db.execute("INSERT INTO derivation_progress (key) VALUES (?)", (skipped,))
    assert retire_role_skipped_progress(node_db, load_packs(bundled_pack_dir()), _ambient) == 0
    assert skipped in _keys(node_db)


def test_a_skipped_owner_message_is_read_after_the_retirement(node_db, monkeypatch):
    _message(node_db, "imessage:30", is_self=True)
    node_db.execute("INSERT INTO derivation_progress (key) VALUES (?)", (_key("work.career", "imessage:30"),))
    node_db.commit()
    calls = []
    _stub_models(monkeypatch, calls)
    stats = {}
    run_derivation_batch(node_db, [], stats=stats)
    assert stats.get("role_skips_retired") == 1
    assert _career_facts(node_db) == 1


def test_owner_backfill_control_reads_the_owner_message(node_db, monkeypatch):
    from topos.features.derivation.registry import seed_pack_registry
    from topos.features.derivation.surfaces import run_pack_backfill
    seed_pack_registry(node_db, bundled_pack_dir())
    _message(node_db, "imessage:40", is_self=True)
    node_db.execute("INSERT INTO derivation_progress (key) VALUES (?)", (_key("work.career", "imessage:40"),))
    node_db.commit()
    calls = []
    _stub_models(monkeypatch, calls)
    out = run_pack_backfill(node_db, "work.career", limit=5)
    assert out["written"] == 1
    assert ROLE_SKIP_RETIREMENT_MARKER in _keys(node_db)


GOAL = "My goal this year is to run a marathon and finish under four hours"


@pytest.mark.parametrize("is_self, sampled", [(True, True), (False, False)])
def test_the_enable_trial_samples_owner_messages_only(node_db, monkeypatch, is_self, sampled):
    """A disabled owner-only pack's trial reads the owner's NULL-role texts, never anyone else's."""
    node_db.execute("INSERT OR IGNORE INTO pack_yield (pack_id, day, prefilter_hits)"
                    " VALUES ('aspirations.goals', date('now'), 40)")
    for i in range(6):
        _message(node_db, f"imessage:g{i}", is_self=is_self, content=GOAL,
                 event_at=node_db.execute("SELECT datetime('now')").fetchone()[0])
    calls = []
    _stub_models(monkeypatch, calls)
    stats = {}
    run_derivation_batch(node_db, [], stats=stats)
    assert ("trial_aspirations.goals" in stats) is sampled
