"""Enrolled-client handlers — principal fabric P2 (Settings → Connected apps backend).

All three types are owner_only: enrollment is an owner consent moment, and the
marker keeps them off the CP /mcp tool surface. Defense in depth on top of the
marker: a THIRD_PARTY principal is refused in-handler, so even a future surface
that forgets the owner-only filter cannot let one enrolled client mint another.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import topos.core.handlers as hub

from .registry import handles


def _refuse_third_party(req_id: Any) -> Optional[Dict[str, Any]]:
    from ...principal import THIRD_PARTY, current_principal

    p = current_principal()
    if p is not None and p.cls == THIRD_PARTY:
        return {"id": req_id, "status": "error",
                "error": "client enrollment is an owner action"}
    return None


@handles("mcp_client_enroll", owner_only=True)
async def handle_mcp_client_enroll(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Mint (or rotate) a per-client token. The plaintext appears in this
    response ONCE and is never retrievable again — only its hash is stored."""
    req_id = message.get("id")
    refused = _refuse_third_party(req_id)
    if refused:
        return refused
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    try:
        from ...mcp_clients import mint_client_token

        row = mint_client_token(
            conn,
            client_id=str(payload.get("client_id") or ""),
            display_name=str(payload.get("display_name") or ""),
        )
        row.pop("token_hash", None)
        return {"id": req_id, "status": "ok", "payload": row}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("mcp_client_list", owner_only=True)
async def handle_mcp_client_list(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    refused = _refuse_third_party(req_id)
    if refused:
        return refused
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    try:
        from ...mcp_clients import list_clients

        return {"id": req_id, "status": "ok", "payload": {"clients": list_clients(conn)}}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("mcp_client_revoke", owner_only=True)
async def handle_mcp_client_revoke(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Tombstone, not delete: the audit trail keeps naming the client."""
    req_id = message.get("id")
    refused = _refuse_third_party(req_id)
    if refused:
        return refused
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    payload = message.get("payload") or {}
    try:
        from ...mcp_clients import revoke_client

        revoked = revoke_client(conn, str(payload.get("client_id") or ""))
        return {"id": req_id, "status": "ok", "payload": {"revoked": bool(revoked)}}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}
