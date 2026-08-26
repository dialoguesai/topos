"""Shared read/write surface for derivation UI (W4) — used by BOTH the message
handlers (core/handlers/derivation.py) and the HTTP routes (api/signal.py), so
the two transports can never drift."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List


def list_packs(conn: sqlite3.Connection) -> Dict[str, Any]:
    from .packs import load_packs
    from .registry import bundled_pack_dir, seed_pack_registry

    seed_pack_registry(conn, bundled_pack_dir())
    catalog = load_packs(bundled_pack_dir())
    counts = dict(conn.execute(
        "SELECT ontology_id, COUNT(*) FROM signal_objects"
        " WHERE object_type='fact' AND ontology_id IS NOT NULL AND valid_to IS NULL"
        " GROUP BY ontology_id").fetchall())
    conflict_counts = dict(conn.execute(
        "SELECT predicate, COUNT(*) FROM fact_conflicts WHERE status='pending'"
        " GROUP BY predicate").fetchall())
    def _q(sql, args=()):
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []              # pre-migration-66 node
    offers = {}
    for oid, pid_, kind, stats_json, created in _q(
            "SELECT offer_id, pack_id, kind, stats_json, created_at FROM pack_offers"
            " WHERE status='pending' ORDER BY created_at DESC"):
        offers.setdefault(pid_, {"offer_id": oid, "kind": kind, "created_at": created,
                                 "stats": json.loads(stats_json or "{}")})
    yield_30d = {r[0]: {"prefilter_hits": r[1], "llm_calls": r[2], "written": r[3]}
                 for r in _q("SELECT pack_id, SUM(prefilter_hits), SUM(llm_calls), SUM(written)"
                             " FROM pack_yield WHERE day >= date('now','-30 day') GROUP BY pack_id")}
    packs: List[Dict[str, Any]] = []
    for pid, ver, enabled, disclosure, last_run in conn.execute(
            "SELECT pack_id, version, enabled, disclosure_default, last_run_at"
            " FROM pack_registry ORDER BY pack_id"):
        p = catalog.get(pid)
        ns = pid.split(".")[0]
        pending = sum(n for pred, n in conflict_counts.items() if pred.split(".")[0] == ns)
        packs.append({
            "pack_id": pid, "version": ver, "enabled": bool(enabled),
            "disclosure_default": disclosure,
            "title": getattr(p, "title", pid) if p else pid,
            "sensitivity_class": getattr(p, "sensitivity_class", "personal") if p else "personal",
            "fact_count": counts.get(pid, 0),
            "pending_conflicts": pending,
            "last_run_at": last_run,
            "predicates": len(getattr(p, "predicates", {}) or {}) if p else 0,
            "offer": offers.get(pid),
            "yield_30d": yield_30d.get(pid),
        })
    return {"packs": packs, "total_conflicts": sum(conflict_counts.values())}


def set_pack_enabled(conn: sqlite3.Connection, pack_id: str, enabled: bool) -> bool:
    from ...storage.db.write_gate import commit_connection, with_db_write
    with with_db_write():
        cur = conn.execute(
            "UPDATE pack_registry SET enabled=?, updated_at=datetime('now') WHERE pack_id=?",
            (1 if enabled else 0, pack_id))
        commit_connection(conn)
    return bool(cur.rowcount)


def list_conflicts(conn: sqlite3.Connection, limit: int = 100) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for cid, subj, pred, incumbent, value, conf, status, created in conn.execute(
            "SELECT conflict_id, subject_entity_id, predicate, incumbent_object_id,"
            " challenger_value, challenger_confidence, status, created_at"
            " FROM fact_conflicts WHERE status='pending'"
            " ORDER BY created_at DESC LIMIT ?", (min(int(limit), 500),)):
        try:
            val = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            val = value
        is_q = str(incumbent).startswith("quarantine:")
        rows.append({"conflict_id": cid, "subject_entity_id": subj, "predicate": pred,
                     "kind": "quarantine" if is_q else "conflict",
                     "reason": str(incumbent)[len("quarantine:"):] if is_q else None,
                     "incumbent_object_id": incumbent, "value": val,
                     "confidence": conf, "created_at": created})
    return rows


def resolve_conflict(conn: sqlite3.Connection, conflict_id: str, status: str) -> bool:
    if status not in ("dismissed", "accepted"):
        raise ValueError("status must be dismissed|accepted")
    from ...storage.db.write_gate import commit_connection, with_db_write
    with with_db_write():
        cur = conn.execute("UPDATE fact_conflicts SET status=? WHERE conflict_id=?",
                           (status, conflict_id))
        commit_connection(conn)
    return bool(cur.rowcount)


def resolve_pack_offer(conn: sqlite3.Connection, offer_id: str, action: str) -> Dict[str, Any]:
    """Owner decision on a self-gating offer. accept on an enable_offer enables
    the pack; accept on a disable_nudge disables it; dismiss just records the
    choice (with backoff — a dismissed offer is not re-minted for 30 days)."""
    if action not in ("accept", "dismiss"):
        raise ValueError("action must be accept|dismiss")
    row = conn.execute("SELECT pack_id, kind, status FROM pack_offers WHERE offer_id=?",
                       (offer_id,)).fetchone()
    if not row:
        return {}
    pack_id, kind, status = row
    from ...storage.db.write_gate import commit_connection, with_db_write
    with with_db_write():
        conn.execute("UPDATE pack_offers SET status=?, updated_at=datetime('now')"
                     " WHERE offer_id=?",
                     ("accepted" if action == "accept" else "dismissed", offer_id))
        if action == "accept":
            enable = 1 if kind == "enable_offer" else 0
            conn.execute("UPDATE pack_registry SET enabled=?, updated_at=datetime('now')"
                         " WHERE pack_id=?", (enable, pack_id))
        commit_connection(conn)
    return {"offer_id": offer_id, "pack_id": pack_id, "kind": kind, "action": action}
