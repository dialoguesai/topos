"""Extracting an entity must not mint a row in the retired graph store.

The entities job called ``upsert_node`` with no ``node_id``, so
``SQLiteGraphEdgeStore.upsert_node`` minted a fresh uuid4 for EVERY mention.
Nothing ever resolved those ids. Measured on the owner's node 2026-08-27:

  * 32,631 ``graph_nodes`` rows of type ``entity``;
  * **0** matched a spine ``entity_id``;
  * **0** were referenced by any edge — only 365 of 32,996 nodes were reachable
    at all, and all 3,866 edges belong to the messenger ``message_frequency``
    projection between contact/conversation nodes.

It was also writing into a store the codebase has already retired.
``lifecycle/gc.py`` declares ``graph_nodes`` "superseded by entity graph
(entities + entity_edges)" and ``graph_edges`` "superseded by entity_edges", and
the product read path is ``entities/reads.py`` -> ``edges.graph_snapshot``.

That is why "graph edges carry no record provenance (0 of 3,826)" does not want
the obvious fix. Adding ``record_id`` to these rows would have been work in the
direction of a store on its way out; the entity graph already carries the real
thing — ``entity_mentions`` links every entity to its record, and ``entity_edges``
carries validity and evidence counts.

The contact/conversation writes are deliberately untouched: those pass an
explicit ``node_id``, form a connected graph, and are what the legacy
``signal_list_graph`` route still serves.

A RESOLVED identity is also still written, and that is not an exception to the
rule but the rule itself. PRD_04 ("relationship edges use person_id") asks for a
graph keyed on resolved people; ``tests/gap/remediation/test_r04_…`` covers it and
was PASSING on the strength of the invented uuids, while the ``person_id`` it
carefully resolved was never read by the writer. The uuid4 path produced orphans
precisely because it had no identity to key on and minted one anyway. With an
identity, the node is joinable and stable across mentions; without one, there is
nothing to write.
"""

from __future__ import annotations

import pytest


class _RecordingGraph:
    def __init__(self):
        self.nodes = []
        self.edges = []

    def upsert_node(self, node):
        self.nodes.append(node)
        return node.get("node_id") or "minted-uuid"

    def upsert_edge(self, edge):
        self.edges.append(edge)
        return "edge"


class _RecordingSignal:
    def __init__(self):
        self.facts = []

    def put_fact(self, fact):
        self.facts.append(fact)

    def put_score(self, score):
        pass


class _Adapters:
    def __init__(self):
        self.graph = _RecordingGraph()
        self.signal = _RecordingSignal()
        self.vector = None


def _write_entities(records):
    from topos.enrichment.job_writer import _write_signal_records_unlocked

    adapters = _Adapters()
    _write_signal_records_unlocked(
        "entities", records, adapters=adapters, tables_manager=None, conn=None
    )
    return adapters


def test_an_unresolved_mention_mints_no_graph_node():
    """No identity to key on means nothing to write."""
    adapters = _write_entities(
        [{"entity_text": "Ada", "record_id": "tl-1", "source_id": "grow_journal"}]
    )

    assert adapters.graph.nodes == [], (
        "every mention used to mint an unreferenced uuid node in a retired store"
    )


def test_a_resolved_mention_mints_a_node_keyed_on_the_person():
    """PRD_04: the graph uses resolved people, not invented ids."""
    adapters = _write_entities(
        [{
            "entity_text": "Alice",
            "record_id": "m1",
            "source_id": "imessage",
            "person_id": "person-abc",
        }]
    )

    assert len(adapters.graph.nodes) == 1
    node = adapters.graph.nodes[0]
    assert node["node_id"] == "person-abc", "the node must be keyed on the resolved id"
    assert node["node_type"] == "person"
    assert adapters.signal.facts[0]["node_id"] == "person-abc"


def test_a_resolved_node_is_stable_across_mentions():
    """Two mentions of one person are one node — the property uuid4 destroyed."""
    adapters = _write_entities(
        [
            {"entity_text": "Alice", "record_id": "m1", "source_id": "imessage",
             "person_id": "person-abc"},
            {"entity_text": "Alice", "record_id": "m2", "source_id": "imessage",
             "person_id": "person-abc"},
        ]
    )

    assert {n["node_id"] for n in adapters.graph.nodes} == {"person-abc"}


def test_the_fact_is_still_written():
    """Control: this removed a node write, not the extraction output."""
    adapters = _write_entities(
        [{"entity_text": "Ada", "record_id": "tl-1", "source_id": "grow_journal"}]
    )

    assert len(adapters.signal.facts) == 1
    assert adapters.signal.facts[0]["entity_text"] == "Ada"
    assert adapters.signal.facts[0]["record_id"] == "tl-1"


def test_the_fact_carries_no_dead_node_id():
    """A key holding an id that resolves to nothing reads as provenance.

    32,039 signal_facts on the owner's node carry a ``node_id``, and every value
    points at an orphan. Dropping the key is more honest than writing ``None``,
    which a reader cannot distinguish from "not yet resolved".
    """
    adapters = _write_entities(
        [{"entity_text": "Ada", "record_id": "tl-1", "source_id": "grow_journal"}]
    )

    assert "node_id" not in adapters.signal.facts[0]


def test_a_record_keeps_its_real_provenance():
    """The entity graph carries what the graph store never did.

    ``record_id`` on the fact plus ``entity_mentions`` is the link to the source
    record — which is exactly what the retired store was supposed to provide and
    did not.
    """
    adapters = _write_entities(
        [{"entity_text": "Ada", "record_id": "tl-1", "source_id": "grow_journal"}]
    )

    fact = adapters.signal.facts[0]
    assert fact["record_id"] == "tl-1"
    assert fact["source_id"] == "grow_journal"


def test_an_entity_with_no_text_is_skipped():
    adapters = _write_entities([{"record_id": "tl-1", "source_id": "grow_journal"}])

    assert adapters.signal.facts == []
    assert adapters.graph.nodes == []


def test_the_graph_store_is_declared_deprecated():
    """Pins the reason. If these come OUT of DEPRECATED_TABLES, revisit this file.

    Removing the write is only correct while the store is retiring. If the graph
    tables are un-deprecated the way ``relationship_edges`` was on 2026-08-26,
    this decision needs making again rather than inheriting.
    """
    from topos.features.lifecycle.gc import DEPRECATED_TABLES

    assert "graph_nodes" in DEPRECATED_TABLES
    assert "graph_edges" in DEPRECATED_TABLES
