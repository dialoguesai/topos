"""Local API for MCP-style tools (no Control Plane). Same auth as engine; for same-device/offline use."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Body, Depends

from ..auth import require_api_key
from ..core.handlers import handle_control_plane_request

router = APIRouter(prefix="/api/local", tags=["local-mcp"])


def _local_mcp_payload(extra: dict | None = None) -> dict:
    """Payload for local MCP requests; source=claude_desktop so engine counts per source."""
    p = {"mcp_source": "claude_desktop"}
    if extra:
        p.update(extra)
    return p


@router.post("/list_database_tables")
async def local_list_database_tables(_: None = Depends(require_api_key)) -> dict:  # noqa: B008
    """List tables (same as CP-forwarded tool). Requires Bearer TOPOS_KEY."""
    msg = {"id": str(uuid.uuid4()), "type": "list_database_tables", "payload": _local_mcp_payload()}
    out = await handle_control_plane_request(msg)
    if out.get("status") == "error":
        return {"status": "error", "error": out.get("error", "unknown")}
    return out.get("payload", {})


@router.post("/get_table_schema")
async def local_get_table_schema(
    body: dict = Body(default_factory=dict),
    _: None = Depends(require_api_key),  # noqa: B008
) -> dict:
    """Get table schema (same as CP-forwarded tool). Body: {"table_name": "..."}. Requires Bearer TOPOS_KEY."""
    table_name = (body.get("table_name") or "").strip()
    if not table_name:
        return {"status": "error", "error": "table_name required"}
    msg = {"id": str(uuid.uuid4()), "type": "get_table_schema", "payload": _local_mcp_payload({"table_name": table_name})}
    out = await handle_control_plane_request(msg)
    if out.get("status") == "error":
        return {"status": "error", "error": out.get("error", "unknown")}
    return out.get("payload", {})
