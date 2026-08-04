"""openCypher over the entity graph (PLAN_GRAPH_QUERY_AND_LATENT_EDGES §4).

Covers the three things the milestone claims: multi-hop MATCH returns the
expected paths on a fixture graph, a malformed query is a clean error rather
than a traceback, and result rows leave through the engine's vector gate.

The §4.1 owner-only leak gate lives in tests/test_protocol_handled_types.py,
next to the marker it asserts against.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.entities.cypher import (
    MAX_QUERY_CHARS,
    CypherQueryError,
    build_entity_graph,
    run_cypher,
)
from topos.features.entities.edges import EDGE_SEMANTIC_AFFINITY, update_edge
from topos.features.entities.resolver import EntityResolver
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "cypher.db"))
    apply_all_migrations(c)
    yield c
    c.close()


@pytest.fixture()
def graph_ids(conn):
    """Owner -> Ada -> Bram co-occurrence chain, plus Ada ~ Cass affinity.

    Cass never co-occurs with anyone: she is only reachable through the
    semantic_affinity edge, which is the traversal §4 exists to make possible.
    """
    resolver = EntityResolver(conn)
    owner = resolver._create_entity("Owner", "person")
    ada = resolver._create_entity("Ada", "person")
    bram = resolver._create_entity("Bram", "person")
    cass = resolver._create_entity("Cass", "person")
    acme = resolver._create_entity("Acme", "org")
    conn.execute("UPDATE entities SET is_self=1 WHERE entity_id=?", (owner,))
    conn.commit()

    update_edge(conn, src_entity_id=owner, dst_entity_id=ada, edge_type="co_occurrence")
    update_edge(conn, src_entity_id=ada, dst_entity_id=bram, edge_type="co_occurrence")
    update_edge(conn, src_entity_id=bram, dst_entity_id=acme, edge_type="part_of")
    # Affinity weight is a bounded cosine snapshot (§3.2); update_edge is only
    # standing in for the real rebuild writer to seed one row.
    update_edge(
        conn,
        src_entity_id=ada,
        dst_entity_id=cass,
        edge_type=EDGE_SEMANTIC_AFFINITY,
        increment=0.82,
    )
    conn.commit()
    return {"owner": owner, "ada": ada, "bram": bram, "cass": cass, "acme": acme}


def _column(result, name):
    return [row[name] for row in result["rows"]]


class TestGraphMaterialisation:
    def test_active_edges_only(self, conn, graph_ids):
        from topos.features.entities.edges import supersede_edge

        supersede_edge(
            conn,
            src_entity_id=graph_ids["bram"],
            dst_entity_id=graph_ids["acme"],
            edge_type="part_of",
        )
        conn.commit()
        graph = build_entity_graph(conn)
        assert not graph.has_edge(graph_ids["bram"], graph_ids["acme"])
        assert graph.has_edge(graph_ids["ada"], graph_ids["bram"])

    def test_symmetric_edges_are_traversable_both_ways(self, conn, graph_ids):
        """Storage keeps one canonically-ordered row; a traversal must not care."""
        graph = build_entity_graph(conn)
        assert graph.has_edge(graph_ids["ada"], graph_ids["cass"])
        assert graph.has_edge(graph_ids["cass"], graph_ids["ada"])
        # part_of is directed and must stay that way.
        assert graph.has_edge(graph_ids["bram"], graph_ids["acme"])
        assert not graph.has_edge(graph_ids["acme"], graph_ids["bram"])

    def test_nodes_carry_labels_and_names(self, conn, graph_ids):
        graph = build_entity_graph(conn)
        ada = graph.nodes[graph_ids["ada"]]
        assert ada["canonical_name"] == "Ada"
        assert ada["entity_type"] == "person"
        assert ada["__labels__"] == {"person"}
        assert graph.nodes[graph_ids["owner"]]["is_self"] is True


class TestMultiHopMatch:
    def test_two_hop_path_returns_expected_nodes(self, conn, graph_ids):
        result = run_cypher(
            conn,
            """
            MATCH (a)-[:co_occurrence]->(b)-[:co_occurrence]->(c)
            WHERE a.canonical_name == "Owner"
            RETURN a.canonical_name, b.canonical_name, c.canonical_name
            """,
        )
        paths = {
            (r["a.canonical_name"], r["b.canonical_name"], r["c.canonical_name"])
            for r in result["rows"]
        }
        assert ("Owner", "Ada", "Bram") in paths
        assert result["graph"]["nodes"] == 5

    def test_variable_length_match_reaches_three_hops(self, conn, graph_ids):
        result = run_cypher(
            conn,
            """
            MATCH (a)-[r*1..3]->(b)
            WHERE a.canonical_name == "Owner"
            RETURN b.canonical_name
            """,
        )
        assert {"Ada", "Bram"} <= set(_column(result, "b.canonical_name"))

    def test_affinity_hop_reaches_a_non_co_occurring_entity(self, conn, graph_ids):
        """The §4 payoff: Cass is unreachable except through semantic_affinity."""
        result = run_cypher(
            conn,
            """
            MATCH (me)-[:co_occurrence]->(known)-[aff:semantic_affinity]->(unknown)
            WHERE me.canonical_name == "Owner"
            RETURN known.canonical_name, unknown.canonical_name, aff.weight
            """,
        )
        assert result["row_count"] == 1
        row = result["rows"][0]
        assert row["known.canonical_name"] == "Ada"
        assert row["unknown.canonical_name"] == "Cass"
        assert row["aff.weight"] == pytest.approx(0.82)

    def test_result_is_json_serialisable(self, conn, graph_ids):
        result = run_cypher(
            conn,
            "MATCH (a)-[r:semantic_affinity]->(b) RETURN a, r, b",
        )
        json.dumps(result)  # edge buckets and __labels__ sets would both blow up here
        row = result["rows"][0]
        assert row["a"] in (graph_ids["ada"], graph_ids["cass"])
        assert row["r"]["edge_type"] == "semantic_affinity"
        assert row["r"]["labels"] == ["semantic_affinity"]

    def test_limit_truncates_and_reports(self, conn, graph_ids):
        result = run_cypher(conn, "MATCH (a) RETURN a.canonical_name", limit=2)
        assert result["row_count"] == 2
        assert result["truncated"] is True


class TestMalformedQueries:
    @pytest.mark.parametrize(
        "query",
        [
            "MATCH bogus (((",
            "SELECT * FROM entities",
            "MATCH (a) RETURN",
            "MATCH (a)-[r]->(b) WHERE a.canonical_name = 'single quotes' RETURN a",
        ],
    )
    def test_malformed_query_raises_cypher_query_error(self, conn, graph_ids, query):
        with pytest.raises(CypherQueryError):
            run_cypher(conn, query)

    def test_empty_query_is_rejected(self, conn, graph_ids):
        with pytest.raises(CypherQueryError):
            run_cypher(conn, "   ")

    def test_oversized_query_is_rejected(self, conn, graph_ids):
        with pytest.raises(CypherQueryError):
            run_cypher(conn, "MATCH (a) RETURN a.canonical_name -- " + "x" * MAX_QUERY_CHARS)

    def test_error_message_is_a_single_line(self, conn, graph_ids):
        with pytest.raises(CypherQueryError) as excinfo:
            run_cypher(conn, "MATCH bogus (((")
        assert "\n" not in str(excinfo.value)


class TestVectorGate:
    """Result rows pass through the engine's existing vector gate on the way out."""

    def test_vector_shaped_columns_are_dropped(self, conn, graph_ids, monkeypatch):
        import networkx as nx

        import topos.features.entities.cypher as cypher_mod

        def _leaky_graph(_conn, **_kwargs):
            graph = nx.MultiDiGraph()
            graph.add_node(
                "e1",
                __labels__={"person"},
                canonical_name="Ada",
                embedding_blob=b"\x00\x01",
                centroid_blob=b"\x02\x03",
                context_centroid=[0.1, 0.2],
            )
            graph.add_node("e2", __labels__={"person"}, canonical_name="Bram")
            graph.add_edge("e1", "e2", __labels__={"co_occurrence"}, weight=1.0)
            return graph

        monkeypatch.setattr(cypher_mod, "build_entity_graph", _leaky_graph)

        result = run_cypher(
            conn,
            "MATCH (a)-[r]->(b) RETURN a.canonical_name, a.embedding_blob, a.context_centroid",
        )
        assert result["columns"] == ["a.canonical_name"]
        assert all(set(row) == {"a.canonical_name"} for row in result["rows"])

    def test_whole_element_return_is_stripped(self, conn, graph_ids, monkeypatch):
        """``RETURN r`` hands back every attribute of the element at once."""
        import networkx as nx

        import topos.features.entities.cypher as cypher_mod

        def _leaky_graph(_conn, **_kwargs):
            graph = nx.MultiDiGraph()
            graph.add_node("e1", __labels__={"person"}, canonical_name="Ada")
            graph.add_node("e2", __labels__={"person"}, canonical_name="Bram")
            graph.add_edge(
                "e1",
                "e2",
                __labels__={"co_occurrence"},
                edge_type="co_occurrence",
                weight=1.0,
                embedding_blob=b"\x00\x01",
                centroid_blob=b"\x02\x03",
                pair_centroid=[0.1, 0.2],
            )
            return graph

        monkeypatch.setattr(cypher_mod, "build_entity_graph", _leaky_graph)

        result = run_cypher(conn, "MATCH (a)-[r]->(b) RETURN r")
        returned = result["rows"][0]["r"]
        assert returned["edge_type"] == "co_occurrence"
        assert returned["labels"] == ["co_occurrence"]
        assert not {"embedding_blob", "centroid_blob", "pair_centroid"} & set(returned)
