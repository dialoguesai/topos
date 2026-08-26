"""Connected apps REST — the engine-direct lane for Settings → Connected apps.

Thin wrappers over the owner-only mcp_client_* message types (P2). The CP
mirrors these paths at /v1/topos/{id}/signal/connected-apps/* via the signal
proxy, so the FE's kind-gated signal client serves both targets. Auth resolves
the channel principal: an enrolled client's tpk token authenticates here but
every handler refuses THIRD_PARTY for the owner actions — a client cannot list,
mint, or approve anything through this surface.
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, Query

from ..auth import resolve_request_principal
from ..core.handlers import handle_control_plane_request

router = APIRouter(prefix="/signal/connected-apps", tags=["connected-apps"])


async def _dispatch(msg_type: str, payload: Dict[str, Any], principal) -> Dict[str, Any]:
    out: Optional[Dict[str, Any]] = await handle_control_plane_request(
        {"id": str(uuid.uuid4()), "type": msg_type, "payload": payload},
        principal=principal,
    )
    if not out or out.get("status") != "ok":
        return {"status": "error", "error": str((out or {}).get("error") or "engine error")}
    return {"status": "ok", **(out.get("payload") or {})}


@router.get("")
async def list_connected_apps(principal=Depends(resolve_request_principal)) -> dict:  # noqa: B008
    return await _dispatch("mcp_client_list", {}, principal)


@router.post("/enroll")
async def enroll_connected_app(
    body: dict = Body(default_factory=dict),
    principal=Depends(resolve_request_principal),  # noqa: B008
) -> dict:
    return await _dispatch(
        "mcp_client_enroll",
        {"client_id": body.get("client_id"), "display_name": body.get("display_name")},
        principal,
    )


@router.post("/revoke")
async def revoke_connected_app(
    body: dict = Body(default_factory=dict),
    principal=Depends(resolve_request_principal),  # noqa: B008
) -> dict:
    return await _dispatch("mcp_client_revoke", {"client_id": body.get("client_id")}, principal)


@router.get("/elevations")
async def list_elevations(
    client_id: str = Query("", description="Filter to one app (default: all)"),
    principal=Depends(resolve_request_principal),  # noqa: B008
) -> dict:
    return await _dispatch("mcp_client_list_elevations", {"client_id": client_id}, principal)


@router.post("/elevations/decide")
async def decide_elevation(
    body: dict = Body(default_factory=dict),
    principal=Depends(resolve_request_principal),  # noqa: B008
) -> dict:
    return await _dispatch(
        "mcp_client_decide_elevation",
        {"request_id": body.get("request_id"), "approve": body.get("approve"),
         "expires_at": body.get("expires_at")},
        principal,
    )


@router.post("/elevations/revoke")
async def revoke_elevation(
    body: dict = Body(default_factory=dict),
    principal=Depends(resolve_request_principal),  # noqa: B008
) -> dict:
    return await _dispatch(
        "mcp_client_revoke_elevation",
        {"client_id": body.get("client_id"), "scope_id": body.get("scope_id")},
        principal,
    )
