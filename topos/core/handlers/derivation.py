"""Derivation-layer handlers (W4 surfaces): pack registry + conflicts queue.

get_derivation_packs   — registry rows joined with live fact counts and the
                         quarantine (fact_conflicts) count per pack: the lens
                         catalog page (W4.3) renders straight from this.
put_derivation_pack    — enable/disable one pack (owner toggle; W2.2 registry).
get_fact_conflicts     — pending quarantine/conflict rows (W4.2 review queue).
put_fact_conflict      — resolve one row (status: dismissed | accepted).
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

import topos.core.handlers as hub
from .registry import handles


@handles("get_derivation_packs")
async def handle_get_derivation_packs(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    try:
        from ...features.derivation.packs import load_packs
        from ...features.derivation.registry import bundled_pack_dir, seed_pack_registry

        seed_pack_registry(conn, bundled_pack_dir())
        catalog = load_packs(bundled_pack_dir())
        counts = dict(conn.execute(
            "SELECT ontology_id, COUNT(*) FROM signal_objects"
            " WHERE object_type='fact' AND ontology_id IS NOT NULL AND valid_to IS NULL"
            " GROUP BY ontology_id").fetchall())
        conflict_counts = dict(conn.execute(
            "SELECT predicate, COUNT(*) FROM fact_conflicts WHERE status='pending'"
            " GROUP BY predicate").fetchall())
        packs = []
        for pid, ver, enabled, disclosure, last_run in conn.execute(
                "SELECT pack_id, version, enabled, disclosure_default, last_run_at"
                " FROM pack_registry ORDER BY pack_id"):
            p = catalog.get(pid)
            pending = sum(n for pred, n in conflict_counts.items()
                          if pred.split(".")[0] == pid.split(".")[0])
            packs.append({
                "pack_id": pid, "version": ver, "enabled": bool(enabled),
                "disclosure_default": disclosure,
                "title": getattr(p, "title", pid) if p else pid,
                "sensitivity_class": getattr(p, "sensitivity_class", "personal") if p else "personal",
                "fact_count": counts.get(pid, 0),
                "pending_conflicts": pending,
                "last_run_at": last_run,
                "predicates": len(getattr(p, "predicates", {}) or {}) if p else 0,
            })
        return {"id": req_id, "status": "ok",
                "payload": {"status": "ok", "packs": packs,
                            "total_conflicts": sum(conflict_counts.values())}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("put_derivation_pack")
async def handle_put_derivation_pack(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    pack_id = str(payload.get("pack_id") or "").strip()
    enabled = payload.get("enabled")
    if not pack_id or not isinstance(enabled, bool):
        return {"id": req_id, "status": "error", "error": "pack_id and enabled (bool) required"}
    try:
        from ...storage.db.write_gate import commit_connection, with_db_write
        with with_db_write():
            cur = conn.execute(
                "UPDATE pack_registry SET enabled=?, updated_at=datetime('now') WHERE pack_id=?",
                (1 if enabled else 0, pack_id))
            commit_connection(conn)
        if not cur.rowcount:
            return {"id": req_id, "status": "error", "error": f"unknown pack {pack_id}"}
        return {"id": req_id, "status": "ok",
                "payload": {"status": "ok", "pack_id": pack_id, "enabled": enabled}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("get_fact_conflicts")
async def handle_get_fact_conflicts(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    limit = min(int(payload.get("limit") or 100), 500)
    try:
        rows = []
        for cid, subj, pred, incumbent, value, conf, status, created in conn.execute(
                "SELECT conflict_id, subject_entity_id, predicate, incumbent_object_id,"
                " challenger_value, challenger_confidence, status, created_at"
                " FROM fact_conflicts WHERE status='pending'"
                " ORDER BY created_at DESC LIMIT ?", (limit,)):
            try:
                val = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                val = value
            rows.append({"conflict_id": cid, "subject_entity_id": subj, "predicate": pred,
                         "kind": ("quarantine" if str(incumbent).startswith("quarantine:") else "conflict"),
                         "reason": (str(incumbent)[len("quarantine:"):]
                                    if str(incumbent).startswith("quarantine:") else None),
                         "incumbent_object_id": incumbent, "value": val,
                         "confidence": conf, "created_at": created})
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", "conflicts": rows}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("put_fact_conflict")
async def handle_put_fact_conflict(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    cid = str(payload.get("conflict_id") or "").strip()
    status = str(payload.get("status") or "").strip()
    if not cid or status not in ("dismissed", "accepted"):
        return {"id": req_id, "status": "error",
                "error": "conflict_id and status (dismissed|accepted) required"}
    try:
        from ...storage.db.write_gate import commit_connection, with_db_write
        with with_db_write():
            cur = conn.execute("UPDATE fact_conflicts SET status=? WHERE conflict_id=?",
                               (status, cid))
            commit_connection(conn)
        if not cur.rowcount:
            return {"id": req_id, "status": "error", "error": f"unknown conflict {cid}"}
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", "conflict_id": cid,
                                                          "new_status": status}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}
