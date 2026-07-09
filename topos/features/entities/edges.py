"""Typed, time-decayed entity edges with validity intervals (B2.2).

Weight update on new evidence:  w <- w * 0.5^(dt / half_life) + increment
O(1) per edge, no rescans; stale relationships fade instead of accumulating
forever (raw counts made 2021 coworkers look as close as current ones).

Validity (B2.2): each (src, dst, type) triple has at most ONE active row
(valid_to IS NULL — enforced by a partial unique index); ended relationships
are CLOSED via supersede_edge (valid_to stamped, row kept), so edge history
accumulates and past-tense queries can surface former connections with an
explicit staleness marker.
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..stats.fold import parse_ts

DEFAULT_HALF_LIFE_DAYS = 60.0

EDGE_CO_OCCURRENCE = "co_occurrence"
EDGE_COMMUNICATES = "communicates_with"
EDGE_DISCUSSES = "discusses"
EDGE_LOCATED_AT = "located_at"
EDGE_PART_OF = "part_of"  # directed: product/sub-unit -> parent org


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_order(src_entity_id: str, dst_entity_id: str, edge_type: str):
    """Undirected edge types get a canonical order so A-B and B-A share a row."""
    if edge_type in (EDGE_CO_OCCURRENCE, EDGE_COMMUNICATES) and dst_entity_id < src_entity_id:
        return dst_entity_id, src_entity_id
    return src_entity_id, dst_entity_id


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
    src_entity_id, dst_entity_id = _canonical_order(src_entity_id, dst_entity_id, edge_type)

    # Only the ACTIVE row folds new evidence; closed revisions are history.
    row = conn.execute(
        """
        SELECT edge_id, weight, evidence_count, last_event_at FROM entity_edges
        WHERE src_entity_id=? AND dst_entity_id=? AND edge_type=? AND valid_to IS NULL
        """,
        (src_entity_id, dst_entity_id, edge_type),
    ).fetchone()

    if row is None:
        conn.execute(
            """
            INSERT INTO entity_edges (
                edge_id, src_entity_id, dst_entity_id, edge_type,
                weight, evidence_count, last_event_at, valid_from
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                f"edg_{uuid.uuid4().hex[:16]}",
                src_entity_id,
                dst_entity_id,
                edge_type,
                float(increment),
                event_at,
                _now_iso(),  # belief validity: when the edge started being held
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


def supersede_edge(
    conn: sqlite3.Connection,
    *,
    src_entity_id: str,
    dst_entity_id: str,
    edge_type: str,
    valid_to: Optional[str] = None,
    successor: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Close the ACTIVE edge for the triple; optionally insert a successor.

    The closed row keeps its weight/evidence history and gains ``valid_to``
    (default: now) — history is never deleted, so past-tense queries can
    surface the ended relationship with a staleness marker (T7).

    ``successor`` (optional) inserts a new ACTIVE edge chained at the close
    instant (successor.valid_from == closed.valid_to, the FactStore pattern).
    Keys: ``edge_type`` / ``dst_entity_id`` / ``src_entity_id`` (default: the
    closed edge's), ``weight`` (default 1.0), ``event_at`` (default None).

    Returns the closed edge_id, or None when no active row matched.
    """
    if not src_entity_id or not dst_entity_id:
        return None
    src_entity_id, dst_entity_id = _canonical_order(src_entity_id, dst_entity_id, edge_type)
    row = conn.execute(
        """
        SELECT edge_id FROM entity_edges
        WHERE src_entity_id=? AND dst_entity_id=? AND edge_type=? AND valid_to IS NULL
        """,
        (src_entity_id, dst_entity_id, edge_type),
    ).fetchone()
    if row is None:
        return None
    closed_at = valid_to or _now_iso()
    conn.execute(
        "UPDATE entity_edges SET valid_to=?, updated_at=datetime('now') WHERE edge_id=?",
        (closed_at, row[0]),
    )
    if successor:
        s_type = str(successor.get("edge_type") or edge_type)
        s_src = str(successor.get("src_entity_id") or src_entity_id)
        s_dst = str(successor.get("dst_entity_id") or dst_entity_id)
        s_src, s_dst = _canonical_order(s_src, s_dst, s_type)
        conn.execute(
            """
            INSERT INTO entity_edges (
                edge_id, src_entity_id, dst_entity_id, edge_type,
                weight, evidence_count, last_event_at, valid_from
            ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                f"edg_{uuid.uuid4().hex[:16]}",
                s_src,
                s_dst,
                s_type,
                float(successor.get("weight") or 1.0),
                successor.get("event_at"),
                closed_at,
            ),
        )
    return str(row[0])


def top_edges(
    conn: sqlite3.Connection,
    entity_id: str,
    *,
    edge_type: Optional[str] = None,
    limit: int = 20,
    include_closed: bool = False,
) -> List[Dict[str, Any]]:
    params: List[Any] = [entity_id, entity_id]
    type_clause = ""
    if edge_type:
        type_clause = " AND edge_type=?"
        params.append(edge_type)
    closed_clause = "" if include_closed else " AND e.valid_to IS NULL"
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT e.src_entity_id, e.dst_entity_id, e.edge_type, e.weight, e.evidence_count,
               e.last_event_at, e.valid_from, e.valid_to
        FROM entity_edges e
        WHERE (e.src_entity_id=? OR e.dst_entity_id=?){type_clause}{closed_clause}
        ORDER BY (e.valid_to IS NULL) DESC, e.weight DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    out = []
    for src, dst, etype, weight, evidence, last_at, valid_from, valid_to in rows:
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
                "valid_from": valid_from,
                "valid_to": valid_to,
            }
        )
    return out


def graph_snapshot(
    conn: sqlite3.Connection,
    *,
    limit_nodes: int = 100,
    limit_edges: int = 300,
    min_weight: float = 0.0,
    include_closed: bool = False,
) -> Dict[str, List[Dict[str, Any]]]:
    """Entity graph in the legacy list_graph shape (nodes/edges dicts).

    Validity fields ride first-class on each edge (graph-UI audit): active
    edges carry valid_to=None; include_closed=True adds ended revisions.
    """
    closed_clause = "" if include_closed else " AND valid_to IS NULL"
    edge_rows = conn.execute(
        f"""
        SELECT edge_id, src_entity_id, dst_entity_id, edge_type, weight, evidence_count,
               last_event_at, valid_from, valid_to
        FROM entity_edges WHERE weight >= ?{closed_clause} ORDER BY weight DESC LIMIT ?
        """,
        (min_weight, limit_edges),
    ).fetchall()
    node_ids: List[str] = []
    for _eid, src, dst, *_ in edge_rows:
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
    node_id_set = set(node_ids)
    edges = [
        {
            # Legacy synthesized id for active rows; closed revisions get the
            # storage edge_id appended so ids stay unique across history.
            "edge_id": (
                f"{src}->{dst}:{etype}" if valid_to is None else f"{src}->{dst}:{etype}:{eid}"
            ),
            "src_node_id": src,
            "dst_node_id": dst,
            "edge_type": etype,
            "weight": round(float(weight or 0.0), 4),
            "last_event_at": last_at,
            "valid_from": valid_from,
            "valid_to": valid_to,
            "metadata_json": json.dumps(
                {"evidence_count": evidence, "last_event_at": last_at}
            ),
        }
        for eid, src, dst, etype, weight, evidence, last_at, valid_from, valid_to in edge_rows
        if src in node_id_set and dst in node_id_set
    ]
    return {"nodes": nodes, "edges": edges}
