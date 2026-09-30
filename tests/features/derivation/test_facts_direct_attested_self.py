"""The owner's known-item answers keep reading facts written on their attested self.

Derived owner facts bind to the owner's attested self once there is exactly one
(`fact_owner_subject`). The facts-direct lane read only the fact-bearing `is_self` row, so
every fact written after the owner attested another self row vanished from their own
answers until that row happened to hold more facts. It now reads both rows; without an
attestation it reads the one row it always did.
"""
import json
import sqlite3

import pytest

from tests.permissions_v2.test_owner_identity_binding import do_attest
from topos.permissions_v2.protection_clock import ensure_protection_clock
from topos.query.facts_direct import fetch_direct_facts
from topos.storage.db.migrations import apply_all_migrations

GUESS = "ent_a_fact_bearing"
ATTESTED = "ent_z_attested"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "f.db"
    c = sqlite3.connect(path)
    apply_all_migrations(c)
    c.execute("CREATE TABLE IF NOT EXISTS engine_config(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("INSERT OR REPLACE INTO engine_config VALUES('user_id','owner-1')")
    for entity_id, is_self in ((GUESS, 1), (ATTESTED, 1), ("ent_friend", 0)):
        c.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json,"
                  " is_self) VALUES (?,'person','P','p','[]',?)", (entity_id, is_self))
    c.commit()
    c.close()
    ensure_protection_clock(path, owner_id="owner-1")
    c = sqlite3.connect(path)
    _fact(c, "f_old", GUESS, "topos", "2026-06-01")
    _fact(c, "f_old2", GUESS, "garden", "2026-06-02")
    _fact(c, "f_friend", "ent_friend", "their startup", "2026-09-10")
    yield c
    c.close()


def _fact(conn, object_id, subject, project, valid_from):
    conn.execute("INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json,"
                 " confidence, source_refs_json, valid_from, created_at, updated_at, ontology_id, altitude)"
                 " VALUES (?,'work','fact',?,?,0.9,'[]',?,?,?,'work.career','stated')",
                 (object_id, f"fact:{subject}:work.project:{object_id}",
                  json.dumps({"object_value": json.dumps({"project": project})}), valid_from, valid_from, valid_from))
    conn.commit()


def _projects(conn):
    got = fetch_direct_facts(conn, ["work.project"], special=False, packet_resolution="facts") or []
    return [json.loads(f["value"])["project"] for f in got]


def test_a_fact_on_the_attested_self_reaches_the_owners_answer(conn):
    with conn:
        do_attest(conn, ATTESTED)
    _fact(conn, "f_new", ATTESTED, "marathon plan", "2026-09-20")
    # The reproduction: before the fix only GUESS was read and the new fact was missing.
    assert _projects(conn) == ["marathon plan", "garden", "topos"]


def test_without_an_attestation_only_the_fact_bearing_row_is_read(conn):
    _fact(conn, "f_new", ATTESTED, "marathon plan", "2026-09-20")
    assert _projects(conn) == ["garden", "topos"]


def test_attesting_the_fact_bearing_row_reads_it_once(conn):
    with conn:
        do_attest(conn, GUESS)
    assert _projects(conn) == ["garden", "topos"]
