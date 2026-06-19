"""Graph endpoint sanitization for force-graph consumers."""

from topos.features.signal.graph_sanitize import ensure_graph_endpoints


def test_ensure_graph_endpoints_adds_contact_unknown_stub() -> None:
    nodes = [{"node_id": "entity:SQL", "node_type": "entity", "label": "SQL"}]
    edges = [
        {
            "edge_id": "e1",
            "src_node_id": "contact:unknown",
            "dst_node_id": "conversation:chatgpt:abc123",
            "edge_type": "message_frequency",
            "weight": 1.0,
        }
    ]
    out_nodes, out_edges = ensure_graph_endpoints(nodes, edges)
    node_ids = {n["node_id"] for n in out_nodes}
    assert "contact:unknown" in node_ids
    assert "conversation:chatgpt:abc123" in node_ids
    assert out_nodes[-2]["label"] == "Unknown sender"
    assert len(out_edges) == 1
