"""L4-8 — outward facts must not be restated as graph edges.

The engine holds a third-party fact at `owner_only` disclosure. The entity graph is a
different surface with different readers, so projecting the claim onto an edge would restate
it somewhere that disclosure rule does not reach. This query had no subject filter at all,
which was harmless while every pack fact was about the owner and became unsafe the moment
one was not.
"""

from __future__ import annotations

import json
import sqlite3
import uuid

import pytest

from topos.features.entities.fact_materializer import (
    _owner_entity_ids,
    materialize_signal_objects_to_graph,
)


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "g.db"))
    # The REAL schema, built by running every registered migration in order. A fixture that
    # hand-rolls these tables tests the hand-rolling: `entity_edges` alone gains columns
    # across several migrations, and the materializer reads ones the base DDL does not have.
    c.execute("CREATE TABLE IF NOT EXISTS wiki_schema_migrations (migration_id TEXT PRIMARY KEY)")
    from topos.storage.db.migrations.registry import MIGRATIONS
    for spec in sorted(MIGRATIONS, key=lambda m: m.order):
        try:
            spec.fn(c)
        except Exception:  # noqa: BLE001 — a migration needing absent state is not this test
            pass
    c.commit()
    for eid, name, is_self in (("ent_owner", "Owner", 1), ("ent_nora", "Nora Whitfield", 0)):
        c.execute("INSERT INTO entities (entity_id, entity_type, canonical_name,"
                  " normalized_name, is_self, mention_count) VALUES (?,?,?,?,?,3)",
                  (eid, "person", name, name.lower(), is_self))
    c.commit()
    yield c
    c.close()


def _fact(conn, subject, predicate, value, ontology=None):
    oid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key,"
        " payload_json, confidence, source_refs_json, valid_from, valid_to,"
        " extractor_version, created_at, updated_at, created_by, updated_by, ontology_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, "identity", "fact", f"fact:{subject}:{predicate}",
         json.dumps({"subject_entity_id": subject, "predicate": predicate,
                     "object_value": value, "confidence": 0.9}),
         0.9, "[]", "2026-05-01", None, "t", "2026-05-01", "2026-05-01",
         "test", "test", ontology))
    conn.commit()
    return oid


def _edges(conn):
    return conn.execute("SELECT COUNT(*) FROM entity_edges").fetchone()[0]


def test_an_owner_fact_still_projects(conn):
    """The guard must not cost the owner their own graph."""
    _fact(conn, "ent_owner", "works_at", "Dialogues")
    materialize_signal_objects_to_graph(conn)
    assert _edges(conn) >= 1


def test_a_third_party_fact_does_not_project(conn):
    """The claim stays in the fact store at owner_only; it does not become an edge."""
    _fact(conn, "ent_nora", "works_at", "Halcyon", ontology="net.capability")
    materialize_signal_objects_to_graph(conn)
    assert _edges(conn) == 0


def test_the_guard_separates_them_in_one_pass(conn):
    """Both facts, one pass: the owner's projects and the third party's does not."""
    _fact(conn, "ent_owner", "works_at", "Dialogues")
    _fact(conn, "ent_nora", "works_at", "Halcyon", ontology="net.capability")
    materialize_signal_objects_to_graph(conn)
    srcs = {r[0] for r in conn.execute("SELECT src_entity_id FROM entity_edges")}
    assert "ent_owner" in srcs
    assert "ent_nora" not in srcs, "a third-party claim must not become an edge"


def test_every_is_self_entity_counts(conn):
    """Plural on purpose — this machine has three, and reading one by rowid luck would make
    the guard depend on which one won."""
    conn.execute("UPDATE entities SET is_self=1 WHERE entity_id='ent_nora'")
    conn.commit()
    assert _owner_entity_ids(conn) == {"ent_owner", "ent_nora"}
    _fact(conn, "ent_nora", "works_at", "Halcyon")
    materialize_signal_objects_to_graph(conn)
    assert _edges(conn) >= 1, "a second is_self entity is still the owner"


def test_with_no_owner_entity_the_guard_fails_closed(conn):
    """REVERSED 2026-08-26 after adversarial review.

    The first version stood the guard down when no is_self entity existed, reasoning that a
    fresh node cannot hold outward facts. But the realistic way to LOSE the flag is an
    entity merge gone wrong — and in that state the old behaviour projected every
    third-party fact on the node. Owner facts pausing until the flag is repaired is
    recoverable; a dossier restated into the graph is not.
    """
    conn.execute("UPDATE entities SET is_self=0")
    conn.commit()
    assert _owner_entity_ids(conn) == set()
    _fact(conn, "ent_owner", "works_at", "Dialogues")
    materialize_signal_objects_to_graph(conn)
    assert _edges(conn) == 0, "no owner flag, no subject-fact projection"


def test_a_blackholed_object_never_becomes_an_edge(conn):
    """An owner-subject fact naming an excluded person as its OBJECT restated them into the
    graph — a surface the read-side blackhole filter never sees."""
    import json as _json
    import uuid as _uuid

    # the migrated table has 11 columns — insert by name, not position
    cols = {r[1] for r in conn.execute("PRAGMA table_info(entity_blackholes)")}
    assert {"blackhole_id", "entity_id"} <= cols, "real schema expected"
    conn.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name)"
                 " VALUES ('bh1', 'ent_nora', 'nora')")
    oid = str(_uuid.uuid4())
    conn.execute(
        "INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key,"
        " payload_json, confidence, source_refs_json, valid_from, valid_to,"
        " extractor_version, created_at, updated_at, created_by, updated_by)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, "identity", "fact", "fact:ent_owner:collaborated_with",
         _json.dumps({"subject_entity_id": "ent_owner", "predicate": "collaborated_with",
                      "object_value": "Nora", "object_entity_id": "ent_nora",
                      "confidence": 0.9}),
         0.9, "[]", "2026-05-01", None, "t", "2026-05-01", "2026-05-01", "test", "test"))
    conn.commit()
    materialize_signal_objects_to_graph(conn)
    dsts = {r[0] for r in conn.execute("SELECT dst_entity_id FROM entity_edges")}
    assert "ent_nora" not in dsts, "forget-this-person holds at projection too"
