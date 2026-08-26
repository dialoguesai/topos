"""C7 facts-direct lane (W3.1) — deterministic known-item answers."""
import json
import sqlite3

import pytest

from topos.query.facts_direct import (match_known_item, fetch_direct_facts,
                                      compose_facts_answer, try_facts_direct)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "f.db")
    conn.executescript("""
      CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT,
        canonical_name TEXT, normalized_name TEXT, aliases_json TEXT, is_self INTEGER DEFAULT 0);
      CREATE TABLE signal_objects (object_id TEXT PRIMARY KEY, signal_dimension TEXT,
        object_type TEXT, object_key TEXT, payload_json TEXT, confidence REAL,
        source_refs_json TEXT, valid_from TEXT, valid_to TEXT, extractor_version TEXT,
        created_at TEXT, updated_at TEXT, created_by TEXT, updated_by TEXT,
        period_start TEXT, period_end TEXT, ontology_id TEXT, ontology_version TEXT, altitude TEXT);
      INSERT INTO entities VALUES ('ent_o','person','O','o','[]',1);
    """)
    def fact(oid, key, value, pack, altitude="stated", vf="2026-06-01", vt=None):
        conn.execute("INSERT INTO signal_objects (object_id, object_type, object_key, payload_json,"
                     " confidence, valid_from, valid_to, ontology_id, altitude)"
                     " VALUES (?, 'fact', ?, ?, 0.9, ?, ?, ?, ?)",
                     (oid, key, json.dumps({"object_value": value,
                                            "source_refs": [{"r": 1}, {"r": 2}]}), vf, vt, pack, altitude))
    fact("f1", "fact:ent_o:health.medication:metformin", "metformin", "health.physical")
    fact("f2", "fact:ent_o:work.project:topo", json.dumps({"project": "topos", "status": "active"}), "work.career")
    fact("f3", "fact:ent_o:work.project:old", json.dumps({"project": "old thing"}), "work.career",
         vf="2026-01-01", vt="2026-02-01")   # closed — must never surface
    conn.commit()
    return conn


def test_matcher_requires_owner_frame():
    assert match_known_item("what medications am I taking") is not None
    assert match_known_item("medications in general") is None          # no owner frame
    assert match_known_item("what is the weather like where I live") is None


def test_special_class_needs_facts_all(db):
    assert fetch_direct_facts(db, ["health.medication"], special=True,
                              packet_resolution="facts") is None
    got = fetch_direct_facts(db, ["health.medication"], special=True,
                             packet_resolution="facts_all")
    assert got and got[0]["value"] == "metformin" and got[0]["evidence_count"] == 2


def test_scores_only_never_fires(db):
    assert try_facts_direct(db, "what are my projects at work",
                            packet_resolution="scores_only") is None


def test_full_lane_known_item(db):
    out = try_facts_direct(db, "what medications am I taking",
                           packet_resolution="facts_all")
    assert out and out["answer_type"] == "facts" and "metformin" in out["answer"]
    assert out["facts"][0]["valid_from"] == "2026-06-01"


def test_closed_facts_never_surface(db):
    out = try_facts_direct(db, "what are my active projects at work",
                           packet_resolution="facts_all")
    assert out is not None
    assert "old thing" not in json.dumps(out)


def test_empty_store_falls_through(db):
    out = try_facts_direct(db, "what am I allergic to", packet_resolution="facts_all")
    assert out is None   # LLM path gets to say "no data", with empty_cause intact


def test_durable_role_facts_outrank_recent_events(db):
    """Live 2026-08-26: recency-only ordering let a month of met-events push the
    owner's parents past the compose cap. Durable facts (role/status) sort first;
    events keep recency order among themselves."""
    conn = db
    def fact(oid, value, vf):
        conn.execute(
            "INSERT INTO signal_objects (object_id, object_type, object_key, payload_json,"
            " confidence, valid_from, ontology_id, altitude)"
            " VALUES (?, 'fact', ?, ?, 0.9, ?, 'relationships.social', 'stated')",
            (oid, f"fact:ent_o:rel.relationship:{oid}",
             json.dumps({"object_value": json.dumps(value)}), vf))
    # Old durable kin facts…
    fact("mom", {"person": "mom", "role": "parent", "status": "active"}, "2026-05-01")
    fact("bro", {"person": "brother", "role": "sibling", "status": "active"}, "2026-05-02")
    # …buried under newer event facts.
    for i in range(5):
        fact(f"met{i}", {"person": f"acquaintance {i}", "event": "met"}, f"2026-08-{10 + i:02d}")
    conn.commit()

    got = fetch_direct_facts(conn, ["rel.relationship"], special=False,
                             packet_resolution="facts")
    values = [json.loads(f["value"]) for f in got]
    roles = [v.get("role") for v in values]
    # Every durable (role-bearing) fact precedes every event fact.
    first_event = next(i for i, v in enumerate(values) if v.get("event"))
    assert all(r for r in roles[:first_event])
    assert {"parent", "sibling"} <= set(roles[:first_event])
    # Events keep recency DESC among themselves (stable sort).
    events = [v["person"] for v in values[first_event:] if v.get("event")]
    assert events == sorted(events, key=lambda p: p, reverse=True) or events == [
        f"acquaintance {i}" for i in range(4, -1, -1)
    ]
    # The composed answer now names the kin within the cap.
    answer = compose_facts_answer(got)
    assert "parent" in answer and "sibling" in answer
