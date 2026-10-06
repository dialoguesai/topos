"""Owner socket: keep one source's data only from a date onward (``topos.sources.retention``).

On the node's owner socket only (the 0600 UDS; no bearer, no relay). Every answer is counts and
dates; no message id, text or name leaves the node.

``POST /v1/sources/retention`` sets a source's retention floor and removes what is older. Body:
``{"source_id": "imessage", "keep_since": "2026-01-01", "dry_run": true|false, "batch_size": n,
"pause_seconds": s, "window_days": n}``. ``dry_run`` defaults to true, so a bare request only
counts. ``{"source_id": ..., "clear": true, "dry_run": false}`` lifts the floor (nothing comes
back by itself). After a real run that removed rows, the signed message-search indexes are
rebuilt through the owner's own rebuild, so a grant indexes what remains.

``GET /v1/sources/retention`` lists the floors and their state.

``POST /v1/sources/retention/compact`` reports what compacting the database would reclaim and
whether the volume has room (``dry_run`` defaults to true); a real run refuses when it has not.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from topos.auth import resolve_request_principal

router = APIRouter(prefix="/v1/sources/retention", tags=["sources-owner-maintenance"])

_NO_STORE = {"Cache-Control": "no-store"}
_FIELDS = frozenset({"source_id", "keep_since", "dry_run", "clear", "batch_size", "pause_seconds", "window_days"})
_COMPACT_FIELDS = frozenset({"dry_run"})
_CONFLICTS = frozenset({"retention_source_busy", "retention_sync_in_progress"})


def _owner(principal) -> None:
    from topos.principal import OWNER_APP

    if principal is None or principal.cls != OWNER_APP or principal.channel != "uds":
        raise HTTPException(403, "owner_socket_required", headers=_NO_STORE)


def _invalid(code: str = "source_retention_payload_invalid") -> HTTPException:
    return HTTPException(400, code, headers=_NO_STORE)


def _number(value: Any) -> bool:
    return value is None or (not isinstance(value, bool) and isinstance(value, (int, float)))


def _conn():
    from topos.core.state import get_db_connection

    conn = get_db_connection()
    if conn is None:
        raise RuntimeError("database unavailable")
    return conn


async def _refresh_grant_indexes(principal) -> Dict[str, Any]:
    """The owner's own message-search rebuild, after the removal committed.

    The same door as ``POST /v1/sharing/message-search/rebuild`` and the same
    switch as every owner change that can move what a grant releases. Without it, the
    index sweeper still drops any index whose member rows changed (fail closed), and a
    recipient search refuses until the next rebuild.
    """
    from topos.permissions_v2 import switches

    if not switches.on(switches.MESSAGE_SEARCH):
        return {"status": "skipped", "reason": "message_search_disabled"}
    from topos.core.handlers.permissions_v2 import handle_permissions_v2_message_search_rebuild
    from topos.principal import reset_principal, set_principal

    token = set_principal(principal)
    try:
        result = await handle_permissions_v2_message_search_rebuild({"id": "source-retention-search-rebuild"})
    except Exception:  # noqa: BLE001 -- the removal stands; the sweeper fails closed
        return {"status": "failed", "error": "message_search_rebuild_failed"}
    finally:
        reset_principal(token)
    if result.get("status") != "ok":
        return {"status": "failed", "error": str(result.get("error") or "message_search_rebuild_failed")}
    return {"status": "rebuilt", **(result.get("payload") or {})}


@router.get("")
async def list_floors(principal=Depends(resolve_request_principal)):
    _owner(principal)
    from topos.sources.retention import describe_floors

    try:
        floors = await asyncio.to_thread(lambda: describe_floors(_conn()))
    except Exception:  # noqa: BLE001 -- no path or exception text leaves the node
        raise HTTPException(503, "source_retention_unavailable", headers=_NO_STORE) from None
    return JSONResponse({"floors": floors}, headers=_NO_STORE)


@router.post("")
async def source_retention(payload: Optional[dict] = Body(None), principal=Depends(resolve_request_principal)):
    _owner(principal)
    from topos.sources.retention import RetentionError, apply_retention, clear_retention_floor, plan_retention

    body = {} if payload is None else payload
    if not isinstance(body, dict) or not set(body) <= _FIELDS:
        raise _invalid()
    dry_run = body.get("dry_run", True)
    clear = body.get("clear", False)
    source_id = body.get("source_id")
    if type(dry_run) is not bool or type(clear) is not bool or type(source_id) is not str:
        raise _invalid()
    if not all(_number(body.get(key)) for key in ("batch_size", "pause_seconds", "window_days")):
        raise _invalid()
    if clear and ("keep_since" in body or dry_run):
        raise _invalid()
    if not clear and type(body.get("keep_since")) is not str:
        raise _invalid()

    def run() -> Dict[str, Any]:
        conn = _conn()
        if clear:
            return {"source_id": source_id, "cleared": clear_retention_floor(conn, source_id)}
        if dry_run:
            return plan_retention(conn, source_id, body["keep_since"], window_days=body.get("window_days") or 90)
        return apply_retention(conn, source_id, body["keep_since"],
                               batch_size=body.get("batch_size") or 1000,
                               pause_seconds=body.get("pause_seconds") or 0.0)

    try:
        result = await asyncio.to_thread(run)
    except RetentionError as exc:
        raise HTTPException(409 if exc.code in _CONFLICTS else 400, exc.code, headers=_NO_STORE) from None
    except Exception:  # noqa: BLE001 -- no path, row or exception text leaves the node
        raise HTTPException(503, "source_retention_unavailable", headers=_NO_STORE) from None
    if not clear and not dry_run and result.get("rows_removed"):
        result["grant_indexes"] = await _refresh_grant_indexes(principal)
    return JSONResponse(result, headers=_NO_STORE)


@router.post("/compact")
async def compact(payload: Optional[dict] = Body(None), principal=Depends(resolve_request_principal)):
    _owner(principal)
    from topos.sources.retention import RetentionError, compact_database

    body = {} if payload is None else payload
    if not isinstance(body, dict) or not set(body) <= _COMPACT_FIELDS:
        raise _invalid("source_retention_compact_payload_invalid")
    dry_run = body.get("dry_run", True)
    if type(dry_run) is not bool:
        raise _invalid("source_retention_compact_payload_invalid")
    try:
        result = await asyncio.to_thread(lambda: compact_database(_conn(), dry_run=dry_run))
    except RetentionError as exc:
        raise HTTPException(400, exc.code, headers=_NO_STORE) from None
    except Exception:  # noqa: BLE001
        raise HTTPException(503, "source_retention_compact_unavailable", headers=_NO_STORE) from None
    return JSONResponse(result, headers=_NO_STORE)
