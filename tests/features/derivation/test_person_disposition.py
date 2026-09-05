"""Plan §2.2, decision 1: the owner's informant rating is the primary witness for a
person's disposition — a STATED fact through the consent path — and decision 2: Negative
Emotionality is never rated for a third party.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.derivation import surfaces as S
from topos.analytics.person_graph import person_disposition_facts


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
      CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT,
        canonical_name TEXT, normalized_name TEXT, aliases_json TEXT, is_self INTEGER DEFAULT 0);
      CREATE TABLE fact_conflicts (conflict_id TEXT PRIMARY KEY, subject_entity_id TEXT NOT NULL,
        predicate TEXT NOT NULL, incumbent_object_id TEXT NOT NULL, challenger_value TEXT NOT NULL,
        challenger_confidence REAL, status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL DEFAULT (datetime('now')));
      INSERT INTO entities VALUES ('ent_owner','person','Owner','owner','[]',1);
      INSERT INTO entities VALUES ('ent_nora','person','Nora Vale','nora vale','[]',0);
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
    return conn


def _active(conn):
    rows = conn.execute("SELECT payload_json FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL").fetchall()
    return [json.loads(r[0]) for r in rows]


def test_a_rating_is_a_stated_fact_on_the_person_with_the_consent_recorded():
    conn = _conn()
    out = S.rate_person_disposition(conn, subject_entity_id="ent_nora", domain="agreeableness", level="high")
    assert out["outcome"] == "written" and out["consent_recorded"] is True
    facts = _active(conn)
    assert len(facts) == 1
    f = facts[0]
    assert f["predicate"] == "trait.bfi2_domain" and f["subject_entity_id"] == "ent_nora"
    assert f["value_struct"] == {"domain": "agreeableness", "level": "high"}
    assert f["asserted_by"] == "owner", "the owner said it — the card labels it 'your read'"
    assert conn.execute("SELECT policy, decided_by FROM net_subject_policy WHERE subject_entity_id='ent_nora'").fetchone() == ("allow", "owner")


def test_re_rating_revises_the_level_in_place_and_keeps_history():
    """The domain is the identity; the level is state. Two ratings of one domain must not
    read as two facts, and the first must stay in history."""
    conn = _conn()
    S.rate_person_disposition(conn, subject_entity_id="ent_nora", domain="extraversion", level="low")
    S.rate_person_disposition(conn, subject_entity_id="ent_nora", domain="extraversion", level="high")
    S.rate_person_disposition(conn, subject_entity_id="ent_nora", domain="open_mindedness", level="very_high")
    active = {f["value_struct"]["domain"]: f["value_struct"]["level"] for f in _active(conn)}
    assert active == {"extraversion": "high", "open_mindedness": "very_high"}
    total = conn.execute("SELECT COUNT(*) FROM signal_objects WHERE object_type='fact'").fetchone()[0]
    assert total >= 2


def test_negative_emotionality_is_never_rated_for_another_person():
    conn = _conn()
    with pytest.raises(ValueError, match="not rated for other people"):
        S.rate_person_disposition(conn, subject_entity_id="ent_nora", domain="negative_emotionality", level="low")
    assert _active(conn) == []


def test_a_black_holed_person_cannot_be_rated_even_by_the_owner():
    conn = _conn()
    conn.execute("INSERT INTO entity_blackholes VALUES ('bh1','ent_nora','nora vale')")
    with pytest.raises(ValueError, match="blackhole"):
        S.rate_person_disposition(conn, subject_entity_id="ent_nora", domain="agreeableness", level="high")


def test_bad_inputs_are_refused_before_anything_is_written():
    conn = _conn()
    for kw in ({"domain": "charisma", "level": "high"}, {"domain": "agreeableness", "level": "medium"},
               {"domain": "agreeableness", "level": "high", "subject_entity_id": "ent_nobody"}):
        args = {"subject_entity_id": "ent_nora", **kw}
        with pytest.raises(ValueError):
            S.rate_person_disposition(conn, **args)
    assert _active(conn) == []


def test_the_ratings_read_back_onto_the_persons_node_labelled_as_the_owners():
    conn = _conn()
    S.rate_person_disposition(conn, subject_entity_id="ent_nora", domain="conscientiousness", level="very_high")
    node = {"node_id": "ent:ent_nora", "entity_id": "ent_nora", "is_owner": False}
    other = {"node_id": "ent:ent_x", "entity_id": "ent_x", "is_owner": False}
    assert person_disposition_facts(conn, [node, other])["attached"] == 1
    disp = node["disposition"]
    assert disp["domains"]["conscientiousness"]["level"] == "very_high"
    assert disp["domains"]["conscientiousness"]["stated_by_owner"] is True
    assert disp["excluded"] == ["negative_emotionality"]
    assert "disposition" not in other
