"""The owner's goal and place edges start from the owner's attested self.

The v2 relationship projection releases a goal's `pursues` edge only when the edge's
source is a subject the owner attested. The enrichers sourced every owner edge from
`owner_entity_id`, a fact-count pick among several `is_self` rows, so the edge failed that
check whenever the owner attested another row. With exactly one attested self row, the
edges start there and a rebuild's sweep retires the old ones; without an attestation
nothing changes. Neither owner spelling may become a goal's related entity or a place.
"""
import json
import sqlite3

import pytest

from tests.permissions_v2.test_owner_identity_binding import do_attest
from topos.features.entities.edges import graph_snapshot
from topos.features.entities.fact_materializer import sweep_stale_materialized_edges
from topos.features.entities.graph_enrichers import materialize_graph_enrichments
from topos.permissions_v2.identity import ATTESTED_CONTRACT, permit_subjects
from topos.permissions_v2.protection_clock import ensure_protection_clock
from topos.storage.db.migrations import apply_all_migrations

GUESS = "ent_a_fact_bearing"   # sorts first: owner_entity_id's tie-break picks it
ATTESTED = "ent_z_attested"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "g.db"
    c = sqlite3.connect(path)
    apply_all_migrations(c)
    c.execute("CREATE TABLE IF NOT EXISTS engine_config(key TEXT PRIMARY KEY, value TEXT)")
    c.execute("INSERT OR REPLACE INTO engine_config VALUES('user_id','owner-1')")
    for entity_id in (GUESS, ATTESTED):
        c.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json,"
                  " is_self) VALUES (?,'person','Owner','owner','[]',1)", (entity_id,))
    c.execute("INSERT INTO user_goals (goal_id, record_id, source_id, goal_text, payload_json)"
              " VALUES ('g1', 'msg-9', 'imessage', 'Run the spring half marathon', '{}')")
    c.commit()
    c.close()
    ensure_protection_clock(path, owner_id="owner-1")
    c = sqlite3.connect(path)
    yield c
    c.close()


def _attest(conn, entity_id):
    with conn:
        do_attest(conn, entity_id)


def _edges(conn, edge_type):
    return [e for e in graph_snapshot(conn, min_weight=0.0)["edges"] if e["edge_type"] == edge_type]


def _mention(conn, entity_id, record_id="msg-9"):
    conn.execute("INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, surface_text,"
                 " confidence, created_at) VALUES (?, ?, ?, 'imessage', 'me', 0.9, '2026-09-01')",
                 (f"m-{entity_id}", entity_id, record_id))
    conn.commit()


def test_a_goal_edge_starts_from_the_attested_self(conn):
    """The reproduction: before the fix the edge started at GUESS, outside the permit set."""
    _attest(conn, ATTESTED)
    assert GUESS not in permit_subjects(conn, contract=ATTESTED_CONTRACT)
    materialize_graph_enrichments(conn)
    pursues = _edges(conn, "pursues")
    assert [e["src_node_id"] for e in pursues] == [ATTESTED]
    assert ATTESTED in permit_subjects(conn, contract=ATTESTED_CONTRACT)


def test_without_an_attestation_the_goal_edge_is_unchanged(conn):
    materialize_graph_enrichments(conn)
    assert [e["src_node_id"] for e in _edges(conn, "pursues")] == [GUESS]


def test_an_attestation_moves_the_edge_at_the_next_rebuild(conn):
    materialize_graph_enrichments(conn)
    _attest(conn, ATTESTED)
    touched: set = set()
    materialize_graph_enrichments(conn, touched_edges=touched)
    sweep_stale_materialized_edges(conn, touched)
    conn.commit()
    assert [e["src_node_id"] for e in _edges(conn, "pursues")] == [ATTESTED]


@pytest.mark.parametrize("mentioned", [GUESS, ATTESTED])
def test_neither_owner_spelling_becomes_a_goals_related_entity(conn, mentioned):
    _attest(conn, ATTESTED)
    _mention(conn, mentioned)
    materialize_graph_enrichments(conn)
    assert _edges(conn, "relates_to") == []


def test_another_person_on_the_goal_record_still_relates(conn):
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json,"
                 " is_self) VALUES ('ent_coach','person','Coach','coach','[]',0)")
    _attest(conn, ATTESTED)
    _mention(conn, "ent_coach")
    materialize_graph_enrichments(conn)
    assert [e["dst_node_id"] for e in _edges(conn, "relates_to")] == ["ent_coach"]


def test_place_edges_start_from_the_attested_self(conn):
    _attest(conn, ATTESTED)
    for i in range(2):
        conn.execute("INSERT INTO location_events (event_id, place_name, event_at, source_id)"
                     f" VALUES ('l{i}', 'Metro Fitness', '2026-06-0{i + 1}T10:00:00Z', 'grow_journal')")
    conn.commit()
    materialize_graph_enrichments(conn)
    located = _edges(conn, "located_at")
    assert [e["src_node_id"] for e in located] == [ATTESTED]
    assert json.loads(located[0]["metadata_json"] or "{}").get("visit_count") == 2


def test_a_place_resolving_to_either_owner_spelling_is_skipped(conn, monkeypatch):
    from topos.features.entities import resolver as resolver_module
    _attest(conn, ATTESTED)
    conn.execute("INSERT INTO location_events (event_id, place_name, event_at, source_id)"
                 " VALUES ('l0', 'Home Base', '2026-06-01T10:00:00Z', 'grow_journal')")
    conn.commit()
    monkeypatch.setattr(resolver_module.EntityResolver, "resolve",
                        lambda self, name, **kw: (GUESS, "exact"))
    materialize_graph_enrichments(conn)
    assert _edges(conn, "located_at") == []
