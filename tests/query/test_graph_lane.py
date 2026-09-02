"""S6 (PLAN_QUERY_LOOP.md) — the graph retrieval lane.

protects: relations that exist in the graph are consulted at query time —
and only for the owner. Before this lane, one edge type of 24 had a reader;
"Who works on this with me?" returned topic-cluster fragments while the
project's edges sat unread. The integration test here IS the severed-wire
tripwire: removing the ("graph", …) tuple from the fusion table reds it.

Privacy pins: the lane never runs (and never writes a ledger receipt) below
owner_raw; a black-holed entity arrives STAMPED for the owner via the exit
wire — matchable at either endpoint (neighbor entity_id, anchor
subject_entity_id) — never silently dropped from the owner's own view.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.query.graph_lane import (
    GRAPH_LANE_MAX_ITEMS,
    graph_neighborhood_items,
)
from topos.query.manifest_validation import resolve_scope_manifest
from topos.query.narrowing import NarrowingLedger
from topos.query.retrieval import DefaultSignalRetrievalAdapter
from topos.query.types import RetrievalRequest
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "graph.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _seed_graph(conn: sqlite3.Connection) -> None:
    rows = [
        ("ent_self", "person", "Owner Person", "owner person", 50, 1),
        ("ent_topos", "project", "Topos", "topos", 20, 0),
        ("ent_ada", "person", "Ada Quill", "ada quill", 12, 0),
        ("ent_bo", "person", "Bo Marsh", "bo marsh", 7, 0),
        ("ent_ghost", "person", "Casper Veil", "casper veil", 4, 0),
    ]
    for entity_id, etype, name, norm, mentions, is_self in rows:
        conn.execute(
            """INSERT INTO entities
               (entity_id, entity_type, canonical_name, normalized_name,
                mention_count, is_self)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (entity_id, etype, name, norm, mentions, is_self),
        )
    edges = [
        ("e1", "ent_ada", "ent_topos", "participates_in", 5.0, 9),
        ("e2", "ent_bo", "ent_topos", "co_occurrence", 3.0, 6),
        ("e3", "ent_ghost", "ent_topos", "participates_in", 2.0, 3),
        ("e4", "ent_self", "ent_ada", "communicates_with", 8.0, 20),
    ]
    for edge_id, src, dst, etype, weight, evidence in edges:
        conn.execute(
            """INSERT INTO entity_edges
               (edge_id, src_entity_id, dst_entity_id, edge_type, weight,
                evidence_count, valid_from, valid_to, last_event_at)
               VALUES (?, ?, ?, ?, ?, ?, '2026-06-01T00:00:00Z', NULL,
                       '2026-08-30T00:00:00Z')""",
            (edge_id, src, dst, etype, weight, evidence),
        )
    conn.commit()


def _blackhole(conn: sqlite3.Connection, entity_id: str, name: str) -> None:
    conn.execute(
        """INSERT INTO entity_blackholes
           (blackhole_id, entity_id, normalized_name, canonical_name)
           VALUES (?, ?, ?, ?)""",
        (f"bh_{entity_id}", entity_id, name.lower(), name),
    )
    conn.commit()


def _retrieve(conn, *, tier: str = "owner_raw", scope: str = "relationship_context:read",
              ledger: NarrowingLedger | None = None):
    bundle = AdapterFactory.create("local_database", conn=conn)
    adapter = DefaultSignalRetrievalAdapter(bundle)
    manifest = resolve_scope_manifest(scope)
    return adapter.retrieve(
        RetrievalRequest(
            manifest=manifest,
            access_mode="summary",
            query_text="Who works on Topos with me?",
            disclosure_tier=tier,
            ledger=ledger,
        )
    )


def _graph_items(bundle) -> list:
    summaries = (bundle.context_packet or {}).get("summaries") or []
    return [
        i
        for i in summaries
        if str(i.get("retrieval_source") or "").startswith("graph:")
    ]


