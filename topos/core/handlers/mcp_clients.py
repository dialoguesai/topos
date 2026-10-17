"""Enrolled-client handlers — principal fabric P2 (Settings → Connected apps backend).

All three types are owner_only: enrollment is an owner consent moment, and the
marker keeps them off the CP /mcp tool surface. Defense in depth on top of the
marker: a THIRD_PARTY principal is refused in-handler, so even a future surface
that forgets the owner-only filter cannot let one enrolled client mint another.

The web app renders ONE Connected-apps list from ``mcp_client_list``, so the
list carries both kinds of row — third-party clients (``tpk_`` tokens) and the
owner's own coding agents (``agent-<client>`` rows, class ``owner_agent``,
CONTRACTS §1.1) — each naming its ``principal_class``. ``mcp_client_revoke``
with an ``agent-*`` id is the agent Disconnect (``topos/agent_lifecycle.py``):
revoke, pause every transcript source, remove the label file. Agents are never
minted here: enrolment is ``topos-node agents add`` with a human's yes.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

import topos.core.handlers as hub

from .registry import handles

AGENT_ID_PREFIX = "agent-"


def _agent_rows(conn) -> list:
    """L1's ``list_agents`` rows projected onto the Connected-apps row shape.
    A build without the function (before lane L1 lands) simply has no agents."""
    from ... import mcp_clients

    list_agents = getattr(mcp_clients, "list_agents", None)
    rows = []
    for row in (list_agents(conn) if list_agents else []):
        client_id = str(row.get("client_id") or "")
        if not client_id.startswith(AGENT_ID_PREFIX):
            continue
        rows.append(
            {
                "client_id": client_id,
                "display_name": row.get("display_name") or client_id[len(AGENT_ID_PREFIX):],
                "principal_class": "owner_agent",
                "created_at": row.get("created_at"),
                "last_used_at": row.get("last_used_at"),
                "revoked_at": row.get("revoked_at"),
            }
        )
    return rows


def _disconnect_agent_in_thread(client: str) -> dict:
    """Runs off the event loop: the pause goes over the owner socket and the
    worker thread gets its own connection to the database."""
    from ...agent_lifecycle import disconnect_agent

    return disconnect_agent(hub.get_db_connection(), client).as_dict()


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

        clients = [row for row in list_clients(conn) if not str(row.get("client_id") or "").startswith(AGENT_ID_PREFIX)]
        return {"id": req_id, "status": "ok", "payload": {"clients": clients + _agent_rows(conn)}}
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
    client_id = str(payload.get("client_id") or "")
    try:
        if client_id.startswith(AGENT_ID_PREFIX):
            result = await asyncio.to_thread(_disconnect_agent_in_thread, client_id[len(AGENT_ID_PREFIX):])
            return {"id": req_id, "status": "ok", "payload": result}
        from ...mcp_clients import revoke_client

        revoked = revoke_client(conn, client_id)
        return {"id": req_id, "status": "ok", "payload": {"revoked": bool(revoked)}}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}
