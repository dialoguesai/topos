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


@router.post("/verify_claim")
async def local_verify_claim(
    body: dict = Body(default_factory=dict),
    _: None = Depends(require_api_key),  # noqa: B008
) -> dict:
    """Same-device truth check (PLAN_TRUTHFULNESS_PLUGIN.md). Owner-key only;
    mirrors the CP door: `app_id` is mandatory and `mode` is pinned to fun —
    this route cannot reach a mode the registry doesn't ship. Body:
    {"statement": "...", "app_id": "truth-mirror"}."""
    statement = str(body.get("statement") or "").strip()
    app_id = str(body.get("app_id") or "").strip()
    if not statement or not app_id:
        return {"status": "error", "error": "statement and app_id required"}
    msg = {
        "id": str(uuid.uuid4()),
        "type": "verify_claim",
        "payload": {"statement": statement, "mode": "fun", "caller_app_id": app_id},
    }
    out = await handle_control_plane_request(msg)
    if out.get("status") == "error":
        return {"status": "error", "error": out.get("error", "unknown")}
    return out.get("payload", {})


@router.post("/truth_prompts")
async def local_truth_prompts(
    body: dict = Body(default_factory=dict),
    _: None = Depends(require_api_key),  # noqa: B008
) -> dict:
    """Same-device "ask me" prompt seeds (fun aperture; topics only, no
    stances). Body: {"app_id": "truth-mirror", "limit": 5}."""
    app_id = str(body.get("app_id") or "").strip()
    if not app_id:
        return {"status": "error", "error": "app_id required"}
    msg = {
        "id": str(uuid.uuid4()),
        "type": "truth_prompts",
        "payload": {"mode": "fun", "caller_app_id": app_id,
                    "limit": int(body.get("limit") or 5)},
    }
    out = await handle_control_plane_request(msg)
    if out.get("status") == "error":
        return {"status": "error", "error": out.get("error", "unknown")}
    return out.get("payload", {})


@router.post("/truth_seed_fact")
async def local_truth_seed_fact(
    body: dict = Body(default_factory=dict),
    _: None = Depends(require_api_key),  # noqa: B008
) -> dict:
    """Owner adds a fun fact to their own sheet (refused outside the fun
    aperture). Body: {"predicate": "favorite_food", "value": "tacos",
    "app_id": "truth-mirror"}."""
    app_id = str(body.get("app_id") or "").strip()
    if not app_id:
        return {"status": "error", "error": "app_id required"}
    msg = {
        "id": str(uuid.uuid4()),
        "type": "truth_seed_fact",
        "payload": {
            "mode": "fun",
            "caller_app_id": app_id,
            "predicate": str(body.get("predicate") or ""),
            "value": str(body.get("value") or ""),
        },
    }
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
