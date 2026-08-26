"""Elevation consent handlers — principal fabric P2, the UMA-ledger piece.

Lifecycle: a client (or the owner on its behalf) FILES a request; the owner
DECIDES it (approve per-scope + expiring, or deny); the owner may REVOKE an
approval later. Decisions and revocations are mirrored into the engine's UMA
audit tables with subject ``client:<id>``, so the one-consent-ledger view holds
in the accounting even though enforcement reads the node-local rows.

Only the REQUEST type is reachable by a third-party principal — and for that
principal the client_id comes from the STAMP, never the payload, so a client
can only ever ask for itself. Decide/revoke/list are owner_only with the same
in-handler third-party refusal as enrollment.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import topos.core.handlers as hub

from .mcp_clients import _refuse_third_party
from .registry import handles


def _uma_mirror(conn, *, client_id: str, scope_id: str, action: str) -> None:
    """Best-effort UMA audit mirror; never blocks the decision itself."""
    try:
        from .common import get_user_id, record_uma_request

        owner = str(get_user_id(conn) or "") or "owner"
        record_uma_request(
            conn,
            owner_user_id=owner,
            resource_id=f"client-elevation:{client_id}:{scope_id}",
            request_type="consent",
            endpoint=action,
            requesting_user_id=f"client:{client_id}",
            app_id=client_id,
            access_context="owner_self",
        )
    except Exception:  # noqa: BLE001
        hub.logger.debug("uma mirror failed for %s %s", action, client_id, exc_info=True)


@handles("mcp_client_request_elevation")
async def handle_mcp_client_request_elevation(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """File a pending elevation request.

    The one lifecycle step a third-party principal may perform — and its
    client_id is read from the channel stamp, never the payload: a client can
    ask for itself, and nothing else.
    """
    req_id = message.get("id")
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    from ...principal import THIRD_PARTY, current_principal

    p = current_principal()
    if p is not None and p.cls == THIRD_PARTY:
        client_id = str(p.client_id or "")
        if not client_id:
            return {"id": req_id, "status": "error",
                    "error": "unenrolled client — enrollment comes before elevation"}
    else:
        client_id = str(payload.get("client_id") or "")
    try:
        from ...mcp_clients import request_elevation

        row = request_elevation(
            conn,
            client_id=client_id,
            scope_id=str(payload.get("scope_id") or ""),
            note=str(payload.get("note") or ""),
        )
        _uma_mirror(conn, client_id=row["client_id"], scope_id=row["scope_id"],
                    action="elevation_requested")
        return {"id": req_id, "status": "ok", "payload": row}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("mcp_client_decide_elevation", owner_only=True)
async def handle_mcp_client_decide_elevation(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Owner decision: approve (per-scope, expiring) or deny. The resolution is
    clamped to the `facts` ceiling regardless of what was requested."""
    req_id = message.get("id")
    refused = _refuse_third_party(req_id)
    if refused:
        return refused
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    try:
        from ...mcp_clients import decide_elevation

        row = decide_elevation(
            conn,
            request_id=int(payload.get("request_id") or 0),
            approve=bool(payload.get("approve")),
            expires_at=str(payload.get("expires_at") or "") or None,
        )
        _uma_mirror(conn, client_id=row["client_id"], scope_id=row["scope_id"],
                    action=f"elevation_{row['status']}")
        return {"id": req_id, "status": "ok", "payload": row}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("mcp_client_revoke_elevation", owner_only=True)
async def handle_mcp_client_revoke_elevation(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    refused = _refuse_third_party(req_id)
    if refused:
        return refused
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    try:
        from ...mcp_clients import revoke_elevation

        count = revoke_elevation(
            conn,
            client_id=str(payload.get("client_id") or ""),
            scope_id=str(payload.get("scope_id") or ""),
        )
        _uma_mirror(conn, client_id=str(payload.get("client_id") or ""),
                    scope_id=str(payload.get("scope_id") or "*"),
                    action="elevation_revoked")
        return {"id": req_id, "status": "ok", "payload": {"revoked": count}}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("mcp_client_list_elevations", owner_only=True)
async def handle_mcp_client_list_elevations(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    refused = _refuse_third_party(req_id)
    if refused:
        return refused
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    try:
        from ...mcp_clients import list_elevations

        rows = list_elevations(conn, client_id=str(payload.get("client_id") or ""))
        return {"id": req_id, "status": "ok", "payload": {"elevations": rows}}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}
