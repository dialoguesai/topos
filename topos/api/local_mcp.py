"""Local API for MCP-style tools (no Control Plane). Same auth as engine; for same-device/offline use."""

from __future__ import annotations

import json
import asyncio
import uuid

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Response

from ..auth import resolve_request_principal
from ..core.handlers import handle_control_plane_request

router = APIRouter(prefix="/api/local", tags=["local-mcp"])


def _local_mcp_payload(extra: dict | None = None) -> dict:
    """Payload for local MCP requests; source=claude_desktop so engine counts per source."""
    p = {"mcp_source": "claude_desktop"}
    if extra:
        p.update(extra)
    return p


def _truth_door(msg_type: str):
    """The truth routes' principal dependency: the handler's own door decision
    (topos/query/truth_door.py), answered as an HTTP 403 before the route reads
    the body instead of a 200 error body after it."""

    def admitted(principal=Depends(resolve_request_principal)):  # noqa: B008
        from ..query.truth_door import truth_door_refusal

        refused = truth_door_refusal(principal, msg_type)
        if refused:
            raise HTTPException(status_code=refused["code"], detail=refused["error"])
        return principal

    return admitted


@router.post("/list_database_tables")
async def local_list_database_tables(principal=Depends(resolve_request_principal)) -> dict:  # noqa: B008
    """List tables (same as CP-forwarded tool). Requires Bearer TOPOS_KEY."""
    msg = {"id": str(uuid.uuid4()), "type": "list_database_tables", "payload": _local_mcp_payload()}
    out = await handle_control_plane_request(msg, principal=principal)
    if out.get("status") == "error":
        return {"status": "error", "error": out.get("error", "unknown")}
    return out.get("payload", {})


@router.post("/verify_claim")
async def local_verify_claim(
    request: Request,
    body: dict = Body(default_factory=dict),
    principal=Depends(_truth_door("verify_claim")),  # noqa: B008
) -> dict:
    """Same-device truth check (PLAN_TRUTHFULNESS_PLUGIN.md). The owner socket,
    or an enrolled client the owner lists in TOPOS_TRUTH_CLIENT_ALLOWLIST; any
    bearer on TCP otherwise gets 403 owner_mode_required — the owner key there
    is a third party too. Mirrors the CP door: `app_id` is mandatory and `mode`
    is optional, with no fun mode unless the caller names it (DR-64). Body:
    {"statement": "...", "app_id": "truth-mirror", "mode": "fun"}."""
    statement = str(body.get("statement") or "").strip()
    app_id = str(body.get("app_id") or "").strip()
    if not statement or not app_id:
        return {"status": "error", "error": "statement and app_id required"}
    payload = {"statement": statement, "caller_app_id": app_id}
    if body.get("mode"):
        payload["mode"] = str(body["mode"])
    if "deadline_s" in body:
        payload["deadline_s"] = body["deadline_s"]
    msg = {"id": str(uuid.uuid4()), "type": "verify_claim", "payload": payload}

    async def disconnected():
        while (await request.receive())["type"] != "http.disconnect":
            pass

    work = asyncio.create_task(handle_control_plane_request(msg, principal=principal))
    gone = asyncio.create_task(disconnected())
    try:
        done, _ = await asyncio.wait((work, gone), return_when=asyncio.FIRST_COMPLETED)
        if work not in done:
            raise HTTPException(status_code=499, detail="Caller disconnected")
        out = await work
    finally:
        for future in (work, gone):
            if not future.done():
                future.cancel()
        await asyncio.gather(work, gone, return_exceptions=True)
    if out.get("status") == "error":
        from ..engine.overload import unavailable_from_detail

        unavailable = unavailable_from_detail(out.get("error_code"), {
            "code": out.get("error_type"), "retry_after_seconds": out.get("retry_after_seconds"),
        })
        if unavailable is not None:
            detail = unavailable.detail()
            if out.get("termination_reason") in {"cancelled", "deadline_exceeded"}:
                detail["reason"] = out["termination_reason"]
            raise HTTPException(status_code=unavailable.status_code, detail=detail, headers=unavailable.headers())
        return {"status": "error", "error": out.get("error", "unknown")}
    return out.get("payload", {})


