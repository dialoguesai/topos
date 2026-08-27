"""The relationship read surfaces, transport-free.

SGU-1's no-drift rule: the HTTP routes (`api/messenger_analytics.py`) and the websocket
handlers (`core/handlers/messenger_analytics.py`) both delegate HERE, so the two transports
cannot diverge — the relay serving different fields than the local API is exactly the class
of skew the adversarial retest kept finding between tested parts.

Every function takes an explicit connection and explicit arguments. No FastAPI types, no
hub state — a plain function a contract test can call twice and compare.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Dict, List, Optional


def _table_missing(conn: Any, table: str) -> bool:
    """Read paths never create tables.

    `ensure_directed_tables_present` runs DDL, which takes SQLite's WRITE LOCK — on a read
    endpoint that is a write on every page load, and on a read-only connection it raises
    outright. A read whose table is absent has an honest answer already: nothing computed
    yet. Measured while adding the read-cost guard, so the fix is cheaper than the bug.
    """
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return not row
    except sqlite3.Error:
        return True


def peer_labels(conn: Any, dataset_id: str, keys: List[str]) -> Dict[str, str]:
    """Flat peer_key -> display string.

    `resolve_participant_labels` is keyword-only and returns NESTED dicts; calling it
    positionally 500'd every request, and passing its result through raw embedded objects
    where strings were promised. Both were hidden by mocked tests — this helper exists so
    the flattening happens in exactly one place.
    """
    if not keys:
        return {}
    from .messenger_labels import resolve_participant_labels

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


def read_relationships(
    conn: Any,
    *,
    dataset_id: str,
    tie_state: Optional[str] = None,
    include_automated: bool = False,
    limit: int = 100,
) -> Dict[str, Any]:
    """The lifetime view of every messaging relationship, stated from the owner's side."""
    from .messenger_directed import (MESSENGER_DYAD_STATS_TABLE, PEER_CLASS_HUMAN,
                                     SELF_KEY, resolve_peer_identities)

    if _table_missing(conn, MESSENGER_DYAD_STATS_TABLE):
        return {"dataset_id": dataset_id, "count": 0, "unnamed_count": 0,
                "relationships": []}
    # `warmth_band` arrived after the table did (G3), so a node whose lane has not re-run
    # has the rows without the column. Select it only when present rather than 500ing on a
    # mid-upgrade node — the rest of the relationship is still true and worth showing.
    has_warmth = "warmth_band" in {
        r[1] for r in conn.execute(f"PRAGMA table_info({MESSENGER_DYAD_STATS_TABLE})")}
    columns = ("a_key, b_key, peer_class, total_msgs, a_to_b, b_to_a, balance, first_ts,"
               " last_ts, active_periods, reciprocal_periods, longest_contact_streak_weeks,"
               " longest_reciprocal_streak_weeks, longest_contact_streak_months,"
               " max_gap_days, median_gap_days, recent_gap_days, drift_ratio, tie_state")
    if has_warmth:
        columns += ", warmth_band"
    sql = (f"SELECT {columns}"
           f" FROM {MESSENGER_DYAD_STATS_TABLE} WHERE dataset_id = ? AND involves_self = 1"
           # an owner-owner row (both keys 'self') is corpus damage, not a relationship
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
    if has_warmth:
        keys.append("warmth_band")
    out: List[Dict[str, Any]] = []
    for row in conn.execute(sql, args).fetchall():
        d = dict(zip(keys, tuple(row)))
        d.setdefault("warmth_band", None)
        peer = d["b_key"] if d["a_key"] == SELF_KEY else d["a_key"]
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
            "warmth_band": d["warmth_band"],
            "tie_state": d["tie_state"],
        })
    labels = peer_labels(conn, dataset_id, [r["peer_key"] for r in out])
    idents = resolve_peer_identities(conn, [r["peer_key"] for r in out])
    unnamed = 0
    for r in out:
        label = labels.get(r["peer_key"]) or r["peer_key"]
        r["label"] = label
        cid, eid, _dn = idents.get(r["peer_key"], (None, None, None))
        r["contact_id"] = cid
        r["person_id"] = eid
        r["needs_name"] = bool(label == r["peer_key"] or not any(ch.isalpha() for ch in label))
        if r["needs_name"]:
            unnamed += 1
    return {"dataset_id": dataset_id, "count": len(out), "unnamed_count": unnamed,
            "relationships": out}


