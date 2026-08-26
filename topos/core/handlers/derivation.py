"""Derivation-layer handlers (W4 surfaces) — thin delegates over
features/derivation/surfaces.py (shared with the HTTP routes in api/signal.py)."""

from __future__ import annotations

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
        from ...features.derivation.surfaces import list_packs
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", **list_packs(conn)}}
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
        from ...features.derivation.surfaces import set_pack_enabled
        if not set_pack_enabled(conn, pack_id, enabled):
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
    try:
        from ...features.derivation.surfaces import list_conflicts
        rows = list_conflicts(conn, limit=int(payload.get("limit") or 100))
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
    try:
        from ...features.derivation.surfaces import resolve_conflict
        if not resolve_conflict(conn, cid, status):
            return {"id": req_id, "status": "error", "error": f"unknown conflict {cid}"}
        return {"id": req_id, "status": "ok",
                "payload": {"status": "ok", "conflict_id": cid, "new_status": status}}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("put_pack_offer")
async def handle_put_pack_offer(message):
    req_id = message.get("id")
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    try:
        from ...features.derivation.surfaces import resolve_pack_offer
        out = resolve_pack_offer(conn, str(payload.get("offer_id") or ""),
                                 str(payload.get("action") or ""))
        if not out:
            return {"id": req_id, "status": "error", "error": "unknown offer"}
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", **out}}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("run_pack_backfill")
async def handle_run_pack_backfill(message):
    """Owner-initiated bounded history backfill for one enabled pack (W-B #4).
    Runs in a worker thread; bounded by `limit` hit-records per call."""
    import asyncio as _asyncio
    req_id = message.get("id")
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    pack_id = str(payload.get("pack_id") or "").strip()
    limit = min(int(payload.get("limit") or 200), 1000)
    try:
        from ...features.derivation.surfaces import run_pack_backfill
        stats = await _asyncio.to_thread(run_pack_backfill, conn, pack_id, limit)
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", "pack_id": pack_id, **stats}}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}
