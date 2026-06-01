"""Messenger graph importance and community detection (Sprint 02)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import networkx as nx

from .messenger_graph import extract_messenger_graph

MESSENGER_SOCIAL_EDGES_TABLE = "messenger_social_edges"
MESSENGER_PARTICIPANT_IMPORTANCE_TABLE = "messenger_participant_importance"
MESSENGER_COMMUNITIES_TABLE = "messenger_communities"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _source_scope(source_ids: Optional[Sequence[str]]) -> str:
    if not source_ids:
        return "all"
    normalized = sorted({str(s).strip() for s in source_ids if str(s).strip()})
    return ",".join(normalized) if normalized else "all"


def ensure_messenger_analytics_tables(conn: Any) -> None:
    """Create Sprint 02 derived messenger analytics tables."""
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MESSENGER_SOCIAL_EDGES_TABLE} (
            dataset_id TEXT NOT NULL,
            period_key TEXT NOT NULL,
            source_scope TEXT NOT NULL DEFAULT 'all',
            source_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            weight REAL NOT NULL,
            edge_type TEXT,
            edge_type_counts_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (dataset_id, period_key, source_scope, source_id, target_id)
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{MESSENGER_SOCIAL_EDGES_TABLE}_dataset_period
        ON {MESSENGER_SOCIAL_EDGES_TABLE}(dataset_id, period_key, source_scope)
        """
    )

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE} (
            dataset_id TEXT NOT NULL,
            period_key TEXT NOT NULL,
            source_scope TEXT NOT NULL DEFAULT 'all',
            participant_id TEXT NOT NULL,
            centrality_degree REAL NOT NULL,
            centrality_betweenness REAL NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (dataset_id, period_key, source_scope, participant_id)
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{MESSENGER_PARTICIPANT_IMPORTANCE_TABLE}_dataset_period
        ON {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE}(dataset_id, period_key, source_scope)
        """
    )

    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MESSENGER_COMMUNITIES_TABLE} (
            dataset_id TEXT NOT NULL,
            period_key TEXT NOT NULL,
            source_scope TEXT NOT NULL DEFAULT 'all',
            participant_id TEXT NOT NULL,
            community_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (dataset_id, period_key, source_scope, participant_id)
        )
        """
    )
    conn.execute(
        f"""
        CREATE INDEX IF NOT EXISTS idx_{MESSENGER_COMMUNITIES_TABLE}_dataset_period
        ON {MESSENGER_COMMUNITIES_TABLE}(dataset_id, period_key, source_scope)
        """
    )
    conn.commit()


def build_networkx_graph(period_payload: Dict[str, Any]) -> nx.Graph:
    """Build an undirected weighted graph from Sprint 01 period payload."""
    graph = nx.Graph()
    for node in period_payload.get("nodes", []):
        node_id = str(node.get("id") or "").strip()
        if not node_id:
            continue
        graph.add_node(node_id, **node)

    for edge in period_payload.get("edges", []):
        source_id = str(edge.get("source") or "").strip()
        target_id = str(edge.get("target") or "").strip()
        if not source_id or not target_id or source_id == target_id:
            continue
        weight = float(edge.get("weight") or 0.0)
        graph.add_edge(source_id, target_id, weight=max(weight, 0.0))
    return graph


