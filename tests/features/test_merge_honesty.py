"""`merge_entities` said "reversible merge" and ended in a DELETE.

Two defects, both found by measuring what a real owner collapse would touch:

  * it folded aliases, mentions and edges, and missed the derivation corpus entirely —
    335 signal_objects, 13 fact_conflicts and 3 intelligence_exclusions on the live node
    would have been left pointing at a row that no longer exists;
  * it recorded nothing, so an incorrect merge could be undone only by re-ingesting.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.entities.resolver import (
    _remap_derivation_corpus,
    _write_merge_tombstone,
)


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "m.db"))
    c.executescript("""
      CREATE TABLE signal_objects (object_id TEXT PRIMARY KEY, object_type TEXT,
        object_key TEXT, payload_json TEXT);
      CREATE TABLE fact_conflicts (conflict_id TEXT PRIMARY KEY, subject_entity_id TEXT);
      CREATE TABLE intelligence_exclusions (exclusion_id TEXT PRIMARY KEY, entity_id TEXT);
      CREATE TABLE entity_blackholes (blackhole_id TEXT PRIMARY KEY, entity_id TEXT);
    """)
    c.execute("INSERT INTO signal_objects VALUES ('o1','fact','fact:ent_gone:rel.x',?)",
              (json.dumps({"subject_entity_id": "ent_gone", "predicate": "rel.x"}),))
    c.execute("INSERT INTO fact_conflicts VALUES ('c1','ent_gone')")
    c.execute("INSERT INTO intelligence_exclusions VALUES ('x1','ent_gone')")
    c.execute("INSERT INTO entity_blackholes VALUES ('b1','ent_gone')")
    c.commit()
    yield c
    c.close()


def test_facts_follow_the_surviving_entity(conn):
    """A merge that leaves facts on the absorbed id produces the dangling-subject shape that
    made two promoted facts unreachable — nothing can display, correct or exclude them."""
    _remap_derivation_corpus(conn, keep_id="ent_keep", absorb_id="ent_gone")
    key, payload = conn.execute(
        "SELECT object_key, payload_json FROM signal_objects").fetchone()
    assert key == "fact:ent_keep:rel.x"
    assert json.loads(payload)["subject_entity_id"] == "ent_keep"


def test_the_key_and_the_payload_agree_after_a_merge(conn):
    """The subject appears in both, and a reader trusting one over the other would see two
    different answers for the same fact."""
    _remap_derivation_corpus(conn, keep_id="ent_keep", absorb_id="ent_gone")
    key, payload = conn.execute(
        "SELECT object_key, payload_json FROM signal_objects").fetchone()
    assert key.split(":")[1] == json.loads(payload)["subject_entity_id"]


def test_the_review_queue_and_the_exclusions_follow_too(conn):
    _remap_derivation_corpus(conn, keep_id="ent_keep", absorb_id="ent_gone")
    assert conn.execute("SELECT subject_entity_id FROM fact_conflicts").fetchone()[0] == "ent_keep"
    assert conn.execute("SELECT entity_id FROM intelligence_exclusions").fetchone()[0] == "ent_keep"


def test_an_exclusion_survives_the_merge(conn):
    """The most important one. If the blackhole did not follow, merging an excluded person
    into another entity would silently un-exclude them."""
    _remap_derivation_corpus(conn, keep_id="ent_keep", absorb_id="ent_gone")
    assert conn.execute("SELECT entity_id FROM entity_blackholes").fetchone()[0] == "ent_keep"


def test_a_missing_table_is_a_zero_not_a_crash(conn):
    """net_subject_policy ships ahead of its migration by design, so a node without it must
    still merge."""
    counts = _remap_derivation_corpus(conn, keep_id="ent_keep", absorb_id="ent_gone")
    assert counts["net_subject_policy"] == 0
    assert counts["fact_conflicts"] == 1


def test_the_tombstone_records_what_was_absorbed(conn):
    _write_merge_tombstone(conn, keep_id="ent_keep", absorb_id="ent_gone",
                           name="Golf Zulu", aliases=["JZ"], identifiers=["jz@example.com"])
    row = conn.execute(
        "SELECT merged_into, canonical_name, identifiers_json FROM entity_merge_tombstones"
    ).fetchone()
    assert row[0] == "ent_keep"
    assert row[1] == "Golf Zulu"
    assert json.loads(row[2]) == ["jz@example.com"]


def test_a_chain_of_merges_resolves_to_the_survivor(conn):
    """A -> B then B -> C must leave A pointing at C, not at a row that is itself gone."""
    _write_merge_tombstone(conn, keep_id="ent_b", absorb_id="ent_a",
                           name="A", aliases=[], identifiers=[])
    _write_merge_tombstone(conn, keep_id="ent_c", absorb_id="ent_b",
                           name="B", aliases=[], identifiers=[])
    rows = dict(conn.execute(
        "SELECT absorbed_entity_id, merged_into FROM entity_merge_tombstones").fetchall())
    assert rows["ent_a"] == "ent_c"
    assert rows["ent_b"] == "ent_c"
