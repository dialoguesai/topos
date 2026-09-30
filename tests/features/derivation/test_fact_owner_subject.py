"""A derived owner fact binds to the owner's attested self, so the permissions-v2 subject gate can pass.

`owner_entity_id` picks the fact-bearing `is_self` row, a guess among several. The v2 permit
set holds only what the owner attested (OD-29). A pack or fact-LLM fact written on an
unattested self row therefore failed the subject gate however clean it was. With exactly one
attested self row, new owner facts bind to it; without an attestation nothing changes.
"""
import json
import sqlite3

import pytest

from tests.permissions_v2.test_owner_identity_binding import do_attest, do_revoke
from topos.enrichment.jobs.canonical.derivation_job import run_derivation_batch
from topos.features.entities.owner import fact_owner_subject, owner_entity_id
from topos.features.facts.extract import _owner_entity_id as rules_owner
from topos.permissions_v2.identity import ATTESTED_CONTRACT, attested_self, permit_subjects
from topos.permissions_v2.protection_clock import ensure_protection_clock
from topos.storage.canonical.conversations_tables import ensure_all_tables
from topos.storage.db.migrations import apply_all_migrations

GUESS = "ent_a_fact_bearing"   # sorts first: owner_entity_id's tie-break picks it
ATTESTED = "ent_z_attested"
SECOND = "ent_y_attested"


@pytest.fixture
def node(tmp_path):
    path = tmp_path / "node.db"
    conn = sqlite3.connect(path)
    ensure_all_tables(conn)
    apply_all_migrations(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS engine_config(key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR REPLACE INTO engine_config VALUES('user_id','owner-1')")
    for entity_id in (GUESS, ATTESTED, SECOND):
        conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json,"
                     " is_self) VALUES (?,'person','Owner','owner','[]',1)", (entity_id,))
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json,"
                 " is_self) VALUES ('ent_other','person','Other','other','[]',0)")
    conn.commit()
    conn.close()
    ensure_protection_clock(path, owner_id="owner-1")
    conn = sqlite3.connect(path)
    yield conn
    conn.close()


def _attest(conn, entity_id):
    with conn:
        return do_attest(conn, entity_id)


def _stub_models(monkeypatch):
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
        return {"text": extract}
    monkeypatch.setattr(ollama.OllamaAdapter, "_generate", fake)
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model", lambda s, c: "stub-9b")


ROW = {"content": "Signed the offer — starting as Staff Engineer at Meridian in March",
       "message_id": "imessage:1", "_table": "conversation_messages", "actor_role": "authored",
       "event_at": "2026-09-20", "source_id": "imessage"}


def _pack_fact_subjects(conn):
    return [json.loads(r[0])["subject_entity_id"] for r in conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='fact' AND ontology_id='work.career'")]


def test_a_pack_fact_passes_the_subject_gate_once_the_owner_attested(node, monkeypatch):
    """The reproduction: before the fix the fact landed on GUESS, outside the permit set."""
    _attest(node, ATTESTED)
    assert owner_entity_id(node) == GUESS
    assert GUESS not in permit_subjects(node, contract=ATTESTED_CONTRACT)
    _stub_models(monkeypatch)
    assert run_derivation_batch(node, [ROW], stats={}) == 1
    assert _pack_fact_subjects(node) == [ATTESTED]
    assert ATTESTED in permit_subjects(node, contract=ATTESTED_CONTRACT)


def test_without_an_attestation_the_pack_fact_is_unchanged(node, monkeypatch):
    _stub_models(monkeypatch)
    assert run_derivation_batch(node, [ROW], stats={}) == 1
    assert _pack_fact_subjects(node) == [GUESS]


def test_the_rules_and_fact_llm_owner_binds_the_same_way(node):
    assert rules_owner(node) == GUESS
    _attest(node, ATTESTED)
    assert rules_owner(node) == ATTESTED


@pytest.mark.parametrize("build, expected", [
    pytest.param(lambda conn: None, GUESS, id="no_attestation"),
    pytest.param(lambda conn: _attest(conn, ATTESTED), ATTESTED, id="one_attested_self"),
    pytest.param(lambda conn: _attest(conn, GUESS), GUESS, id="attested_the_guess"),
    pytest.param(lambda conn: (_attest(conn, ATTESTED), _attest(conn, GUESS)), GUESS, id="attested_guess_and_another"),
    pytest.param(lambda conn: (_attest(conn, ATTESTED), _attest(conn, SECOND)), GUESS, id="two_attested_selves"),
    pytest.param(lambda conn: (lambda entry: _revoke(conn, ATTESTED, entry))(_attest(conn, ATTESTED)), GUESS,
                 id="revoked"),
    pytest.param(lambda conn: (_attest(conn, ATTESTED), _unself(conn, ATTESTED)), GUESS, id="no_longer_self"),
])
def test_fact_owner_subject(node, build, expected):
    build(node)
    assert fact_owner_subject(node) == expected
    # Whatever it picks is either what the owner attested or the unchanged legacy guess.
    assert expected in permit_subjects(node, contract=ATTESTED_CONTRACT) or attested_self(node) is None


def _revoke(conn, entity_id, entry_id):
    with conn:
        do_revoke(conn, entity_id, entry_id)


def _unself(conn, entity_id):
    with conn:
        conn.execute("UPDATE entities SET is_self=0 WHERE entity_id=?", (entity_id,))


def test_unreadable_identity_state_is_unattested(tmp_path):
    conn = sqlite3.connect(tmp_path / "bare.db")
    conn.execute("CREATE TABLE entities (entity_id TEXT, is_self INTEGER)")
    conn.execute("INSERT INTO entities VALUES ('ent_only', 1)")
    assert attested_self(conn) is None
    assert fact_owner_subject(conn) == "ent_only"


def test_the_owner_backfill_control_binds_the_same_way(node, monkeypatch):
    from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry
    from topos.features.derivation.surfaces import run_pack_backfill
    seed_pack_registry(node, bundled_pack_dir())
    node.execute(
        "INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, event_at, sender_type, sender_id,"
        " content, source_id, is_from_self, actor_role) VALUES ('imessage:9','t','d','2026-09-20T10:00:00+00:00',"
        " 'human','self',?,'imessage',1,NULL)", (ROW["content"],))
    node.commit()
    _attest(node, ATTESTED)
    _stub_models(monkeypatch)
    assert run_pack_backfill(node, "work.career", limit=5)["written"] == 1
    assert _pack_fact_subjects(node) == [ATTESTED]
