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


def _peer_labels(conn: Any, dataset_id: str, keys: List[str]) -> Dict[str, str]:
    """Flat peer_key -> display string.

    `resolve_participant_labels` is keyword-only and returns NESTED dicts
    ({label, display_name, identifier}). The first version of these endpoints called it
    positionally (TypeError -> HTTP 500 on every request that reached labelling) and would
    then have embedded the nested object where a string was promised. Both defects were
    invisible to the endpoint tests because they MOCKED the resolver — the mock is the
    documented counter-example for why these tests now use the real one.
    """
    if not keys:
        return {}
    try:
        raw = resolve_participant_labels(conn, dataset_id=dataset_id, participant_ids=keys)
    except Exception:  # noqa: BLE001 — labels are decoration; data must still flow
        return {}
    out: Dict[str, str] = {}
    for k, entry in (raw or {}).items():
        if isinstance(entry, dict):
            label = str(entry.get("label") or entry.get("display_name") or "").strip()
        else:
            label = str(entry or "").strip()
        if label:
            out[str(k)] = label
    return out


# --------------------------------------------------------------------------- L1 read

@router.get("/messenger-analytics/relationships", dependencies=[Depends(require_api_key)])
def get_relationships(
    dataset_id: str = Query(...),
    tie_state: Optional[str] = Query(None, description="active | cooling | dormant | one_sided | broadcast_only"),
    include_automated: bool = Query(False, description="shortcodes, 2FA and delivery notices"),
    limit: int = Query(100, ge=1, le=500),
) -> Dict[str, Any]:
    """L1 — the lifetime view of every messaging relationship.

    Automated peers are stored but excluded by default. They are 29 of 179 DM counterparties
    on the first live corpus checked, and ranking a carrier shortcode alongside a friend
    makes every relationship number meaningless — but dropping them at write would also lose
    the honest answer to "what is actually filling my inbox", so the filter lives here.
    """
    conn = get_db_connection()
    if conn is None:
        return {"dataset_id": dataset_id, "relationships": [], "error": "no database"}
    from ..analytics.messenger_directed import (MESSENGER_DYAD_STATS_TABLE, PEER_CLASS_HUMAN,
                                                SELF_KEY, ensure_directed_tables_present)

    ensure_directed_tables_present(conn)
    sql = (f"SELECT a_key, b_key, peer_class, total_msgs, a_to_b, b_to_a, balance, first_ts,"
           f" last_ts, active_periods, reciprocal_periods, longest_contact_streak_weeks,"
           f" longest_reciprocal_streak_weeks, longest_contact_streak_months, max_gap_days,"
           f" median_gap_days, recent_gap_days, drift_ratio, tie_state"
           f" FROM {MESSENGER_DYAD_STATS_TABLE} WHERE dataset_id = ? AND involves_self = 1"
           # an owner-owner row (both keys 'self') is corpus damage, not a relationship —
           # presenting it as one labels the owner as their own contact
           f" AND NOT (a_key = '{SELF_KEY}' AND b_key = '{SELF_KEY}')")
    args: List[Any] = [dataset_id]
    if not include_automated:
        sql += " AND peer_class = ?"
        args.append(PEER_CLASS_HUMAN)
    if tie_state:
        sql += " AND tie_state = ?"
        args.append(tie_state)
    sql += " ORDER BY total_msgs DESC LIMIT ?"
    args.append(int(limit))

    keys = ["a_key", "b_key", "peer_class", "total_msgs", "a_to_b", "b_to_a", "balance",
            "first_ts", "last_ts", "active_periods", "reciprocal_periods",
            "contact_streak_weeks", "reciprocal_streak_weeks", "contact_streak_months",
            "max_gap_days", "median_gap_days", "days_since_last", "drift_ratio", "tie_state"]
    out: List[Dict[str, Any]] = []
    for row in conn.execute(sql, args).fetchall():
        d = dict(zip(keys, tuple(row)))
        peer = d["b_key"] if d["a_key"] == SELF_KEY else d["a_key"]
        # sent/received are stated from the OWNER's side, so a caller never has to know
        # which side of the canonical pair the owner landed on
        owner_sent = d["a_to_b"] if d["a_key"] == SELF_KEY else d["b_to_a"]
        out.append({
            "peer_key": peer,
            "peer_class": d["peer_class"],
            "total_msgs": d["total_msgs"],
            "sent": owner_sent,
            "received": d["total_msgs"] - owner_sent,
            "balance": d["balance"],
            "first_ts": d["first_ts"], "last_ts": d["last_ts"],
            "days_since_last": d["days_since_last"],
            "active_periods": d["active_periods"],
            "reciprocal_periods": d["reciprocal_periods"],
            "contact_streak_weeks": d["contact_streak_weeks"],
            "reciprocal_streak_weeks": d["reciprocal_streak_weeks"],
            "contact_streak_months": d["contact_streak_months"],
            "median_gap_days": d["median_gap_days"],
            "max_gap_days": d["max_gap_days"],
            "drift_ratio": d["drift_ratio"],
            "tie_state": d["tie_state"],
        })
    labels = _peer_labels(conn, dataset_id, [r["peer_key"] for r in out])
    for r in out:
        r["label"] = labels.get(r["peer_key"]) or r["peer_key"]
    return {"dataset_id": dataset_id, "count": len(out), "relationships": out}


