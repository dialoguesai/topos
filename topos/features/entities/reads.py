"""Read queries over the entity spine (People tab backend).

Every entry point takes a required `guard`. It is keyword-only and has no
default on purpose: a black-hole filter that can be forgotten is a black-hole
filter that will be forgotten, and the failure mode of forgetting is a silent
leak. With no default, a call site that has not decided who is asking raises
`TypeError` instead of quietly serving protected rows.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List, Optional

from ..lifecycle.blackhole_guard import BlackholeGuard
from .dossier import load_dossier_for_entity
from .edges import EDGE_SEMANTIC_AFFINITY, graph_snapshot, top_edges

_ENTITY_COLUMNS = (
    "entity_id, entity_type, canonical_name, aliases_json, identifiers_json,"
    " contact_id, is_self, first_seen, last_seen, mention_count"
)


def _row_to_entity(row: Any) -> Dict[str, Any]:
    def _json_list(raw: Any) -> List[str]:
        try:
            parsed = json.loads(raw or "[]")
            return [str(v) for v in parsed] if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []

    return {
        "entity_id": row[0],
        "entity_type": row[1],
        "canonical_name": row[2],
        "aliases": _json_list(row[3]),
        "identifier_count": len(_json_list(row[4])),
        "is_contact": bool(row[5]),
        "is_self": bool(row[6]),
        "first_seen": row[7],
        "last_seen": row[8],
        "mention_count": int(row[9] or 0),
    }


def list_entities(
    conn: sqlite3.Connection,
    *,
    guard: BlackholeGuard,
    q: Optional[str] = None,
    entity_type: Optional[str] = None,
    contacts_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    where: List[str] = ["is_self = 0"]
    params: List[Any] = []
    if q and str(q).strip():
        where.append("(normalized_name LIKE ? OR aliases_json LIKE ?)")
        needle = f"%{str(q).strip().lower()}%"
        params.extend([needle, needle])
    if entity_type and str(entity_type).strip():
        where.append("entity_type = ?")
        params.append(str(entity_type).strip().lower())
    if contacts_only:
        where.append("contact_id IS NOT NULL")

    # Spliced into every query below — rows, total and the type histogram alike,
    # so a protected entity cannot be inferred from a count that fails to match
    # the rows it accompanies.
    bh_clause, bh_params = guard.sql_exclusion("entity_id")
    if bh_clause:
        where.append(bh_clause)
        params.extend(bh_params)
    where_sql = " AND ".join(where)

    total = conn.execute(
        f"SELECT COUNT(*) FROM entities WHERE {where_sql}", params
    ).fetchone()[0]
    rows = conn.execute(
        f"""
        SELECT {_ENTITY_COLUMNS} FROM entities
        WHERE {where_sql}
        ORDER BY mention_count DESC, canonical_name ASC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()

    type_where = "is_self = 0" + (f" AND {bh_clause}" if bh_clause else "")
    type_rows = conn.execute(
        f"SELECT entity_type, COUNT(*) FROM entities WHERE {type_where} GROUP BY entity_type",
        bh_params,
    ).fetchall()

    return {
        "items": [_row_to_entity(r) for r in rows],
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "type_counts": {str(t): int(n) for t, n in type_rows},
    }


