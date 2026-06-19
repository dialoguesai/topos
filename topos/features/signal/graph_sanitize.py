"""Ensure graph edge endpoints exist as nodes (force-graph / d3 require this)."""

from __future__ import annotations

from typing import Any, Dict, List, Tuple


def _node_id(raw: Dict[str, Any]) -> str:
    return str(raw.get("node_id") or raw.get("id") or "").strip()


def _infer_node_type(node_id: str) -> str:
    if node_id.startswith("contact:"):
        return "contact"
    if node_id.startswith("conversation:"):
        return "conversation"
    if node_id.startswith("topic:"):
        return "topic"
    return "entity"


def _infer_node_label(node_id: str) -> str:
    if node_id == "contact:unknown":
        return "Unknown sender"
    if node_id.startswith("conversation:"):
        tail = node_id.split(":", 1)[-1]
        if tail.startswith("chatgpt:"):
            return f"Chat {tail.split(':')[-1][:8]}"
        return tail[:24] or node_id
    if ":" in node_id:
        return node_id.split(":", 1)[-1][:48] or node_id
    return node_id


def _stub_node(node_id: str, *, source_id: str | None = None) -> Dict[str, Any]:
    stub: Dict[str, Any] = {
        "node_id": node_id,
        "node_type": _infer_node_type(node_id),
        "label": _infer_node_label(node_id),
        "synthetic": True,
    }
    if source_id:
        stub["source_id"] = source_id
    return stub


def ensure_graph_endpoints(
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Add stub nodes for any edge src/dst missing from the node list."""
    known = {_node_id(node) for node in nodes if _node_id(node)}
    out_nodes = list(nodes)
    for edge in edges:
        for key in ("src_node_id", "dst_node_id", "source", "target"):
            endpoint = str(edge.get(key) or "").strip()
            if not endpoint or endpoint in known:
                continue
            known.add(endpoint)
            out_nodes.append(_stub_node(endpoint, source_id=edge.get("source_id")))
    return out_nodes, edges