def compute_importance_and_communities(period_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Compute centrality metrics and Louvain communities for one period."""
    graph = build_networkx_graph(period_payload)
    node_ids = list(graph.nodes())
    if not node_ids:
        return {"importance": {}, "communities": {}, "graph": graph}

    degree = nx.degree_centrality(graph)
    # Use unweighted betweenness to keep metric stable with strength-based edges.
    betweenness = nx.betweenness_centrality(graph, weight=None, normalized=True)

    if graph.number_of_edges() > 0:
        try:
            from community import community_louvain  # type: ignore

            communities = community_louvain.best_partition(graph, weight="weight", random_state=42)
        except Exception:
            # Fallback keeps pipeline functional if python-louvain is unavailable at runtime.
            communities = {}
            for idx, component in enumerate(nx.connected_components(graph)):
                for node_id in component:
                    communities[node_id] = idx
    else:
        communities = {node_id: idx for idx, node_id in enumerate(sorted(node_ids))}

    importance: Dict[str, Dict[str, float]] = {}
    for node_id in node_ids:
        importance[node_id] = {
            "centrality_degree": float(degree.get(node_id, 0.0)),
            "centrality_betweenness": float(betweenness.get(node_id, 0.0)),
        }
    return {
        "importance": importance,
        "communities": {k: int(v) for k, v in communities.items()},
        "graph": graph,
    }


def _persist_period_results(
    conn: Any,
    *,
    dataset_id: str,
    period_key: str,
    source_scope: str,
    period_payload: Dict[str, Any],
    importance: Dict[str, Dict[str, float]],
    communities: Dict[str, int],
) -> Dict[str, int]:
    now = _utc_now()
    conn.execute(
        f"""
        DELETE FROM {MESSENGER_SOCIAL_EDGES_TABLE}
        WHERE dataset_id = ? AND period_key = ? AND source_scope = ?
        """,
        (dataset_id, period_key, source_scope),
    )
    conn.execute(
        f"""
        DELETE FROM {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE}
        WHERE dataset_id = ? AND period_key = ? AND source_scope = ?
        """,
        (dataset_id, period_key, source_scope),
    )
    conn.execute(
        f"""
        DELETE FROM {MESSENGER_COMMUNITIES_TABLE}
        WHERE dataset_id = ? AND period_key = ? AND source_scope = ?
        """,
        (dataset_id, period_key, source_scope),
    )

    edge_rows = []
    for edge in period_payload.get("edges", []):
        source_id = str(edge.get("source") or "").strip()
        target_id = str(edge.get("target") or "").strip()
        if not source_id or not target_id:
            continue
        edge_rows.append(
            (
                dataset_id,
                period_key,
                source_scope,
                source_id,
                target_id,
                float(edge.get("weight") or 0.0),
                str(edge.get("edge_type") or ""),
                json.dumps(edge.get("edge_type_counts") or {}, ensure_ascii=False),
                now,
                now,
            )
        )
    if edge_rows:
        conn.executemany(
            f"""
            INSERT INTO {MESSENGER_SOCIAL_EDGES_TABLE}
            (
                dataset_id, period_key, source_scope, source_id, target_id,
                weight, edge_type, edge_type_counts_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            edge_rows,
        )

    importance_rows = []
    for participant_id, metrics in importance.items():
        importance_rows.append(
            (
                dataset_id,
                period_key,
                source_scope,
                participant_id,
                float(metrics.get("centrality_degree", 0.0)),
                float(metrics.get("centrality_betweenness", 0.0)),
                now,
                now,
            )
        )
    if importance_rows:
        conn.executemany(
            f"""
            INSERT INTO {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE}
            (
                dataset_id, period_key, source_scope, participant_id,
                centrality_degree, centrality_betweenness, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            importance_rows,
        )

    community_rows = []
    for participant_id, community_id in communities.items():
        community_rows.append(
            (
                dataset_id,
                period_key,
                source_scope,
                participant_id,
                int(community_id),
                now,
                now,
            )
        )
    if community_rows:
        conn.executemany(
            f"""
            INSERT INTO {MESSENGER_COMMUNITIES_TABLE}
            (
                dataset_id, period_key, source_scope, participant_id,
                community_id, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            community_rows,
        )
    conn.commit()
    return {
        "edges_written": len(edge_rows),
        "importance_written": len(importance_rows),
        "communities_written": len(community_rows),
    }


def compute_and_persist_messenger_analytics(
    *,
    dataset_id: str,
    conn: Optional[Any] = None,
    start_ts: Optional[str] = None,
    end_ts: Optional[str] = None,
    source_ids: Optional[Sequence[str]] = None,
    period_granularity: str = "month",
    cumulative: bool = False,
) -> Dict[str, Any]:
    """Run Sprint 01 extraction + Sprint 02 metrics and persist derived analytics."""
    if conn is not None:
        db = conn
    else:
        from ..core.state import get_db_connection

        db = get_db_connection()
    if db is None:
        raise RuntimeError("Database connection not available")

    ensure_messenger_analytics_tables(db)
    extraction = extract_messenger_graph(
        dataset_id=dataset_id,
        conn=db,
        start_ts=start_ts,
        end_ts=end_ts,
        source_ids=source_ids,
        period_granularity=period_granularity,
        cumulative=cumulative,
    )

    scope = _source_scope(source_ids)
    periods_out: List[Dict[str, Any]] = []
    totals = {"edges_written": 0, "importance_written": 0, "communities_written": 0}

    for period_payload in extraction.get("periods", []):
        period_key = str(period_payload.get("period_key") or "")
        if not period_key:
            continue
        computed = compute_importance_and_communities(period_payload)
        writes = _persist_period_results(
            db,
            dataset_id=dataset_id,
            period_key=period_key,
            source_scope=scope,
            period_payload=period_payload,
            importance=computed["importance"],
            communities=computed["communities"],
        )
        for key in totals:
            totals[key] += writes[key]
        periods_out.append(
            {
                "period_key": period_key,
                **writes,
                "nodes_count": len(period_payload.get("nodes", [])),
                "edges_count": len(period_payload.get("edges", [])),
            }
        )

    return {
        "dataset_id": dataset_id,
        "period_granularity": period_granularity,
        "source_scope": scope,
        "periods": periods_out,
        "totals": totals,
    }