def read_directed_edges(
    conn: Any,
    *,
    dataset_id: str,
    peer_key: Optional[str] = None,
    edge_kind: str = "dm",
    limit: int = 200,
) -> Dict[str, Any]:
    """Per-period, per-direction detail behind a relationship. Defaults to dm on purpose:
    group broadcast fans one message to every speaker and would outrank every real
    correspondence."""
    from .messenger_directed import MESSENGER_DIRECTED_EDGES_TABLE

    if _table_missing(conn, MESSENGER_DIRECTED_EDGES_TABLE):
        return {"dataset_id": dataset_id, "edge_kind": edge_kind, "count": 0, "edges": []}
    # affect_* and topic_* were added to the table after it shipped, so a node whose lane
    # has not re-run has the rows without them. Select what exists rather than 500ing on a
    # mid-upgrade node; the counts and latencies are still true.
    present = {r[1] for r in
               conn.execute(f"PRAGMA table_info({MESSENGER_DIRECTED_EDGES_TABLE})")}
    optional = [c for c in ("affect_counts_json", "affect_coverage",
                            "topic_counts_json", "topic_coverage") if c in present]
    columns = ("period_key, connector, edge_kind, from_key, to_key, msgs,"
               " sessions_initiated, replies, median_reply_latency_s, first_ts, last_ts")
    if optional:
        columns += ", " + ", ".join(optional)
    sql = (f"SELECT {columns}"
           f" FROM {MESSENGER_DIRECTED_EDGES_TABLE} WHERE dataset_id = ? AND edge_kind = ?")
    args: List[Any] = [dataset_id, edge_kind]
    if peer_key:
        sql += " AND (from_key = ? OR to_key = ?)"
        args.extend([peer_key, peer_key])
    sql += " ORDER BY period_key DESC, msgs DESC LIMIT ?"
    args.append(int(limit))
    keys = ["period_key", "connector", "edge_kind", "from_key", "to_key", "msgs",
            "sessions_initiated", "replies", "median_reply_latency_s", "first_ts",
            "last_ts"] + optional
    edges = []
    for r in conn.execute(sql, args).fetchall():
        row = dict(zip(keys, tuple(r)))
        # absent columns read as null, never as a zero coverage that would imply a measured
        # "nothing" where the truth is "not measured"
        for c in ("affect_counts_json", "affect_coverage",
                  "topic_counts_json", "topic_coverage"):
            row.setdefault(c, None)
        edges.append(row)
    return {"dataset_id": dataset_id, "edge_kind": edge_kind, "count": len(edges),
            "edges": edges}


def read_relationship_signals(conn: Any, *, dataset_id: str, signal: str = "all") -> Dict[str, Any]:
    """The derived read: warmth / drift / reciprocity, calibrated against the owner's own
    distribution, with what was NOT judged reported rather than hidden."""
    from ..features.derivation.social_kernels import (_dyad_rows, apply_evidence_floor,
                                                      compute_drift, compute_reciprocity,
                                                      compute_warmth)
    from .messenger_directed import MESSENGER_DYAD_STATS_TABLE

    if _table_missing(conn, MESSENGER_DYAD_STATS_TABLE):
        return {"dataset_id": dataset_id, "dyads_considered": 0, "dyads_above_floor": 0,
                "excluded_below_floor": 0}
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
    labels = peer_labels(conn, dataset_id, sorted(labels_for))
    for k in ("warmth", "drift_alarms", "reciprocity"):
        for r in out.get(k, []):
            r["label"] = labels.get(r["peer_key"]) or r["peer_key"]
    return out


def read_bench(conn: Any) -> Dict[str, Any]:
    """THE BENCH, computed at read. Owner-only by construction."""
    from ..features.derivation.social_bench import build_bench_slate

    return build_bench_slate(conn)


