"""Revising a pack fact's value keeps whose statement it was.

revise_fact re-asserted every revision as actor_role 'authored', so correcting a
value on an assistant- or contact-attributed fact silently made it the owner's own
statement.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.derivation.packs import load_packs
from topos.features.derivation.registry import bundled_pack_dir
from topos.features.derivation.surfaces import revise_fact
from topos.features.derivation.writer import DerivationWriter

PACK_ID = "relationships.social"  # role_policy authored_addressed


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(tmp_path / "revise.db")
    connection.executescript("""
      CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT,
        canonical_name TEXT, normalized_name TEXT, aliases_json TEXT, is_self INTEGER DEFAULT 0);
      CREATE TABLE fact_conflicts (conflict_id TEXT PRIMARY KEY, subject_entity_id TEXT NOT NULL,
        predicate TEXT NOT NULL, incumbent_object_id TEXT NOT NULL, challenger_value TEXT NOT NULL,
        challenger_confidence REAL, status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT (datetime('now')));
      INSERT INTO entities VALUES ('ent_owner','person','Owner','owner','[]',1);
      CREATE TABLE net_subject_policy (subject_entity_id TEXT PRIMARY KEY,
        policy TEXT NOT NULL, decided_by TEXT NOT NULL DEFAULT 'owner',
        note TEXT NOT NULL DEFAULT '', decided_at TEXT NOT NULL DEFAULT (datetime('now')));
      CREATE TABLE entity_blackholes (blackhole_id TEXT PRIMARY KEY,
        entity_id TEXT NOT NULL DEFAULT '', normalized_name TEXT NOT NULL);
      CREATE TABLE signal_objects (object_id TEXT PRIMARY KEY, signal_dimension TEXT,
        object_type TEXT, object_key TEXT, payload_json TEXT, confidence REAL,
        source_refs_json TEXT, valid_from TEXT, valid_to TEXT, extractor_version TEXT,
        created_at TEXT, updated_at TEXT, created_by TEXT, updated_by TEXT,
        period_start TEXT, period_end TEXT, ontology_id TEXT, ontology_version TEXT,
        altitude TEXT);
    """)
    yield connection
    connection.close()


def _fact(conn, *, actor_role):
    pack = load_packs(bundled_pack_dir(), only=[PACK_ID])[PACK_ID]
    out = DerivationWriter(conn, model="test-model").assert_pack_fact(
        pack=pack, predicate="rel.relationship", subject_entity_id="ent_owner",
        value={"person": "Nora", "role": "friend", "status": "active"}, actor_role=actor_role,
        source_refs=[{"table": "ai_chat_messages", "record_id": "assistant-reply-1"}],
        confidence=0.9, quote="Your friend Nora", about="owner")
    assert out["outcome"] == "written"
    return out["object_id"]


def _payload(conn, object_id):
    return json.loads(conn.execute("SELECT payload_json FROM signal_objects WHERE object_id=?", (object_id,)).fetchone()[0])


def _correct_attribution_in_place(conn, object_id, asserted_by):
    payload = _payload(conn, object_id)
    payload["asserted_by"] = asserted_by
    conn.execute("UPDATE signal_objects SET payload_json=? WHERE object_id=?", (json.dumps(payload), object_id))
    conn.commit()


CLOSE_FRIEND = {"person": "Nora", "role": "close_friend", "status": "active"}


def test_revising_an_assistant_attributed_fact_keeps_its_attribution(conn):
    incumbent = _fact(conn, actor_role="addressed")

    revised = _payload(conn, revise_fact(conn, incumbent, value=CLOSE_FRIEND)["object_id"])

    assert revised["value_struct"]["role"] == "close_friend"
    assert (revised["actor_role"], revised["asserted_by"]) == ("addressed", "extracted:addressed")


def test_a_verdict_corrected_attribution_survives_a_value_revision(conn):
    incumbent = _fact(conn, actor_role="authored")
    _correct_attribution_in_place(conn, incumbent, "assistant")

    revised = _payload(conn, revise_fact(conn, incumbent, value=CLOSE_FRIEND)["object_id"])

    assert (revised["actor_role"], revised["asserted_by"]) == ("addressed", "assistant")


def test_the_owner_can_restate_attribution_explicitly(conn):
    incumbent = _fact(conn, actor_role="addressed")

    revised = _payload(conn, revise_fact(conn, incumbent, value=CLOSE_FRIEND, asserted_by="owner")["object_id"])

    assert (revised["actor_role"], revised["asserted_by"]) == ("authored", "owner")


def test_an_attribution_the_pack_refuses_leaves_the_fact_live(conn):
    incumbent = _fact(conn, actor_role="authored")
    _correct_attribution_in_place(conn, incumbent, "contact:ent_nora")

    with pytest.raises(ValueError, match="contact:ent_nora"):
        revise_fact(conn, incumbent, value=CLOSE_FRIEND)

    assert conn.execute("SELECT valid_to FROM signal_objects WHERE object_id=?", (incumbent,)).fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM signal_objects WHERE object_type='fact'").fetchone()[0] == 1
