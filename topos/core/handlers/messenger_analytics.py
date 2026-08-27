"""Messenger analytics message handlers."""
from __future__ import annotations

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    List,
    MESSENGER_COMMUNITIES_TABLE,
    MESSENGER_PARTICIPANT_IMPORTANCE_TABLE,
    MESSENGER_SOCIAL_EDGES_TABLE,
    Optional,
    _normalize_contact_key,
    compute_and_persist_messenger_analytics,
    ensure_messenger_analytics_tables,
    json,
    resolve_participant_labels,
)
from .registry import handles


def _normalize_messenger_source_filter(payload: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    source_id = payload.get("source_id")
    if isinstance(source_id, str) and source_id.strip():
        out.append(source_id.strip())
    source_ids = payload.get("source_ids")
    if isinstance(source_ids, str):
        out.extend([s.strip() for s in source_ids.split(",") if s.strip()])
    elif isinstance(source_ids, list):
        out.extend([str(s).strip() for s in source_ids if str(s).strip()])
    return sorted(set(out))

def _messenger_source_scope(source_filter: List[str]) -> str:
    if not source_filter:
        return "all"
    return ",".join(sorted(set(source_filter)))

def _build_messenger_contact_graph(
    conn,
    *,
    dataset_id: str,
    source_ids: Optional[List[str]] = None,
    max_messages: int = 25000,
    max_nodes: int = 40,
    include_broadcast_edges: bool = True,
) -> Dict[str, Any]:
    """Build lightweight people-interaction graph for messenger verification."""
    src_filter = source_ids or ["imessage", "signal"]
    placeholders = ",".join("?" for _ in src_filter)
    params: List[Any] = [dataset_id, *src_filter, int(max_messages)]
    rows = conn.execute(
        f"""
        SELECT message_id, conversation_id, sender_id, reply_to_message_id, source_id, event_at
        FROM conversation_messages
        WHERE dataset_id = ?
          AND source_id IN ({placeholders})
        ORDER BY event_at ASC
        LIMIT ?
        """,
        params,
    ).fetchall()

    # display name lookup by normalized identifier (prefer first non-empty name)
    name_rows = conn.execute(
        f"""
        SELECT ci.identifier, c.display_name
        FROM contact_identifiers ci
        JOIN contacts c
          ON c.contact_id = ci.contact_id
         AND c.dataset_id = ci.dataset_id
        WHERE ci.dataset_id = ?
          AND ci.source_id IN ({placeholders}, '*')
          AND c.display_name IS NOT NULL
          AND c.display_name != ''
        """,
        [dataset_id, *src_filter],
    ).fetchall()
    display_by_norm: Dict[str, str] = {}
    for identifier, display_name in name_rows:
        nk = _normalize_contact_key(identifier)
        if nk and nk not in display_by_norm and display_name:
            display_by_norm[nk] = str(display_name)

    msg_sender: Dict[str, str] = {}
    conversation_participants: Dict[str, set[str]] = {}
    conversation_rows: Dict[str, List[tuple[str, str, Optional[str]]]] = {}
    source_counts: Dict[str, int] = {}
    for message_id, conversation_id, sender_id, reply_to_message_id, source_id, _ts in rows:
        sid = _normalize_contact_key(sender_id)
        if not sid:
            continue
        mid = str(message_id or "").strip()
        cid = str(conversation_id or "").strip()
        if mid:
            msg_sender[mid] = sid
        if cid:
            conversation_participants.setdefault(cid, set()).add(sid)
            conversation_rows.setdefault(cid, []).append((mid, sid, str(reply_to_message_id or "").strip() or None))
        src = str(source_id or "")
        source_counts[src] = int(source_counts.get(src, 0)) + 1

    edge_weights: Dict[tuple[str, str, str], float] = {}

    def _add_edge(a: str, b: str, kind: str, weight: float) -> None:
        if not a or not b or a == b:
            return
        key = (a, b, kind)
        edge_weights[key] = float(edge_weights.get(key, 0.0)) + float(weight)

    for cid, participants in conversation_participants.items():
        plist = sorted(participants)
        n = len(plist)
        if n < 2:
            continue
        convo_msgs = conversation_rows.get(cid) or []
        if n == 2:
            # Two-party chat: one undirected relationship edge weighted by message volume.
            a, b = plist[0], plist[1]
            _add_edge(a, b, "pair_dialog", float(len(convo_msgs) or 1))
            _add_edge(b, a, "pair_dialog", float(len(convo_msgs) or 1))
            continue

        # Group chat: reply edges + optional broadcast-to-group heuristic edges.
        for mid, sender, reply_to in convo_msgs:
            if reply_to:
                target = msg_sender.get(reply_to)
                if target and target != sender:
                    _add_edge(sender, target, "reply", 1.0)
                    continue
            if include_broadcast_edges:
                others = [p for p in plist if p != sender]
                if others:
                    w = 1.0 / float(len(others))
                    for target in others:
                        _add_edge(sender, target, "broadcast", w)

    degree: Dict[str, float] = {}
    for (a, b, _kind), weight in edge_weights.items():
        degree[a] = float(degree.get(a, 0.0)) + weight
        degree[b] = float(degree.get(b, 0.0)) + weight

    ranked_nodes = sorted(degree.items(), key=lambda x: x[1], reverse=True)
    keep_nodes = {n for n, _ in ranked_nodes[: max(5, int(max_nodes))]}
    filtered_edges = []
    for (a, b, kind), weight in edge_weights.items():
        if a in keep_nodes and b in keep_nodes and weight > 0:
            filtered_edges.append({"from": a, "to": b, "kind": kind, "weight": round(weight, 3)})
    filtered_edges.sort(key=lambda e: e["weight"], reverse=True)
    filtered_edges = filtered_edges[:500]

    nodes = []
    for n, deg in ranked_nodes:
        if n in keep_nodes:
            nodes.append(
                {
                    "id": n,
                    "label": display_by_norm.get(n) or n,
                    "identifier": n,
                    "degree": round(float(deg), 3),
                    "is_self": n == "self",
                }
            )
    nodes.sort(key=lambda x: x["degree"], reverse=True)

    return {
        "dataset_id": dataset_id,
        "sources": src_filter,
        "messages_considered": len(rows),
        "conversations_considered": len(conversation_participants),
        "source_message_counts": source_counts,
        "nodes": nodes,
        "edges": filtered_edges,
    }

@handles("messenger_contact_graph")
async def handle_messenger_contact_graph(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    dataset_id = (payload.get("dataset_id") or "").strip()
    if not dataset_id:
        return {"id": req_id, "status": "error", "error": "dataset_id required"}
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    try:
        source_ids = payload.get("source_ids")
        if not isinstance(source_ids, list):
            source_ids = ["imessage", "signal"]
        graph = _build_messenger_contact_graph(
            conn,
            dataset_id=dataset_id,
            source_ids=[str(s).strip() for s in source_ids if str(s).strip()],
            max_messages=int(payload.get("max_messages") or 25000),
            max_nodes=int(payload.get("max_nodes") or 40),
            include_broadcast_edges=bool(payload.get("include_broadcast_edges", True)),
        )
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", **graph}}
    except Exception as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("messenger_analytics_recompute")
async def handle_messenger_analytics_recompute(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    dataset_id = (payload.get("dataset_id") or "").strip()
    if not dataset_id:
        return {"id": req_id, "status": "error", "error": "dataset_id required"}
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    source_filter = _normalize_messenger_source_filter(payload)
    try:
        result = compute_and_persist_messenger_analytics(
            dataset_id=dataset_id,
            conn=conn,
            start_ts=(payload.get("start_ts") or None),
            end_ts=(payload.get("end_ts") or None),
            source_ids=source_filter or None,
            period_granularity=str(payload.get("period_granularity") or "month"),
            cumulative=bool(payload.get("cumulative", False)),
        )
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", **result}}
    except Exception as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("messenger_analytics_sources")
async def handle_messenger_analytics_sources(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    dataset_id = (payload.get("dataset_id") or "").strip()
    if not dataset_id:
        return {"id": req_id, "status": "error", "error": "dataset_id required"}
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    rows = conn.execute(
        """
            SELECT DISTINCT source_id
            FROM conversation_messages
            WHERE dataset_id = ?
            ORDER BY source_id
            """,
        (dataset_id,),
    ).fetchall()
    sources = [str(r["source_id"]) for r in rows if r and r["source_id"]]
    return {"id": req_id, "status": "ok", "payload": {"status": "ok", "dataset_id": dataset_id, "sources": sources}}

@handles("messenger_analytics_periods", "messenger_analytics_graph", "messenger_analytics_importance", "messenger_analytics_communities")
async def handle_messenger_analytics_periods(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    msg_type = str(message.get("type") or "").strip().lower()
    payload = message.get("payload") or {}
    dataset_id = (payload.get("dataset_id") or "").strip()
    if not dataset_id:
        return {"id": req_id, "status": "error", "error": "dataset_id required"}
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    ensure_messenger_analytics_tables(conn)
    source_filter = _normalize_messenger_source_filter(payload)
    source_scope = _messenger_source_scope(source_filter)
    period_key = (payload.get("period") or "").strip()

    try:
        if bool(payload.get("ensure_data", True)):
            where_period = "AND period_key = ?" if period_key else ""
            params = [dataset_id, source_scope] + ([period_key] if period_key else [])
            row = conn.execute(
                f"""
                    SELECT 1
                    FROM {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE}
                    WHERE dataset_id = ? AND source_scope = ? {where_period}
                    LIMIT 1
                    """,
                tuple(params),
            ).fetchone()
            if not row:
                compute_and_persist_messenger_analytics(
                    dataset_id=dataset_id,
                    conn=conn,
                    source_ids=source_filter or None,
                    period_granularity="month",
                )

        if msg_type == "messenger_analytics_periods":
            rows = conn.execute(
                f"""
                    SELECT DISTINCT period_key
                    FROM {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE}
                    WHERE dataset_id = ? AND source_scope = ?
                    ORDER BY period_key
                    """,
                (dataset_id, source_scope),
            ).fetchall()
            periods = [str(r["period_key"]) for r in rows if r and r["period_key"]]
            return {
                "id": req_id,
                "status": "ok",
                "payload": {"status": "ok", "dataset_id": dataset_id, "source_scope": source_scope, "periods": periods},
            }

        if not period_key:
            return {"id": req_id, "status": "error", "error": "period required"}

        if msg_type == "messenger_analytics_graph":
            edge_rows = conn.execute(
                f"""
                    SELECT source_id, target_id, weight, edge_type, edge_type_counts_json
                    FROM {MESSENGER_SOCIAL_EDGES_TABLE}
                    WHERE dataset_id = ? AND period_key = ? AND source_scope = ?
                    ORDER BY source_id, target_id
                    """,
                (dataset_id, period_key, source_scope),
            ).fetchall()
            node_rows = conn.execute(
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
                (dataset_id, period_key, source_scope),
            ).fetchall()
            labels_by_participant = resolve_participant_labels(
                conn,
                dataset_id=dataset_id,
                participant_ids=[str(r["participant_id"]) for r in node_rows if r and r["participant_id"]],
            )
            nodes = [
                {
                    "id": str(r["participant_id"]),
                    "label": labels_by_participant.get(str(r["participant_id"]), {}).get("label", str(r["participant_id"])),
                    "display_name": labels_by_participant.get(str(r["participant_id"]), {}).get("display_name"),
                    "identifier": labels_by_participant.get(str(r["participant_id"]), {}).get("identifier"),
                    "importance": float(r["centrality_degree"] or 0.0),
                    "community_id": r["community_id"],
                }
                for r in node_rows
            ]
            edges = []
            for row in edge_rows:
                counts = {}
                raw = row["edge_type_counts_json"]
                if raw:
                    try:
                        counts = json.loads(raw)
                    except Exception:
                        counts = {}
                edges.append(
                    {
                        "source": row["source_id"],
                        "target": row["target_id"],
                        "weight": float(row["weight"] or 0.0),
                        "edge_type": row["edge_type"],
                        "edge_type_counts": counts,
                    }
                )
            return {
                "id": req_id,
                "status": "ok",
                "payload": {
                    "status": "ok",
                    "dataset_id": dataset_id,
                    "period": period_key,
                    "source_scope": source_scope,
                    "nodes": nodes,
                    "edges": edges,
                },
            }

        if msg_type == "messenger_analytics_importance":
            rows = conn.execute(
                f"""
                    SELECT participant_id, centrality_degree, centrality_betweenness
                    FROM {MESSENGER_PARTICIPANT_IMPORTANCE_TABLE}
                    WHERE dataset_id = ? AND period_key = ? AND source_scope = ?
                    ORDER BY centrality_degree DESC, centrality_betweenness DESC
                    """,
                (dataset_id, period_key, source_scope),
            ).fetchall()
            labels_by_participant = resolve_participant_labels(
                conn,
                dataset_id=dataset_id,
                participant_ids=[str(row["participant_id"]) for row in rows if row and row["participant_id"]],
            )
            importance = []
            for row in rows:
                participant_id = str(row["participant_id"])
                labels = labels_by_participant.get(participant_id, {})
                importance.append(
                    {
                        "participant_id": participant_id,
                        "participant_label": labels.get("label", participant_id),
                        "participant_display_name": labels.get("display_name"),
                        "participant_identifier": labels.get("identifier"),
                        "centrality_degree": float(row["centrality_degree"] or 0.0),
                        "centrality_betweenness": float(row["centrality_betweenness"] or 0.0),
                    }
                )
            return {
                "id": req_id,
                "status": "ok",
                "payload": {
                    "status": "ok",
                    "dataset_id": dataset_id,
                    "period": period_key,
                    "source_scope": source_scope,
                    "importance": importance,
                },
            }

        rows = conn.execute(
            f"""
                SELECT participant_id, community_id
                FROM {MESSENGER_COMMUNITIES_TABLE}
                WHERE dataset_id = ? AND period_key = ? AND source_scope = ?
                ORDER BY community_id, participant_id
                """,
            (dataset_id, period_key, source_scope),
        ).fetchall()
        labels_by_participant = resolve_participant_labels(
            conn,
            dataset_id=dataset_id,
            participant_ids=[str(row["participant_id"]) for row in rows if row and row["participant_id"]],
        )
        grouped: Dict[int, List[str]] = {}
        for row in rows:
            cid = int(row["community_id"])
            grouped.setdefault(cid, []).append(str(row["participant_id"]))
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
            "id": req_id,
            "status": "ok",
            "payload": {
                "status": "ok",
                "dataset_id": dataset_id,
                "period": period_key,
                "source_scope": source_scope,
                "communities": communities,
            },
        }
    except Exception as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}


# --------------------------------------------------------------------------- SGU-1: L1/L5 reads
#
# One grouped handler for the relationship read surfaces. Delegates to
# analytics/relationship_reads — the SAME functions the HTTP routes wrap — so the relay can
# never serve different fields than the local API. A contract test calls both transports on
# one fixture and asserts byte-equal payloads.

@handles("messenger_relationships", "messenger_relationship_signals",
         "messenger_directed_edges", "messenger_bench")
async def handle_relationship_reads(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    msg_type = str(message.get("type") or "").strip().lower()
    payload = message.get("payload") or {}
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}

    from ...analytics import relationship_reads as reads

    try:
        if msg_type == "messenger_bench":
            # the one read with no dataset scope: roles come from the owner's own record
            result = reads.read_bench(conn)
        else:
            dataset_id = (payload.get("dataset_id") or "").strip()
            if not dataset_id:
                return {"id": req_id, "status": "error", "error": "dataset_id required"}
            if msg_type == "messenger_relationships":
                result = reads.read_relationships(
                    conn,
                    dataset_id=dataset_id,
                    tie_state=(payload.get("tie_state") or None),
                    include_automated=bool(payload.get("include_automated", False)),
                    limit=min(max(int(payload.get("limit") or 100), 1), 500),
                )
            elif msg_type == "messenger_relationship_signals":
                result = reads.read_relationship_signals(
                    conn, dataset_id=dataset_id,
                    signal=str(payload.get("signal") or "all"),
                )
            else:  # messenger_directed_edges
                result = reads.read_directed_edges(
                    conn,
                    dataset_id=dataset_id,
                    peer_key=(payload.get("peer_key") or None),
                    edge_kind=str(payload.get("edge_kind") or "dm"),
                    limit=min(max(int(payload.get("limit") or 200), 1), 1000),
                )
    except Exception as exc:  # noqa: BLE001 — a read must answer, never hang the relay
        return {"id": req_id, "status": "error", "error": str(exc)[:200]}
    return {"id": req_id, "status": "ok", "payload": {"status": "ok", **result}}
