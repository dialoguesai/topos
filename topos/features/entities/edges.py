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


def _fold_weights(
    keep_weight: Any,
    keep_last: Optional[str],
    absorb_weight: Any,
    absorb_last: Optional[str],
):
    """Combine two edge weights under the half-life model.

    Decays the earlier-dated weight forward to the later timestamp, then sums —
    the same decay-then-add rule as ``update_edge``, so a merge and a fresh
    re-observation converge on the same weight. Returns (weight, last_event_at).
    """
    wk = float(keep_weight or 0.0)
    wa = float(absorb_weight or 0.0)
    tk, ta = parse_ts(keep_last), parse_ts(absorb_last)
    if tk is not None and ta is not None:
        if ta > tk:
            wk *= math.pow(0.5, (ta - tk).total_seconds() / 86400.0 / DEFAULT_HALF_LIFE_DAYS)
            return wk + wa, absorb_last
        if tk > ta:
            wa *= math.pow(0.5, (tk - ta).total_seconds() / 86400.0 / DEFAULT_HALF_LIFE_DAYS)
            return wk + wa, keep_last
    return wk + wa, max(filter(None, [keep_last, absorb_last]), default=None)


def merge_entity_edges(
    conn: sqlite3.Connection,
    *,
    keep_id: str,
    absorb_id: str,
) -> None:
    """Rewrite ``absorb_id`` -> ``keep_id`` across entity_edges on a merge.

    A blanket ``UPDATE ... SET src_entity_id=keep WHERE src_entity_id=absorb``
    raises IntegrityError when both entities hold an ACTIVE edge of the same
    type to the same third entity — the active-row partial unique index
    (idx_entity_edges_active) forbids two active rows for one (src, dst, type).
    So per edge: canonicalise the rewritten endpoints, drop edges that collapse
    to a self-loop, and — for an active edge that now collides with an existing
    active twin — fold weight/evidence_count/last_event_at into the survivor and
    delete the duplicate. Closed rows (valid_to set) are unconstrained and
    rewrite in place.
    """
    if not keep_id or not absorb_id or keep_id == absorb_id:
        return
    rows = conn.execute(
        """
        SELECT edge_id, src_entity_id, dst_entity_id, edge_type,
               weight, evidence_count, last_event_at, valid_to
        FROM entity_edges
        WHERE src_entity_id=? OR dst_entity_id=?
        """,
        (absorb_id, absorb_id),
    ).fetchall()
    for edge_id, src, dst, edge_type, weight, evidence, last_at, valid_to in rows:
        new_src = keep_id if src == absorb_id else src
        new_dst = keep_id if dst == absorb_id else dst
        new_src, new_dst = _canonical_order(new_src, new_dst, edge_type)
        if new_src == new_dst:
            # merged endpoints collapsed to a self-loop — meaningless, drop it.
            conn.execute("DELETE FROM entity_edges WHERE edge_id=?", (edge_id,))
            continue
        if valid_to is None:
            twin = conn.execute(
                """
                SELECT edge_id, weight, evidence_count, last_event_at
                FROM entity_edges
                WHERE src_entity_id=? AND dst_entity_id=? AND edge_type=?
                  AND valid_to IS NULL AND edge_id<>?
                """,
                (new_src, new_dst, edge_type, edge_id),
            ).fetchone()
            if twin is not None:
                twin_id, twin_weight, twin_evidence, twin_last = twin
                folded_weight, folded_last = _fold_weights(
                    twin_weight, twin_last, weight, last_at
                )
                conn.execute(
                    """
                    UPDATE entity_edges
                    SET weight=?, evidence_count=?, last_event_at=?, updated_at=datetime('now')
                    WHERE edge_id=?
                    """,
                    (
                        folded_weight,
                        int(twin_evidence or 0) + int(evidence or 0),
                        folded_last,
                        twin_id,
                    ),
                )
                conn.execute("DELETE FROM entity_edges WHERE edge_id=?", (edge_id,))
                continue
        conn.execute(
            """
            UPDATE entity_edges
            SET src_entity_id=?, dst_entity_id=?, updated_at=datetime('now')
            WHERE edge_id=?
            """,
            (new_src, new_dst, edge_id),
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
    as_of: Optional[str] = None,
    selection: str = "weight",
    offset: int = 0,
) -> Dict[str, Any]:
    """Entity graph in the legacy list_graph shape (nodes/edges dicts).

    Validity fields ride first-class on each edge (graph-UI audit): active
    edges carry valid_to=None; include_closed=True adds ended revisions.

    ``as_of`` (ISO date/timestamp) returns the graph AS IT STOOD at that instant
    — edges whose validity window covers it (valid_from <= as_of < valid_to) —
    driving a temporal scrubber. It supersedes include_closed (a point-in-time
    view is neither "active now" nor "all history").

    ``selection``:
      - ``weight`` (default): ORDER BY weight DESC — MCP / firewall-safe slice.
      - ``all``: ORDER BY recency (last_event_at/valid_from) — owner UI full graph.

    ``offset`` pages the ordered edge list for Load more. Response includes
    ``meta`` with truncation flags and ``next_offset`` when more edges remain.
    """
    sel = str(selection or "weight").strip().lower()
    if sel not in ("weight", "all"):
        sel = "weight"
    off = max(0, int(offset or 0))
    lim_e = max(1, int(limit_edges))
    lim_n = max(1, int(limit_nodes))

    where_params: List[Any] = [min_weight]
    if as_of:
        clause = (
            " AND (valid_from IS NULL OR valid_from <= ?)"
            " AND (valid_to IS NULL OR valid_to > ?)"
        )
        where_params.extend([as_of, as_of])
    elif include_closed:
        clause = ""
    else:
        clause = " AND valid_to IS NULL"

    where_sql = f"FROM entity_edges WHERE weight >= ?{clause}"
    total_edges_matching = int(
        conn.execute(f"SELECT COUNT(*) {where_sql}", tuple(where_params)).fetchone()[0]
    )

    if sel == "all":
        order_sql = (
            "ORDER BY COALESCE(last_event_at, valid_from) DESC, edge_id DESC"
        )
    else:
        order_sql = "ORDER BY weight DESC, edge_id DESC"

    edge_params = list(where_params) + [lim_e, off]
    edge_rows = conn.execute(
        f"""
        SELECT edge_id, src_entity_id, dst_entity_id, edge_type, weight, evidence_count,
               last_event_at, valid_from, valid_to, metadata_json
        {where_sql} {order_sql} LIMIT ? OFFSET ?
        """,
        tuple(edge_params),
    ).fetchall()

    def _edge_metadata(evidence, last_at, stored_json) -> str:
        """Stored edge metadata (provenance role_mix, fact statement, mz tag)
        merged with the synthesized evidence fields the UI has always read."""
        try:
            merged = json.loads(stored_json or "{}")
            if not isinstance(merged, dict):
                merged = {}
        except (TypeError, ValueError):
            merged = {}
        merged["evidence_count"] = evidence
        merged["last_event_at"] = last_at
        return json.dumps(merged)

    node_ids: List[str] = []
    for _eid, src, dst, *_ in edge_rows:
        for node in (src, dst):
            if node not in node_ids:
                node_ids.append(node)
    nodes_before_cap = len(node_ids)
    node_ids = node_ids[:lim_n]
    nodes = []
    for entity_id in node_ids:
        row = conn.execute(
            "SELECT canonical_name, entity_type, mention_count, metadata_json, is_self "
            "FROM entities WHERE entity_id=?",
            (entity_id,),
        ).fetchone()
        if row:
            # Stored entity metadata (community_id, mz tag) merged with the
            # synthesized mention_count — same passthrough rule as edges.
            try:
                meta = json.loads(row[3] or "{}")
                if not isinstance(meta, dict):
                    meta = {}
            except (TypeError, ValueError):
                meta = {}
            meta["mention_count"] = row[2]
            # Owner marker so graph UIs can pin/label the self node.
            if row[4]:
                meta["is_self"] = True
            nodes.append(
                {
                    "node_id": entity_id,
                    "node_type": row[1],
                    "label": row[0],
                    "metadata_json": json.dumps(meta),
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
            "metadata_json": _edge_metadata(evidence, last_at, stored_json),
        }
        for eid, src, dst, etype, weight, evidence, last_at, valid_from, valid_to, stored_json in edge_rows
        if src in node_id_set and dst in node_id_set
    ]

    sql_page_len = len(edge_rows)
    truncated_edges = (off + sql_page_len) < total_edges_matching
    truncated_nodes = nodes_before_cap > lim_n
    next_offset = (off + sql_page_len) if truncated_edges else None

    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "selection": sel,
            "limit_nodes": lim_n,
            "limit_edges": lim_e,
            "offset": off,
            "total_edges_matching": total_edges_matching,
            "returned_edges": len(edges),
            "returned_nodes": len(nodes),
            "truncated_edges": truncated_edges,
            "truncated_nodes": truncated_nodes,
            "next_offset": next_offset,
        },
    }
