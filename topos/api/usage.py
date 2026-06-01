"""Engine-owned request counts: UMA + MCP. GET /api/request-counts (requires API key)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from ..auth import require_api_key
from ..core.handlers import handle_control_plane_request

router = APIRouter(prefix="/api", tags=["usage"])


@router.get("/request-counts")
async def get_request_counts(
    owner_user_id: str | None = Query(None, description="Resource owner (default: engine's linked user)"),
    since_days: int = Query(90, ge=1, le=365),
    _: None = Depends(require_api_key),  # noqa: B008
) -> dict:
    """
    Return UMA and MCP request counts from the engine's DB.
    Same data as get_request_counts message type (for direct frontend or CP proxy).
    """
    import uuid
    msg = {
        "id": str(uuid.uuid4()),
        "type": "get_request_counts",
        "payload": {"owner_user_id": owner_user_id or "", "since_days": since_days},
    }
    out = await handle_control_plane_request(msg)
    if out.get("status") == "error":
        return {
            "uma": {
                "total_read_requests": 0,
                "total_write_requests": 0,
                "by_app": [],
                "by_requesting_user": [],
                "access_attribution": {
                    "window_days": since_days,
                    "owner_self_reads": 0,
                    "owner_self_writes": 0,
                    "grantee_reads": 0,
                    "grantee_writes": 0,
                    "unknown_reads": 0,
                    "unknown_writes": 0,
                },
            },
            "mcp": {"by_source": [], "by_tool": [], "by_access_context": [], "total": 0},
        }
    return out.get("payload", {})
