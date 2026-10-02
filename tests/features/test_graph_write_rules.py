"""One rule per write the graph rebuild makes (1.4.4): write when the value moves, never
when it would not, and when it moves, write what was always written.

The end-to-end no-op check lives in ``test_graph_refresh_no_churn``; these pin each
writer on its own, so a regression names the writer.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.entities import fact_materializer, graph_enrichers, maintenance
from topos.features.entities.fact_materializer import (
    _upsert_materialized_edge,
    coalesced_edge_writes,
    widen_node_window,
)
from topos.features.entities.resolver import EntityResolver
from topos.features.lifecycle.derived_scrub import _recount_entity_mentions
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "rules.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _changes(conn, fn, *args, **kwargs):
    """Rows ``fn`` wrote through ``conn``, same-value rewrites included; and its result."""
    before = conn.total_changes
    result = fn(*args, **kwargs)
    return conn.total_changes - before, result


def _edge(conn, edge_id):
    return conn.execute(
        "SELECT weight, evidence_count, valid_from, valid_to, last_event_at, metadata_json, updated_at "
        "FROM entity_edges WHERE edge_id=?", (edge_id,)).fetchone()


# --- evidence edges ------------------------------------------------------------------------------


def test_the_evidence_diff_writes_only_what_moved():
    stored = {
        ("a", "b", "co_occurrence"): ("e1", 2.0, 2, "2026-01-02", "2026-01-01T00:00:00Z", '{"actor_role": "authored"}'),
        ("a", "c", "co_occurrence"): ("e2", 1.0, 1, "2026-01-01", "2026-01-01T00:00:00Z", '{"actor_role": "observed"}'),
        ("a", "d", "co_occurrence"): ("e3", 1.0, 1, "2026-01-01", None, '{"actor_role": "observed"}'),
        ("b", "c", "communicates_with"): ("e4", 1.0, 1, "2026-01-01", "2026-01-01T00:00:00Z", '{"x": 1}'),
    }
    folded = {
        # unchanged, metadata spelled differently but equal
        ("a", "b", "co_occurrence"): (2.0, 2, "2026-01-02", '{"actor_role":"authored"}'),
        # one more observation
        ("a", "c", "co_occurrence"): (2.0, 2, "2026-01-03", '{"actor_role": "observed"}'),
        # same values, but stored without a belief date
        ("a", "d", "co_occurrence"): (1.0, 1, "2026-01-01", '{"actor_role": "observed"}'),
        # new
        ("c", "d", "co_occurrence"): (1.0, 1, "2026-01-04", '{"actor_role": "observed"}'),
    }
    inserts, updates, deletes = maintenance._evidence_edge_writes(stored, folded, "2026-10-02T00:00:00Z")
    assert [row[1:4] for row in inserts] == [("c", "d", "co_occurrence")]
    assert inserts[0][7] == "2026-10-02T00:00:00Z", "a new edge begins believing now"
    assert sorted(u[-1] for u in updates) == ["e2", "e3"]
    by_id = {u[-1]: u for u in updates}
    assert by_id["e2"][:4] == (2.0, 2, "2026-01-03", "2026-01-01T00:00:00Z"), "keeps its belief date"
    assert by_id["e3"][3] == "2026-10-02T00:00:00Z"
    assert deletes == [("e4",)]


def test_an_unchanged_evidence_set_is_not_rewritten(conn):
    r = EntityResolver(conn)
    a, b = r._create_entity("Ada", "person"), r._create_entity("Bram", "person")
    for mid, eid in (("m1", a), ("m2", b)):
        conn.execute(
            "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, canonical_table, surface_text, "
            "confidence, event_at, created_at) VALUES (?, ?, 'r1', 'imessage', 'conversation_messages', 'x', 0.9, "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')", (mid, eid))
    conn.commit()
    maintenance.rebuild_evidence_edges(conn)
    ids = [r[0] for r in conn.execute("SELECT edge_id FROM entity_edges")]
    written, _ = _changes(conn, maintenance.rebuild_evidence_edges, conn)
    assert written == 0
    assert [r[0] for r in conn.execute("SELECT edge_id FROM entity_edges")] == ids


# --- materialized edges --------------------------------------------------------------------------


def _upsert(conn, **overrides):
    kwargs = dict(src="s", dst="d", edge_type="pursues", weight=2.0, valid_from="2026-01-01",
                  valid_to=None, statement="pursues: x", source_object_id="g1", actor_role="authored")
    kwargs.update(overrides)
    return _upsert_materialized_edge(conn, **kwargs)


def test_a_materialized_edge_holding_its_values_is_not_rewritten(conn):
    edge_id = _upsert(conn)
    conn.execute("UPDATE entity_edges SET updated_at='2000-01-01 00:00:00' WHERE edge_id=?", (edge_id,))
    written, again = _changes(conn, _upsert, conn)
    assert again == edge_id and written == 0
    assert _edge(conn, edge_id)[6] == "2000-01-01 00:00:00"


def test_a_materialized_edge_that_moved_is_rewritten_as_before(conn):
    edge_id = _upsert(conn)
    written, _ = _changes(conn, _upsert, conn, weight=3.0, statement="pursues: x (×2)", evidence_count=2)
    assert written == 1
    weight, count, valid_from, _to, last, meta, _ = _edge(conn, edge_id)
    assert (weight, count, valid_from, last) == (3.0, 2, "2026-01-01", "2026-01-01")
    assert json.loads(meta)["statement"] == "pursues: x (×2)"


def test_an_undated_materialized_edge_is_stored_undated_from_the_start(conn):
    edge_id = _upsert(conn, valid_from=None)
    assert _edge(conn, edge_id)[2] is None
    written, _ = _changes(conn, _upsert, conn, valid_from=None)
    assert written == 0


def test_the_visit_count_rides_the_upsert_as_the_old_second_update_wrote_it(conn):
    first = _upsert(conn, edge_type="located_at", valid_from="2026-01-01", last_event_at="2026-01-09",
                    patch_metadata={"visit_count": 2}, evidence_count=2)
    weight, count, valid_from, _to, last, meta, _ = _edge(conn, first)
    assert (count, valid_from, last) == (2, "2026-01-01", "2026-01-09")
    assert json.loads(meta)["visit_count"] == 2 and '"visit_count":2' in meta  # json_patch's compact form
    written, _ = _changes(conn, _upsert, conn, edge_type="located_at", valid_from="2026-01-01",
                          last_event_at="2026-01-09", patch_metadata={"visit_count": 2}, evidence_count=2)
    assert written == 0
    written, _ = _changes(conn, _upsert, conn, edge_type="located_at", valid_from="2026-01-01",
                          last_event_at="2026-01-12", patch_metadata={"visit_count": 3}, evidence_count=3)
    assert written == 2  # the upsert and its patch, as before
    assert json.loads(_edge(conn, first)[5])["visit_count"] == 3


def test_coalesced_writes_give_each_edge_its_last_value_once(conn):
    edge_id = _upsert(conn, edge_type="discusses", statement="topic: x", source_object_id="t1")
    with coalesced_edge_writes() as pending:
        # The topic lane, then the discourse lane, write the same edge.
        first = _upsert(conn, edge_type="discusses", statement="topic: y", source_object_id="t9")
        last = _upsert(conn, edge_type="discusses", statement="topic: x", source_object_id="t1")
        new = _upsert(conn, dst="d2", edge_type="discusses", statement="topic: z", source_object_id="t1")
        assert first == last == edge_id and new != edge_id
        assert _edge(conn, new) is None, "nothing is written before the flush"
        written, _ = _changes(conn, pending.flush, conn)
    assert written == 1, "the edge that ends where it began is not touched; the new one is inserted"
    assert json.loads(_edge(conn, edge_id)[5])["statement"] == "topic: x"
    assert _edge(conn, new) is not None


def test_the_value_surface_purge_keeps_an_entity_only_a_pending_edge_holds(conn, monkeypatch):
    r = EntityResolver(conn)
    owner = r._create_entity("Owner", "person")
    conn.execute("UPDATE entities SET is_self=1 WHERE entity_id=?", (owner,))
    value = r._create_entity("next tuesday", "topic")
    conn.commit()
    monkeypatch.setattr("topos.features.entities.resolver.value_label_surfaces", lambda c: frozenset({"next tuesday"}))
    with coalesced_edge_writes() as pending:
        _upsert(conn, src=owner, dst=value, edge_type="discusses")
        fact_materializer.materialize_signal_objects_to_graph(conn)
        assert conn.execute("SELECT 1 FROM entities WHERE entity_id=?", (value,)).fetchone()
        pending.flush(conn)
    fact_materializer.materialize_signal_objects_to_graph(conn)
    assert conn.execute("SELECT 1 FROM entities WHERE entity_id=?", (value,)).fetchone(), "an edge holds it"


# --- vertices ------------------------------------------------------------------------------------


def _node(conn, node_id):
    return conn.execute(
        "SELECT canonical_name, metadata_json, first_seen, last_seen, updated_at FROM entities WHERE entity_id=?",
        (node_id,)).fetchone()


def test_a_derived_node_is_rewritten_only_when_its_label_or_metadata_moved(conn):
    graph_enrichers._ensure_node(conn, "goal_x", "Sail", "goal", metadata={"occurrences": 2},
                                 first_at="2026-01-01", last_at="2026-01-05")
    # The communities pass stamps it afterwards.
    conn.execute("UPDATE entities SET metadata_json=json_patch(metadata_json, ?), updated_at='2000-01-01 00:00:00' "
                 "WHERE entity_id='goal_x'", (json.dumps({"community_id": 3, "centrality": {"degree": 1}}),))
    written, _ = _changes(conn, graph_enrichers._ensure_node, conn, "goal_x", "Sail", "goal",
                          metadata={"occurrences": 2}, first_at="2026-01-02", last_at="2026-01-04")
    assert written == 0, "same label, same metadata, a window inside the stored one"
    assert _node(conn, "goal_x")[4] == "2000-01-01 00:00:00"

    written, _ = _changes(conn, graph_enrichers._ensure_node, conn, "goal_x", "Sail", "goal",
                          metadata={"occurrences": 3}, first_at="2026-01-02", last_at="2026-01-04")
    assert written == 1
    meta = json.loads(_node(conn, "goal_x")[1])
    assert meta == {"mz": 1, "occurrences": 3}, "a changed node is written as always: stamps dropped for the pass"


def test_a_window_is_widened_only_when_it_grows(conn):
    graph_enrichers._ensure_node(conn, "conv_x", "conv x", "conversation", first_at="2026-01-02", last_at="2026-01-05")
    written, _ = _changes(conn, widen_node_window, conn, "conv_x", "2026-01-03", "2026-01-04")
    assert written == 0
    written, _ = _changes(conn, widen_node_window, conn, "conv_x", "2026-01-01", "2026-01-04")
    assert written == 1 and _node(conn, "conv_x")[2:4] == ("2026-01-01", "2026-01-05")


def test_community_stamps_are_written_only_when_they_change():
    assert maintenance._patch_changes_nothing(
        '{"mz":1,"community_id":2,"centrality":{"degree":3,"eigen":0.5,"betweenness":0.0},"community_label":"Crew"}',
        {"community_id": 2, "centrality": {"degree": 3, "eigen": 0.5, "betweenness": 0.0}, "community_label": "Crew"},
    )
    assert maintenance._patch_changes_nothing('{"community_id": 2}', {"community_id": 2, "community_label": None})
    assert not maintenance._patch_changes_nothing('{"community_id": 2}', {"community_id": 3})
    assert not maintenance._patch_changes_nothing(
        '{"community_id": 2, "community_label": "Crew"}', {"community_id": 2, "community_label": None})
    assert not maintenance._patch_changes_nothing("{}", {"community_id": 0})
    assert not maintenance._patch_changes_nothing("not json", {"community_id": 0})
    assert not maintenance._patch_changes_nothing(None, {"community_id": 0})


def test_the_recount_writes_only_entities_whose_counts_or_window_moved(conn):
    r = EntityResolver(conn)
    a, b = r._create_entity("Ada", "person"), r._create_entity("Bram", "person")
    conn.execute(
        "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, canonical_table, surface_text, "
        "confidence, event_at, created_at) VALUES ('m1', ?, 'r1', 's', 'conversation_messages', 'x', 0.9, "
        "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')", (a,))
    conn.commit()
    _recount_entity_mentions(conn)
    written, recounted = _changes(conn, _recount_entity_mentions, conn)
    assert written == 0 and recounted == 2, "every entity is still counted as recounted"
    conn.execute(
        "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, canonical_table, surface_text, "
        "confidence, event_at, created_at) VALUES ('m2', ?, 'r2', 's', 'conversation_messages', 'x', 0.9, "
        "'2026-02-01T00:00:00Z', '2026-02-01T00:00:00Z')", (b,))
    written, _ = _changes(conn, _recount_entity_mentions, conn)
    assert written == 2  # Bram's count, then Bram's window
    assert conn.execute("SELECT mention_count, first_seen FROM entities WHERE entity_id=?", (b,)).fetchone() == (
        1, "2026-02-01T00:00:00Z")


def test_contact_seeding_rewrites_a_person_only_when_its_identifiers_moved(conn):
    conn.execute("INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self, known_usernames_json) "
                 "VALUES ('c1', 'ds', 'contacts', 'Ada Quill', 0, '[]')")
    conn.execute("INSERT INTO contact_identifiers (contact_id, identifier, dataset_id, source_id) "
                 "VALUES ('c1', 'ada@example.test', 'ds', 'contacts')")
    conn.commit()
    r = EntityResolver(conn)
    r.seed_from_contacts()
    written, _ = _changes(conn, r.seed_from_contacts)
    assert written == 0
    conn.execute("INSERT INTO contact_identifiers (contact_id, identifier, dataset_id, source_id) "
                 "VALUES ('c1', 'ada.q@example.test', 'ds', 'contacts')")
    written, _ = _changes(conn, r.seed_from_contacts)
    assert written == 1
    assert "ada.q@example.test" in json.loads(conn.execute(
        "SELECT identifiers_json FROM entities WHERE contact_id='c1'").fetchone()[0])
