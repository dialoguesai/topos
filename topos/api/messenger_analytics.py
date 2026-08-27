from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from fastapi import APIRouter, Depends, Query

from ..analytics.messenger_communities import (
    MESSENGER_COMMUNITIES_TABLE,
    MESSENGER_PARTICIPANT_IMPORTANCE_TABLE,
    MESSENGER_SOCIAL_EDGES_TABLE,
    compute_and_persist_messenger_analytics,
    ensure_messenger_analytics_tables,
)
from ..analytics.messenger_graph import extract_messenger_graph
from ..analytics.messenger_labels import resolve_participant_labels
from ..auth import require_api_key
from ..core.state import get_db_connection

router = APIRouter()


def _normalize_source_filter(
    source_id: Optional[str],
    source_ids: Optional[str],
) -> List[str]:
    out: List[str] = []
    if source_id and source_id.strip():
        out.append(source_id.strip())
    if source_ids:
        for value in source_ids.split(","):
            if value.strip():
                out.append(value.strip())
    return sorted(set(out))


def _source_scope(source_filter: Sequence[str]) -> str:
    if not source_filter:
        return "all"
    return ",".join(sorted(set(source_filter)))


def _rows_to_dicts(rows: Sequence[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for row in rows:
        if hasattr(row, "keys"):
            out.append({k: row[k] for k in row.keys()})
        else:
            out.append(dict(row))
    return out


@router.get("/messenger-analytics/recompute", dependencies=[Depends(require_api_key)])
async def recompute_messenger_analytics_get_alias() -> Dict[str, Any]:
    """Method helper for users accidentally using GET on recompute."""
    return {"status": "error", "error": "Use POST /v1/messenger-analytics/recompute"}


@router.post("/messenger-analytics/recompute", dependencies=[Depends(require_api_key)])
async def recompute_messenger_analytics(
    dataset_id: str = Query(...),
    period_granularity: str = Query("month"),
    source_id: Optional[str] = Query(None),
    source_ids: Optional[str] = Query(None),
    start_ts: Optional[str] = Query(None),
    end_ts: Optional[str] = Query(None),
    cumulative: bool = Query(False),
) -> Dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    source_filter = _normalize_source_filter(source_id, source_ids)
    result = compute_and_persist_messenger_analytics(
        dataset_id=dataset_id,
        conn=conn,
        start_ts=start_ts,
        end_ts=end_ts,
        source_ids=source_filter or None,
        period_granularity=period_granularity,
        cumulative=cumulative,
    )
    return {"status": "ok", **result}


def _maybe_compute_if_missing(
    *,
    conn: Any,
    dataset_id: str,
    period_key: str,
    source_filter: Sequence[str],
    ensure_data: bool,
) -> None:
    if not ensure_data:
        return
    source_scope = _source_scope(source_filter)
    row = conn.execute(
        f"""
        SELECT 1
        FROM {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE}
        WHERE dataset_id = ? AND period_key = ? AND source_scope = ?
        LIMIT 1
        """,
        (dataset_id, period_key, source_scope),
    ).fetchone()
    if row:
        return
    compute_and_persist_messenger_analytics(
        dataset_id=dataset_id,
        conn=conn,
        source_ids=source_filter or None,
        period_granularity="month",
    )


@router.get("/messenger-analytics/graph", dependencies=[Depends(require_api_key)])
async def get_messenger_graph(
    dataset_id: str = Query(...),
    period: str = Query(...),
    source_id: Optional[str] = Query(None),
    source_ids: Optional[str] = Query(None),
    ensure_data: bool = Query(True),
) -> Dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    ensure_messenger_analytics_tables(conn)
    source_filter = _normalize_source_filter(source_id, source_ids)
    _maybe_compute_if_missing(
        conn=conn,
        dataset_id=dataset_id,
        period_key=period,
        source_filter=source_filter,
        ensure_data=ensure_data,
    )
    source_scope = _source_scope(source_filter)
    rows = _rows_to_dicts(
        conn.execute(
            f"""
            SELECT source_id, target_id, weight, edge_type, edge_type_counts_json
            FROM {MESSENGER_SOCIAL_EDGES_TABLE}
            WHERE dataset_id = ? AND period_key = ? AND source_scope = ?
            ORDER BY source_id, target_id
            """,
            (dataset_id, period, source_scope),
        ).fetchall()
    )
    nodes = _rows_to_dicts(
        conn.execute(
            f"""
            SELECT i.participant_id, i.centrality_degree, c.community_id
            FROM {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE} i
            LEFT JOIN {MESSENGER_COMMUNITIES_TABLE} c
              ON c.dataset_id = i.dataset_id
             AND c.period_key = i.period_key
             AND c.source_scope = i.source_scope
             AND c.participant_id = i.participant_id
            WHERE i.dataset_id = ? AND i.period_key = ? AND i.source_scope = ?
            ORDER BY i.centrality_degree DESC, i.participant_id
            """,
            (dataset_id, period, source_scope),
        ).fetchall()
    )
    labels_by_participant = resolve_participant_labels(
        conn,
        dataset_id=dataset_id,
        participant_ids=[str(row["participant_id"]) for row in nodes if row.get("participant_id")],
    )
    graph_nodes = [
        {
            "id": row["participant_id"],
            "label": labels_by_participant.get(str(row["participant_id"]), {}).get("label", row["participant_id"]),
            "display_name": labels_by_participant.get(str(row["participant_id"]), {}).get("display_name"),
            "identifier": labels_by_participant.get(str(row["participant_id"]), {}).get("identifier"),
            "importance": float(row.get("centrality_degree") or 0.0),
            "community_id": row.get("community_id"),
        }
        for row in nodes
    ]
    graph_edges = []
    for row in rows:
        counts_raw = row.get("edge_type_counts_json")
        counts = {}
        if counts_raw:
            try:
                import json

                counts = json.loads(counts_raw)
            except Exception:
                counts = {}
        graph_edges.append(
            {
                "source": row["source_id"],
                "target": row["target_id"],
                "weight": float(row.get("weight") or 0.0),
                "edge_type": row.get("edge_type"),
                "edge_type_counts": counts,
            }
        )
    return {
        "status": "ok",
        "dataset_id": dataset_id,
        "period": period,
        "source_scope": source_scope,
        "nodes": graph_nodes,
        "edges": graph_edges,
    }


@router.get("/messenger-analytics/importance", dependencies=[Depends(require_api_key)])
async def get_messenger_importance(
    dataset_id: str = Query(...),
    period: str = Query(...),
    source_id: Optional[str] = Query(None),
    source_ids: Optional[str] = Query(None),
    ensure_data: bool = Query(True),
) -> Dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    ensure_messenger_analytics_tables(conn)
    source_filter = _normalize_source_filter(source_id, source_ids)
    _maybe_compute_if_missing(
        conn=conn,
        dataset_id=dataset_id,
        period_key=period,
        source_filter=source_filter,
        ensure_data=ensure_data,
    )
    source_scope = _source_scope(source_filter)
    rows = _rows_to_dicts(
        conn.execute(
            f"""
            SELECT participant_id, centrality_degree, centrality_betweenness
            FROM {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE}
            WHERE dataset_id = ? AND period_key = ? AND source_scope = ?
            ORDER BY centrality_degree DESC, centrality_betweenness DESC
            """,
            (dataset_id, period, source_scope),
        ).fetchall()
    )
    labels_by_participant = resolve_participant_labels(
        conn,
        dataset_id=dataset_id,
        participant_ids=[str(row["participant_id"]) for row in rows if row.get("participant_id")],
    )
    importance = []
    for row in rows:
        participant_id = str(row["participant_id"])
        labels = labels_by_participant.get(participant_id, {})
        importance.append(
            {
                **row,
                "participant_label": labels.get("label", participant_id),
                "participant_display_name": labels.get("display_name"),
                "participant_identifier": labels.get("identifier"),
            }
        )
    return {
        "status": "ok",
        "dataset_id": dataset_id,
        "period": period,
        "source_scope": source_scope,
        "importance": importance,
    }


@router.get("/messenger-analytics/communities", dependencies=[Depends(require_api_key)])
async def get_messenger_communities(
    dataset_id: str = Query(...),
    period: str = Query(...),
    source_id: Optional[str] = Query(None),
    source_ids: Optional[str] = Query(None),
    ensure_data: bool = Query(True),
) -> Dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    ensure_messenger_analytics_tables(conn)
    source_filter = _normalize_source_filter(source_id, source_ids)
    _maybe_compute_if_missing(
        conn=conn,
        dataset_id=dataset_id,
        period_key=period,
        source_filter=source_filter,
        ensure_data=ensure_data,
    )
    source_scope = _source_scope(source_filter)
    rows = _rows_to_dicts(
        conn.execute(
            f"""
            SELECT participant_id, community_id
            FROM {MESSENGER_COMMUNITIES_TABLE}
            WHERE dataset_id = ? AND period_key = ? AND source_scope = ?
            ORDER BY community_id, participant_id
            """,
            (dataset_id, period, source_scope),
        ).fetchall()
    )
    participant_ids = [str(row["participant_id"]) for row in rows if row.get("participant_id")]
    labels_by_participant = resolve_participant_labels(
        conn,
        dataset_id=dataset_id,
        participant_ids=participant_ids,
    )
    grouped: Dict[int, List[str]] = {}
    for row in rows:
        community_id = int(row["community_id"])
        grouped.setdefault(community_id, []).append(row["participant_id"])
    communities = [
        {
            "community_id": cid,
            "participants": participants,
            "participants_labeled": [
                {
                    "id": pid,
                    "label": labels_by_participant.get(str(pid), {}).get("label", pid),
                }
                for pid in participants
            ],
        }
        for cid, participants in sorted(grouped.items(), key=lambda item: item[0])
    ]
    return {
        "status": "ok",
        "dataset_id": dataset_id,
        "period": period,
        "source_scope": source_scope,
        "communities": communities,
    }


@router.get("/messenger-analytics/periods", dependencies=[Depends(require_api_key)])
async def get_messenger_periods(
    dataset_id: str = Query(...),
    source_id: Optional[str] = Query(None),
    source_ids: Optional[str] = Query(None),
    include_empty: bool = Query(False),
) -> Dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    ensure_messenger_analytics_tables(conn)
    source_filter = _normalize_source_filter(source_id, source_ids)
    source_scope = _source_scope(source_filter)
    rows = _rows_to_dicts(
        conn.execute(
            f"""
            SELECT DISTINCT period_key
            FROM {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE}
            WHERE dataset_id = ? AND source_scope = ?
            ORDER BY period_key
            """,
            (dataset_id, source_scope),
        ).fetchall()
    )
    periods = [row["period_key"] for row in rows]
    if include_empty or periods:
        return {"status": "ok", "dataset_id": dataset_id, "source_scope": source_scope, "periods": periods}

    extraction = extract_messenger_graph(
        dataset_id=dataset_id,
        conn=conn,
        source_ids=source_filter or None,
        period_granularity="month",
    )
    fallback_periods = [p["period_key"] for p in extraction.get("periods", [])]
    return {"status": "ok", "dataset_id": dataset_id, "source_scope": source_scope, "periods": fallback_periods}


@router.get("/messenger-analytics/sources", dependencies=[Depends(require_api_key)])
async def get_messenger_sources(dataset_id: str = Query(...)) -> Dict[str, Any]:
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    rows = _rows_to_dicts(
        conn.execute(
            """
            SELECT DISTINCT source_id
            FROM conversation_messages
            WHERE dataset_id = ?
            ORDER BY source_id
            """,
            (dataset_id,),
        ).fetchall()
    )
    return {"status": "ok", "dataset_id": dataset_id, "sources": [r["source_id"] for r in rows]}


# --------------------------------------------------------------------------- L1/L5 reads
#
# Thin wrappers only. The bodies live in `analytics/relationship_reads.py`, shared with the
# websocket handlers — the SGU-1 no-drift rule: two transports, one implementation, so the
# relay can never serve different fields than the local API.

@router.get("/messenger-analytics/relationships", dependencies=[Depends(require_api_key)])
def get_relationships(
    dataset_id: str = Query(...),
    tie_state: Optional[str] = Query(None, description="active | cooling | dormant | one_sided | broadcast_only"),
    include_automated: bool = Query(False, description="shortcodes, 2FA and delivery notices"),
    limit: int = Query(100, ge=1, le=500),
) -> Dict[str, Any]:
    conn = get_db_connection()
    if conn is None:
        return {"dataset_id": dataset_id, "relationships": [], "error": "no database"}
    from ..analytics.relationship_reads import read_relationships

    return read_relationships(conn, dataset_id=dataset_id, tie_state=tie_state,
                              include_automated=include_automated, limit=limit)


@router.get("/messenger-analytics/directed-edges", dependencies=[Depends(require_api_key)])
def get_directed_edges(
    dataset_id: str = Query(...),
    peer_key: Optional[str] = Query(None),
    edge_kind: str = Query("dm", description="dm | group_reply | group_broadcast"),
    limit: int = Query(200, ge=1, le=1000),
) -> Dict[str, Any]:
    conn = get_db_connection()
    if conn is None:
        return {"dataset_id": dataset_id, "edges": [], "error": "no database"}
    from ..analytics.relationship_reads import read_directed_edges

    return read_directed_edges(conn, dataset_id=dataset_id, peer_key=peer_key,
                               edge_kind=edge_kind, limit=limit)


@router.get("/messenger-analytics/relationship-signals", dependencies=[Depends(require_api_key)])
def get_relationship_signals(
    dataset_id: str = Query(...),
    signal: str = Query("all", description="all | warmth | drift | reciprocity"),
) -> Dict[str, Any]:
    conn = get_db_connection()
    if conn is None:
        return {"dataset_id": dataset_id, "error": "no database"}
    from ..analytics.relationship_reads import read_relationship_signals

    return read_relationship_signals(conn, dataset_id=dataset_id, signal=signal)


@router.get("/messenger-analytics/bench", dependencies=[Depends(require_api_key)])
def get_bench() -> Dict[str, Any]:
    conn = get_db_connection()
    if conn is None:
        return {"roles": [], "error": "no database"}
    from ..analytics.relationship_reads import read_bench

    return read_bench(conn)


@router.get("/messenger-analytics/luck-surface", dependencies=[Depends(require_api_key)])
def get_luck_surface(
    dataset_id: str = Query(...),
    explore: float = Query(0.5, ge=0.0, le=1.0,
                           description="0 = deepen existing ties, 1 = reach new circles"),
) -> Dict[str, Any]:
    conn = get_db_connection()
    if conn is None:
        return {"dataset_id": dataset_id, "work_items": [], "error": "no database"}
    from ..analytics.relationship_reads import read_luck_surface

    return read_luck_surface(conn, dataset_id=dataset_id, explore=explore)