class TestGraphLaneIntegration:
    def test_edges_are_consulted_and_declared(self, conn) -> None:
        """The severed-wire tripwire: relations reach fusion AND the store is
        declared touched. Remove the fusion tuple and this reds."""
        _seed_graph(conn)
        ledger = NarrowingLedger()
        bundle = _retrieve(conn, ledger=ledger)
        graph_items = _graph_items(bundle)
        assert graph_items, "the graph lane contributed nothing"
        names = " ".join(str(i.get("summary_text") or "") for i in graph_items)
        assert "Ada Quill" in names or "Bo Marsh" in names, (
            f"neighbors missing from graph items: {names!r}"
        )
        assert "graph" in (bundle.stores_touched or []), (
            f"graph store not declared touched: {bundle.stores_touched}"
        )
        assert any(
            e.get("reason") == "graph_lane" for e in ledger.as_public()["ledger"]
        ), "the lane's contributed receipt is missing"

    def test_items_carry_both_endpoints_for_the_blackhole_wire(self, conn) -> None:
        _seed_graph(conn)
        bundle = _retrieve(conn)
        for item in _graph_items(bundle):
            assert item.get("entity_id"), "neighbor endpoint missing"
            assert item.get("subject_entity_id"), "anchor endpoint missing"

    def test_lane_is_owner_only_and_silent_below_owner_raw(self, conn) -> None:
        """No items AND no receipt below owner_raw — a receipt for a withheld
        lane is itself an existence signal."""
        _seed_graph(conn)
        ledger = NarrowingLedger()
        bundle = _retrieve(conn, tier="default_disclosure", ledger=ledger)
        assert not _graph_items(bundle)
        assert "graph" not in (bundle.stores_touched or [])
        assert not any(
            e.get("reason") == "graph_lane" for e in ledger.as_public()["ledger"]
        )

    def test_scope_without_edges_gets_no_graph(self, conn) -> None:
        _seed_graph(conn)
        bundle = _retrieve(conn, scope="work_context:read")
        assert not _graph_items(bundle)

    def test_blackholed_entity_is_stamped_for_the_owner(self, conn) -> None:
        """Red-then-green pin for the exit wire: the ghost participates in the
        project; black-holing it must not silently vanish it from the OWNER's
        view — it arrives stamped (the taint feed), matched via entity_id."""
        _seed_graph(conn)
        _blackhole(conn, "ent_ghost", "Casper Veil")
        bundle = _retrieve(conn)
        ghost_items = [
            i for i in _graph_items(bundle) if i.get("entity_id") == "ent_ghost"
        ]
        assert ghost_items, "ghost edge vanished from the owner's own view"
        assert all(i.get("blackhole_protected") for i in ghost_items), (
            "black-holed neighbor reached the owner packet without its stamp"
        )


class TestGraphLaneUnit:
    def test_cap_dedupe_and_receipt(self, conn) -> None:
        _seed_graph(conn)
        # Many extra neighbors to exercise the cap.
        for n in range(20):
            conn.execute(
                """INSERT INTO entities (entity_id, entity_type, canonical_name,
                   normalized_name, mention_count) VALUES (?, 'topic', ?, ?, 1)""",
                (f"ent_x{n}", f"Topic {n}", f"topic {n}"),
            )
            conn.execute(
                """INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id,
                   edge_type, weight, valid_from, valid_to)
                   VALUES (?, 'ent_topos', ?, 'co_occurrence', ?, '2026-06-01', NULL)""",
                (f"ex{n}", f"ent_x{n}", 0.1 + n * 0.01),
            )
        conn.commit()
        ledger = NarrowingLedger()
        manifest = resolve_scope_manifest("graph:read")
        items = graph_neighborhood_items(
            conn,
            anchor_ids=["ent_topos"],
            anchor_names={"ent_topos": "Topos"},
            scope_id="graph:read",
            manifest=manifest,
            disclosure_tier="owner_raw",
            ledger=ledger,
        )
        assert 0 < len(items) <= GRAPH_LANE_MAX_ITEMS
        keys = {(i["subject_entity_id"], i["entity_id"], i["edge_type"]) for i in items}
        assert len(keys) == len(items), "duplicate edges in lane output"
        receipts = [
            e for e in ledger.as_public()["ledger"] if e.get("reason") == "graph_lane"
        ]
        assert receipts and receipts[0].get("action") == "contributed"

    def test_tier_gate_returns_nothing_and_writes_nothing(self, conn) -> None:
        _seed_graph(conn)
        ledger = NarrowingLedger()
        manifest = resolve_scope_manifest("graph:read")
        items = graph_neighborhood_items(
            conn,
            anchor_ids=["ent_topos"],
            anchor_names={"ent_topos": "Topos"},
            scope_id="graph:read",
            manifest=manifest,
            disclosure_tier="default_disclosure",
            ledger=ledger,
        )
        assert items == []
        assert not ledger.as_public()["ledger"]