def read_luck_surface(conn: Any, *, dataset_id: str,
                      explore: float = 0.5) -> Dict[str, Any]:
    """LSU-5 — Doing x Telling per body of work, with every basis stated.

    Computed at read like the bench, not stored: the inputs (entity mentions, communities,
    messages) change on every sync, and a cached luck surface would quietly describe last
    month's life. The rollup measures ~330ms on the live corpus, which is a page load.

    Needs `entities` and `entity_mentions`; a node whose extraction has not run yet gets the
    honest empty answer rather than a 500.
    """
    from .luck_surface import build_moves, rollup

    # dataset_id may legitimately arrive empty — see resolve_primary_dataset
    for table in ("entities", "entity_mentions"):
        if _table_missing(conn, table):
            return {"dataset_id": dataset_id, "work_items": [], "moves": [], "coverage": {
                "reason": "entity extraction has not run on this node yet"}}
    out = rollup(conn, dataset_id)
    # Moves ride along rather than taking a second round trip: the ranker re-derives the
    # rollup anyway, and two calls could show a panel of suggestions computed from a
    # different snapshot than the chart above them.
    try:
        out["moves"] = build_moves(conn, dataset_id, explore=explore)
    except Exception:  # noqa: BLE001 — the chart stands on its own if the ranker fails
        out["moves"] = []
    out["explore"] = explore
    return out


def read_person_graph(conn: Any, *, dataset_id: str,
                      include_automated: bool = False) -> Dict[str, Any]:
    """The person-centric graph: one node per person, evidence-gated, owner first.

    Computed at read like the bench and the luck surface — 441 nodes in ~6ms on the live
    corpus, which is a page load. Storing it would mean a graph that quietly describes last
    month's relationships.
    """
    from .dataset_resolution import resolve_messaging_dataset
    from .person_graph import build_person_nodes, resolve_owner_identity

    for table in ("entities",):
        if _table_missing(conn, table):
            return {"dataset_id": dataset_id, "nodes": [], "coverage": {
                "reason": "entity extraction has not run on this node yet"}}
    # Same resolution the luck read uses. Without it an empty or wrong dataset_id finds no
    # messaging at all, and the graph silently becomes mention-only: measured live, that read
    # "0 unnamed, 290 named" for a node with 121 unnamed people.
    dataset_id, dataset_resolved = resolve_messaging_dataset(conn, dataset_id)
    nodes = build_person_nodes(conn, dataset_id, include_automated=include_automated)
    owner = resolve_owner_identity(conn)
    people = [n for n in nodes if not n.get("is_owner")]
    return {
        "dataset_id": dataset_id,
        "nodes": nodes,
        "counts": {
            "total": len(nodes),
            "messaged": sum(1 for n in people if n["evidence"]["messaged"]),
            "mentioned": sum(1 for n in people if n["evidence"]["mentioned"]),
            "both": sum(1 for n in people
                        if n["evidence"]["messaged"] and n["evidence"]["mentioned"]),
            "needs_name": sum(1 for n in people if n.get("needs_name")),
        },
        "owner": {"entity_id": owner["canonical_id"], "label": owner["label"],
                  "identity_count": len(owner["ids"])},
        "dataset_resolved_by_engine": dataset_resolved,
        "coverage": {
            "node_basis": ("evidence only — messaged or mentioned. The address book is a "
                           "naming source, never a node source."),
            "excluded": "automated shortcodes (2FA, delivery notices) are not people",
            "evidence_meaning": ("a node without `messaged` has no cadence, so warmth, drift "
                                 "and reciprocity are unavailable rather than zero"),
        },
    }


def read_naming_queue(conn: Any, *, dataset_id: str, limit: int = 25) -> Dict[str, Any]:
    """Unnamed human peers, busiest first."""
    from .dataset_resolution import resolve_messaging_dataset
    from .person_graph import naming_queue

    if _table_missing(conn, "messenger_dyad_stats"):
        return {"dataset_id": dataset_id, "queue": [], "unnamed_count": 0}
    dataset_id, dataset_resolved = resolve_messaging_dataset(conn, dataset_id)
    out = naming_queue(conn, dataset_id, limit=limit)
    out["dataset_resolved_by_engine"] = dataset_resolved
    return out
