"""`read_person_graph` pays for structure and the heard-about rail only when asked.

Measured on a copy of the owner's database (2,893 nodes, 28 Oct 2026): 10.2 s warm, of
which `structural_metrics` 3.4 s, `shared_context_affinity` 1.4 s and `attach_heard_about`
4.1 s. Every caller that asked before still gets them; a caller that wants the nodes alone
turns them off and gets 1.5 s. Off must mean not computed, said so, and nothing else
changed.
"""

from __future__ import annotations

import json
import sqlite3

from topos.analytics import person_graph as PG
from topos.analytics import relationship_reads as reads

DS = "ds"


def _corpus():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript("""
      CREATE TABLE entities (entity_id TEXT PRIMARY KEY, entity_type TEXT,
        canonical_name TEXT, normalized_name TEXT, aliases_json TEXT, is_self INTEGER,
        contact_id TEXT);
      CREATE TABLE entity_edges (edge_id TEXT, src_entity_id TEXT, dst_entity_id TEXT,
        edge_type TEXT, weight REAL, metadata_json TEXT);
      CREATE TABLE entity_mentions (mention_id TEXT PRIMARY KEY, entity_id TEXT,
        record_id TEXT, source_id TEXT, authored_by_owner INTEGER);
      CREATE TABLE messenger_dyad_stats (dataset_id TEXT, a_key TEXT, b_key TEXT,
        involves_self INTEGER, peer_class TEXT, total_msgs INTEGER);
      CREATE TABLE contacts (contact_id TEXT PRIMARY KEY, display_name TEXT,
        is_self INTEGER DEFAULT 0);
      CREATE TABLE contact_identifiers (contact_id TEXT, identifier TEXT, identifier_type TEXT);
      CREATE TABLE signal_objects (object_id TEXT PRIMARY KEY, object_type TEXT,
        payload_json TEXT, confidence REAL, source_refs_json TEXT, valid_from TEXT,
        valid_to TEXT);
      CREATE TABLE conversation_messages (dataset_id TEXT, message_id TEXT, content TEXT,
        is_from_self INTEGER, conversation_id TEXT, sender_id TEXT, event_at TEXT,
        source_id TEXT, reply_to_message_id TEXT);
      CREATE TABLE messenger_social_edges (dataset_id TEXT, period_key TEXT,
        source_scope TEXT, source_id TEXT, target_id TEXT, weight REAL, edge_type TEXT);
    """)
    c.execute("INSERT INTO entities VALUES ('e-owner','person','Owner','owner','[]',1,NULL)")
    for i, name in enumerate(("Ava Lindqvist", "Bo Ferreira", "Cy Okafor")):
        c.execute("INSERT INTO contacts VALUES (?,?,0)", (f"c-{i}", name))
        c.execute("INSERT INTO contact_identifiers VALUES (?,?,'phone')", (f"c-{i}", f"+1555000000{i}"))
        c.execute("INSERT INTO entities VALUES (?,?,?,?,?,0,?)",
                  (f"e-{i}", "person", name, name.lower(), "[]", f"c-{i}"))
        c.execute("INSERT INTO messenger_dyad_stats VALUES (?,?,?,1,'human',?)",
                  (DS, "self", f"+1555000000{i}", 100 * (3 - i)))
        c.execute("INSERT INTO entity_mentions VALUES (?,?,?,?,1)", (f"m-{i}", f"e-{i}", f"r-{i}", "grow_journal"))
    c.commit()
    return c


def _recording(monkeypatch):
    calls = []
    for name in ("structural_metrics", "shared_context_affinity", "attach_heard_about"):
        real = getattr(PG, name)

        def wrapped(*args, _name=name, _real=real, **kwargs):
            calls.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(PG, name, wrapped)
    return calls


def test_structure_and_the_heard_about_rail_are_computed_unless_turned_off(monkeypatch):
    c = _corpus()
    calls = _recording(monkeypatch)
    on = reads.read_person_graph(c, dataset_id=DS)
    assert sorted(calls) == ["attach_heard_about", "shared_context_affinity", "structural_metrics"]
    assert all("community_id" in n and "centrality_degree" in n for n in on["nodes"])
    assert "skipped" not in on["structure"] and "skipped" not in on["heard_about"]

    calls.clear()
    off = reads.read_person_graph(c, dataset_id=DS, include_structure=False, include_heard_about=False)
    assert calls == []
    assert off["structure"] == {"skipped": True, "reason": "structure not requested"}
    assert off["context_affinity"] == [] and off["context_coverage"]["skipped"] is True
    assert off["heard_about"] == {"attached": 0, "events": 0, "skipped": True}
    for n in off["nodes"]:
        for key in ("community_id", "centrality_degree", "centrality_betweenness",
                    "brokerage_meaningful", "reach", "heard_about"):
            assert key not in n, key
    # everything else is what it was
    assert [n["node_id"] for n in off["nodes"]] == [n["node_id"] for n in on["nodes"]]
    assert off["counts"] == on["counts"] and off["bands"] == on["bands"]
    assert len(off["edges"]) == len(on["edges"])
    json.dumps(off)


def test_each_flag_turns_off_its_own_part_alone(monkeypatch):
    c = _corpus()
    calls = _recording(monkeypatch)
    reads.read_person_graph(c, dataset_id=DS, include_structure=False)
    assert calls == ["attach_heard_about"]
    calls.clear()
    out = reads.read_person_graph(c, dataset_id=DS, include_heard_about=False)
    assert sorted(calls) == ["shared_context_affinity", "structural_metrics"]
    assert out["heard_about"]["skipped"] is True and "skipped" not in out["structure"]


def test_provenance_reads_the_nodes_alone(monkeypatch):
    """One node is wanted; 7.5 of the graph's 10 s said nothing a provenance answer reads."""
    seen = {}

    def record(conn, **kwargs):
        seen.update(kwargs)
        return {"nodes": []}

    monkeypatch.setattr(reads, "read_person_graph", record)
    out = reads.read_person_provenance(_corpus(), dataset_id=DS, node_id="ent:nobody")
    assert out["error"] == "no such person on this graph"
    assert seen["include_structure"] is False and seen["include_heard_about"] is False
    assert seen["include_dismissed"] is True