def get_entity_detail(
    conn: sqlite3.Connection,
    entity_id: str,
    *,
    guard: BlackholeGuard,
    mention_limit: int = 20,
    edge_limit: int = 12,
) -> Optional[Dict[str, Any]]:
    # A protected entity takes the same exit as one that was never stored. The
    # caller turns `None` into its ordinary not-found response, so "hidden from
    # you" and "never existed" are the same answer (D5) — no separate branch to
    # get wrong, and no forbidden-shaped error that only real entities can
    # produce.
    if guard.blocks_entity_id(entity_id):
        return None
    row = conn.execute(
        f"SELECT {_ENTITY_COLUMNS} FROM entities WHERE entity_id = ?",
        (str(entity_id),),
    ).fetchone()
    if row is None:
        return None
    entity = _row_to_entity(row)

    mentions = conn.execute(
        """
        SELECT record_id, source_id, canonical_table, surface_text, confidence, event_at
        FROM entity_mentions WHERE entity_id = ?
        ORDER BY COALESCE(event_at, created_at) DESC
        LIMIT ?
        """,
        (str(entity_id), max(1, min(int(mention_limit), 100))),
    ).fetchall()
    entity["recent_mentions"] = [
        {
            "record_id": m[0],
            "source_id": m[1],
            "canonical_table": m[2],
            "surface_text": m[3],
            "confidence": m[4],
            "event_at": m[5],
        }
        for m in mentions
    ]
    # A visible entity may be connected to a protected one: the neighbour list
    # and the dossier prose both name it, so both are filtered even though the
    # subject of this read is perfectly visible.
    #
    # Affinity edges are fetched separately: their weights are cosines in
    # [0, 1] and lose any mixed ORDER BY weight DESC ranking against evidence
    # counts. The drawer / selection panel render them as their own section.
    observed = [
        e
        for e in top_edges(conn, str(entity_id), limit=max(1, int(edge_limit)))
        if e.get("edge_type") != EDGE_SEMANTIC_AFFINITY
    ]
    affinity = top_edges(
        conn,
        str(entity_id),
        edge_type=EDGE_SEMANTIC_AFFINITY,
        limit=max(1, int(edge_limit)),
    )
    id_keys = ("entity_id", "src_entity_id", "dst_entity_id", "other_entity_id")
    name_keys = ("canonical_name", "name", "entity_name")
    entity["connections"] = guard.filter_rows(
        observed, id_keys=id_keys, name_keys=name_keys
    )
    entity["affinity_connections"] = guard.filter_rows(
        affinity, id_keys=id_keys, name_keys=name_keys
    )
    dossier = load_dossier_for_entity(conn, str(entity_id))
    if isinstance(dossier, dict) and not guard.sees_everything:
        dossier = dict(dossier)
        if dossier.get("summary_text") is not None:
            dossier["summary_text"] = guard.withhold_if_mentions(dossier["summary_text"])
        # The neighbour list is a second, separate copy of the name — stripping
        # the prose and leaving this in place would have leaked it verbatim.
        # Structured entries (entity_id) are preferred; legacy string rows are
        # filtered by text scan so a pre-migration dossier cannot leak either.
        for key in ("top_connections", "affinity_connections"):
            rows = dossier.get(key)
            if not isinstance(rows, list):
                continue
            if rows and all(isinstance(r, str) for r in rows):
                dossier[key] = [
                    r for r in rows if not guard.text_mentions_blackholed(r)
                ]
            else:
                dossier[key] = guard.filter_rows(
                    rows,
                    id_keys=("entity_id",),
                    name_keys=("canonical_name", "name", "label", "entity_name"),
                )
    entity["dossier"] = dossier
    return entity


def entity_graph(
    conn: sqlite3.Connection,
    *,
    guard: BlackholeGuard,
    limit_nodes: int = 100,
    limit_edges: int = 300,
    min_weight: float = 0.0,
    include_closed: bool = False,
    as_of: Optional[str] = None,
    selection: str = "weight",
    offset: int = 0,
    event_after: Optional[str] = None,
    event_before: Optional[str] = None,
) -> Dict[str, Any]:
    snapshot = graph_snapshot(
        conn,
        limit_nodes=max(1, min(int(limit_nodes), 5000)),
        limit_edges=max(1, min(int(limit_edges), 20000)),
        min_weight=max(0.0, float(min_weight)),
        include_closed=bool(include_closed),
        as_of=(str(as_of).strip() or None) if as_of else None,
        selection=str(selection or "weight"),
        offset=max(0, int(offset or 0)),
        event_after=(str(event_after).strip() or None) if event_after else None,
        event_before=(str(event_before).strip() or None) if event_before else None,
    )
    if guard.sees_everything or not isinstance(snapshot, dict):
        return snapshot

    # Both ends of every edge, not just the node list: an edge pointing at a
    # removed node still says "someone is here", and its weight and type say
    # roughly who. Counts in `meta` are restated to match what is returned.
    nodes = guard.filter_rows(
        snapshot.get("nodes") or [],
        id_keys=("node_id",),
        name_keys=("label",),
    )
    edges = guard.filter_rows(
        snapshot.get("edges") or [],
        id_keys=("src_node_id", "dst_node_id"),
    )
    meta = dict(snapshot.get("meta") or {})
    meta["returned_nodes"] = len(nodes)
    meta["returned_edges"] = len(edges)
    return {**snapshot, "nodes": nodes, "edges": edges, "meta": meta}