@router.post("/verify_claim_owner")
async def local_verify_claim_owner(
    request: Request,
    response: Response,
    principal=Depends(_truth_door("verify_claim_owner")),  # noqa: B008
) -> dict:
    """verify_claim v2, owner socket only (VERIFY_CLAIM_V2_DESIGN.md). Body: {"statement": "...", "context":
    [{"speaker": "...", "text": "..."}], "said_at": "ISO 8601 with offset", "speaker": "owner", "effort": "high",
    "deadline_s": 10}. The body is read only after the door has admitted the caller; errors never echo it."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(422, "invalid_truth_request", headers={"Cache-Control": "no-store"}) from None
    response.headers["Cache-Control"] = "no-store"
    correlation_id = str(uuid.uuid4())
    response.headers["X-Request-ID"] = correlation_id
    msg = {"id": correlation_id, "type": "verify_claim_owner", "payload": body if isinstance(body, dict) else {}}
    out = await handle_control_plane_request(msg, principal=principal)
    if out.get("status") == "error":
        return {"status": "error", "error": out.get("error", "unknown")}
    return out.get("payload", {})


async def _owner_truth_door(request: Request, response: Response, principal, msg_type: str) -> dict:
    """The inbound truth lane's owner doors (VERIFY_CLAIM_WEEKLY_PROGRAM.md §2a): the body is read only after the
    truth door admitted the caller (the route's dependency); an empty body is ``{}``; errors never echo it."""
    raw = await request.body()
    try:
        body = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(422, "invalid_truth_request", headers={"Cache-Control": "no-store"}) from None
    response.headers["Cache-Control"] = "no-store"
    msg = {"id": str(uuid.uuid4()), "type": msg_type, "payload": body if isinstance(body, dict) else None}
    out = await handle_control_plane_request(msg, principal=principal)
    if out.get("status") == "error":
        return {"status": "error", "error": out.get("error", "unknown")}
    return out.get("payload", {})


@router.post("/truth_inbound_settings")
async def local_truth_inbound_settings(
    request: Request,
    response: Response,
    principal=Depends(_truth_door("truth_inbound_settings")),  # noqa: B008
) -> dict:
    """The owner's inbound truth lane settings. Body: {"get": true} or {"set": {"enabled": true, "sources":
    ["imessage", "signal", "transcripts", "journal"], "model": "local", "rate_per_minute": 6}} (any subset)."""
    return await _owner_truth_door(request, response, principal, "truth_inbound_settings")


@router.post("/truth_weekly_stats")
async def local_truth_weekly_stats(
    request: Request,
    response: Response,
    principal=Depends(_truth_door("truth_weekly_stats")),  # noqa: B008
) -> dict:
    """Counts per ISO week from the inbound truth lane, newest first; no claim text. Body: {"weeks": 1}."""
    return await _owner_truth_door(request, response, principal, "truth_weekly_stats")


@router.post("/truth_claim_cards")
async def local_truth_claim_cards(
    request: Request,
    response: Response,
    principal=Depends(_truth_door("truth_claim_cards")),  # noqa: B008
) -> dict:
    """The claims the lane found, for the owner to label. Body: {"limit": 20, "unlabelled_only": true}."""
    return await _owner_truth_door(request, response, principal, "truth_claim_cards")


@router.post("/truth_claim_label")
async def local_truth_claim_label(
    request: Request,
    response: Response,
    principal=Depends(_truth_door("truth_claim_label")),  # noqa: B008
) -> dict:
    """The owner's label of one claim. Body: {"check_id": "...", "label": "true"|"partly"|"false", "note": "..."}."""
    return await _owner_truth_door(request, response, principal, "truth_claim_label")


@router.post("/truth_prompts")
async def local_truth_prompts(
    body: dict = Body(default_factory=dict),
    principal=Depends(_truth_door("truth_prompts")),  # noqa: B008
) -> dict:
    """Same-device "ask me" prompt seeds (fun aperture; topics only, no
    stances). Same door as verify_claim. Body: {"app_id": "truth-mirror", "limit": 5}."""
    app_id = str(body.get("app_id") or "").strip()
    if not app_id:
        return {"status": "error", "error": "app_id required"}
    msg = {
        "id": str(uuid.uuid4()),
        "type": "truth_prompts",
        "payload": {"mode": "fun", "caller_app_id": app_id,
                    "limit": int(body.get("limit") or 5)},
    }
    out = await handle_control_plane_request(msg, principal=principal)
    if out.get("status") == "error":
        return {"status": "error", "error": out.get("error", "unknown")}
    return out.get("payload", {})


@router.post("/truth_seed_fact")
async def local_truth_seed_fact(
    body: dict = Body(default_factory=dict),
    principal=Depends(_truth_door("truth_seed_fact")),  # noqa: B008
) -> dict:
    """Owner adds a fun fact to their own sheet (refused outside the fun
    aperture). Owner socket only: the fact is stored as owner-stated, so no
    enrolled client and no bearer on TCP may author it. Body:
    {"predicate": "favorite_food", "value": "tacos", "app_id": "truth-mirror"}."""
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
    out = await handle_control_plane_request(msg, principal=principal)
    if out.get("status") == "error":
        return {"status": "error", "error": out.get("error", "unknown")}
    return out.get("payload", {})


@router.post("/get_table_schema")
async def local_get_table_schema(
    body: dict = Body(default_factory=dict),
    principal=Depends(resolve_request_principal),  # noqa: B008
) -> dict:
    """Get table schema (same as CP-forwarded tool). Body: {"table_name": "..."}. Requires Bearer TOPOS_KEY."""
    table_name = (body.get("table_name") or "").strip()
    if not table_name:
        return {"status": "error", "error": "table_name required"}
    msg = {"id": str(uuid.uuid4()), "type": "get_table_schema", "payload": _local_mcp_payload({"table_name": table_name})}
    out = await handle_control_plane_request(msg, principal=principal)
    if out.get("status") == "error":
        return {"status": "error", "error": out.get("error", "unknown")}
    return out.get("payload", {})
