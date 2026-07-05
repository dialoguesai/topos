"""Typed, time-decayed entity edges.

Weight update on new evidence:  w <- w * 0.5^(dt / half_life) + increment
O(1) per edge, no rescans; stale relationships fade instead of accumulating
forever (raw counts made 2021 coworkers look as close as current ones).
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

from ..stats.fold import parse_ts

DEFAULT_HALF_LIFE_DAYS = 60.0

EDGE_CO_OCCURRENCE = "co_occurrence"
EDGE_COMMUNICATES = "communicates_with"
EDGE_DISCUSSES = "discusses"
EDGE_LOCATED_AT = "located_at"
EDGE_PART_OF = "part_of"  # directed: product/sub-unit -> parent org


def update_edge(
    conn: sqlite3.Connection,
    *,
    src_entity_id: str,
    dst_entity_id: str,
    edge_type: str,
    event_at: Optional[str] = None,
    increment: float = 1.0,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
) -> None:
    if not src_entity_id or not dst_entity_id or src_entity_id == dst_entity_id:
        return
    # Undirected edge types get a canonical order so A-B and B-A share a row.
    if edge_type in (EDGE_CO_OCCURRENCE, EDGE_COMMUNICATES) and dst_entity_id < src_entity_id:
        src_entity_id, dst_entity_id = dst_entity_id, src_entity_id

    row = conn.execute(
        """
        SELECT edge_id, weight, evidence_count, last_event_at FROM entity_edges
        WHERE src_entity_id=? AND dst_entity_id=? AND edge_type=?
        """,
        (src_entity_id, dst_entity_id, edge_type),
    ).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO entity_edges (
                edge_id, src_entity_id, dst_entity_id, edge_type,
                weight, evidence_count, last_event_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (
                f"edg_{uuid.uuid4().hex[:16]}",
                src_entity_id,
                dst_entity_id,
                edge_type,
                float(increment),
                event_at,
            ),
        )
        return

    edge_id, weight, evidence_count, last_event_at = row
    decayed = float(weight or 0.0)
    prev_ts, new_ts = parse_ts(last_event_at), parse_ts(event_at)
    if prev_ts is not None and new_ts is not None and new_ts > prev_ts:
        dt_days = (new_ts - prev_ts).total_seconds() / 86400.0
        decayed *= math.pow(0.5, dt_days / half_life_days)
    latest = max(filter(None, [last_event_at, event_at]), default=None)
    conn.execute(
        """
        UPDATE entity_edges SET weight=?, evidence_count=?, last_event_at=?, updated_at=datetime('now')
        WHERE edge_id=?
        """,
        (decayed + float(increment), int(evidence_count or 0) + 1, latest, edge_id),
    )


def top_edges(
    conn: sqlite3.Connection,
    entity_id: str,
    *,
    edge_type: Optional[str] = None,
    limit: int = 20,
) -> List[Dict[str, Any]]:
    params: List[Any] = [entity_id, entity_id]
    type_clause = ""
    if edge_type:
        type_clause = " AND edge_type=?"
        params.append(edge_type)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT e.src_entity_id, e.dst_entity_id, e.edge_type, e.weight, e.evidence_count,
               e.last_event_at
        FROM entity_edges e
        WHERE (e.src_entity_id=? OR e.dst_entity_id=?){type_clause}
        ORDER BY e.weight DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    out = []
    for src, dst, etype, weight, evidence, last_at in rows:
        other = dst if src == entity_id else src
        name_row = conn.execute(
            "SELECT canonical_name, entity_type FROM entities WHERE entity_id=?", (other,)
        ).fetchone()
        out.append(
            {
                "entity_id": other,
                "entity_name": name_row[0] if name_row else other,
                "entity_type": name_row[1] if name_row else None,
                "edge_type": etype,
                "weight": round(float(weight or 0.0), 4),
                "evidence_count": evidence,
                "last_event_at": last_at,
            }
        )
    return out


def graph_snapshot(
    conn: sqlite3.Connection,
    *,
    limit_nodes: int = 100,
    limit_edges: int = 300,
    min_weight: float = 0.0,
) -> Dict[str, List[Dict[str, Any]]]:
    """Entity graph in the legacy list_graph shape (nodes/edges dicts)."""
    edge_rows = conn.execute(
        """
        SELECT src_entity_id, dst_entity_id, edge_type, weight, evidence_count, last_event_at
        FROM entity_edges WHERE weight >= ? ORDER BY weight DESC LIMIT ?
        """,
        (min_weight, limit_edges),
    ).fetchall()
    node_ids: List[str] = []
    for src, dst, *_ in edge_rows:
        for node in (src, dst):
            if node not in node_ids:
                node_ids.append(node)
    node_ids = node_ids[:limit_nodes]
    nodes = []
    for entity_id in node_ids:
        row = conn.execute(
            "SELECT canonical_name, entity_type, mention_count FROM entities WHERE entity_id=?",
            (entity_id,),
        ).fetchone()
        if row:
            nodes.append(
                {
                    "node_id": entity_id,
                    "node_type": row[1],
                    "label": row[0],
                    "metadata_json": json.dumps({"mention_count": row[2]}),
                }
            )
    edges = [
        {
            "edge_id": f"{src}->{dst}:{etype}",
            "src_node_id": src,
            "dst_node_id": dst,
            "edge_type": etype,
            "weight": round(float(weight or 0.0), 4),
            "metadata_json": json.dumps(
                {"evidence_count": evidence, "last_event_at": last_at}
            ),
        }
        for src, dst, etype, weight, evidence, last_at in edge_rows
        if src in set(node_ids) and dst in set(node_ids)
    ]
    return {"nodes": nodes, "edges": edges}