@router.get("/messenger-analytics/directed-edges", dependencies=[Depends(require_api_key)])
def get_directed_edges(
    dataset_id: str = Query(...),
    peer_key: Optional[str] = Query(None),
    edge_kind: str = Query("dm", description="dm | group_reply | group_broadcast"),
    limit: int = Query(200, ge=1, le=1000),
) -> Dict[str, Any]:
    """L1 — the per-period, per-direction detail behind a relationship.

    `edge_kind` defaults to `dm` on purpose. Group broadcast fans one message out to every
    other speaker in the room, so leaving it in by default would let a busy thread outrank
    every real correspondence — the failure the undirected lane already has.
    """
    conn = get_db_connection()
    if conn is None:
        return {"dataset_id": dataset_id, "edges": [], "error": "no database"}
    from ..analytics.messenger_directed import (MESSENGER_DIRECTED_EDGES_TABLE,
                                                ensure_directed_tables_present)

    ensure_directed_tables_present(conn)
    sql = (f"SELECT period_key, connector, edge_kind, from_key, to_key, msgs,"
           f" sessions_initiated, replies, median_reply_latency_s, first_ts, last_ts"
           f" FROM {MESSENGER_DIRECTED_EDGES_TABLE} WHERE dataset_id = ? AND edge_kind = ?")
    args: List[Any] = [dataset_id, edge_kind]
    if peer_key:
        sql += " AND (from_key = ? OR to_key = ?)"
        args.extend([peer_key, peer_key])
    sql += " ORDER BY period_key DESC, msgs DESC LIMIT ?"
    args.append(int(limit))
    keys = ["period_key", "connector", "edge_kind", "from_key", "to_key", "msgs",
            "sessions_initiated", "replies", "median_reply_latency_s", "first_ts", "last_ts"]
    edges = [dict(zip(keys, tuple(r))) for r in conn.execute(sql, args).fetchall()]
    return {"dataset_id": dataset_id, "edge_kind": edge_kind, "count": len(edges),
            "edges": edges}


@router.get("/messenger-analytics/relationship-signals", dependencies=[Depends(require_api_key)])
def get_relationship_signals(
    dataset_id: str = Query(...),
    signal: str = Query("all", description="all | warmth | drift | reciprocity"),
) -> Dict[str, Any]:
    """L5 — the derived read of the relationship graph.

    Everything here is calibrated against the owner's OWN distribution, and each response
    carries the thresholds it was computed under: a warmth band is a claim about a person,
    and a claim about a person should be able to say what would have changed it.

    `excluded_below_floor` is reported rather than hidden. A dyad under the evidence floor
    has not been judged and found wanting — it has not been judged, and saying so is the
    difference between "you have 35 relationships" and "116 of your contacts are events".
    """
    conn = get_db_connection()
    if conn is None:
        return {"dataset_id": dataset_id, "error": "no database"}
    from ..analytics.messenger_directed import ensure_directed_tables_present
    from ..features.derivation.social_kernels import (_dyad_rows, apply_evidence_floor,
                                                      compute_drift, compute_reciprocity,
                                                      compute_warmth)

    ensure_directed_tables_present(conn)
    rows = _dyad_rows(conn, dataset_id)
    kept, excluded = apply_evidence_floor(rows)
    out: Dict[str, Any] = {
        "dataset_id": dataset_id,
        "dyads_considered": len(rows),
        "dyads_above_floor": len(kept),
        "excluded_below_floor": excluded,
    }
    if signal in ("all", "warmth"):
        out["warmth"] = compute_warmth(rows)
    if signal in ("all", "drift"):
        out["drift_alarms"] = compute_drift(rows)
    if signal in ("all", "reciprocity"):
        out["reciprocity"] = compute_reciprocity(rows)

    labels_for = {r["peer_key"] for k in ("warmth", "drift_alarms", "reciprocity")
                  for r in out.get(k, [])}
    labels = _peer_labels(conn, dataset_id, sorted(labels_for))
    for k in ("warmth", "drift_alarms", "reciprocity"):
        for r in out.get(k, []):
            r["label"] = labels.get(r["peer_key"]) or r["peer_key"]
    return out
