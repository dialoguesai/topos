"""Owner socket: the AI-coding-tool sources (CONTRACTS.md §2.8).

On the node's owner socket only (the 0600 UDS; no bearer, no relay), like ``source_retention``.
Every answer is counts, flags, ids and stamps; no transcript text leaves the node.

``GET /api/local/owner/scope`` → ``{user_id, topos_id, dataset_id}`` from the bound identity (else
the stored ``topos_id``), or 409 ``not_paired``.
``POST /api/local/owner/sources/{id}/preview {skip: [dir…]}`` → counts only, nothing written.
``POST …/enable {skip: [], backfill: "all"|"30d"}`` → the canonical install row, the settings row,
an hourly schedule (``interval_hours=1``) and the backfill job; 409 with the plain sentence when
unpaired or below the disk floor.
``POST …/pause`` / ``POST …/resume`` → the ``paused`` flag the sync door and the scheduler refuse on.
``POST …/sync {session_path?}`` → one since-last sync, 409 ``source_not_enabled`` / ``source_paused``
unless the source is enabled in the active profile; the dataset always comes from the identity
and never from the body.
``GET …/sources/{id}`` → the status block the CLI shows.
``POST …/reclassify-touches {apply?: bool}`` → re-judge stored replies under the read / system-touch rule
(owner ruling 20 Oct 2026): counts only; ``apply`` clears the flag where no read came first. Owner only —
never the enrolled agent — because it widens what can be shared.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from topos.auth import resolve_request_principal

logger = logging.getLogger("topos.api.owner_sources")

router = APIRouter(prefix="/api/local/owner", tags=["sources-owner"])

_NO_STORE = {"Cache-Control": "no-store"}
_PREVIEW_FIELDS = frozenset({"skip"})
_ENABLE_FIELDS = frozenset({"skip", "backfill"})
_SYNC_FIELDS = frozenset({"session_path"})
_RECLASSIFY_FIELDS = frozenset({"apply"})


def _owner(principal, *, reads: bool = False) -> None:
    """The owner on the socket; with ``reads``, the owner's enrolled coding agent too (CONTRACTS §1.2 puts
    ``GET /scope`` and the status block on its route list: the MCP server's session context and
    sources_status call them with the agent's label). Every change stays the owner's, and the agent class
    is minted on the socket only, so the same word on any other channel is nobody."""
    from topos.principal import OWNER_AGENT, OWNER_APP

    allowed = (OWNER_APP, OWNER_AGENT) if reads else (OWNER_APP,)
    if principal is None or principal.cls not in allowed or principal.channel != "uds":
        raise HTTPException(403, "owner_socket_required", headers=_NO_STORE)


def _conn():
    from topos.core.state import get_db_connection

    conn = get_db_connection()
    if conn is None:
        raise RuntimeError("database unavailable")
    return conn


def _refusal(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status, headers=_NO_STORE)


def _ai_source(source_id: str) -> str:
    from topos.ingestion.ai_tool_sync import SOURCE_ID_TO_TOOL

    sid = str(source_id or "").strip()
    if sid not in SOURCE_ID_TO_TOOL:
        raise HTTPException(404, "unknown_source", headers=_NO_STORE)
    return sid


def _skip_list(body: Dict[str, Any]) -> list:
    skip = body.get("skip", [])
    if skip is None:
        return []
    if not isinstance(skip, list) or any(not isinstance(item, str) for item in skip):
        raise HTTPException(400, "owner_sources_payload_invalid", headers=_NO_STORE)
    return [item.strip() for item in skip if item.strip()]


def _body(payload: Optional[dict], allowed: frozenset) -> Dict[str, Any]:
    body = {} if payload is None else payload
    if not isinstance(body, dict):
        raise HTTPException(400, "owner_sources_payload_invalid", headers=_NO_STORE)
    # A dataset in the body is ignored, never an error: the identity decides (security finding 15).
    unknown = set(body) - allowed - {"dataset_id"}
    if unknown:
        raise HTTPException(400, "owner_sources_payload_invalid", headers=_NO_STORE)
    return body


@router.get("/scope")
async def owner_scope(principal=Depends(resolve_request_principal)):
    _owner(principal, reads=True)
    from topos.ingestion.ai_tool_sync import NOT_PAIRED, NOT_PAIRED_SENTENCE, resolve_owner_scope

    try:
        scope = await asyncio.to_thread(lambda: resolve_owner_scope(_conn()))
    except Exception:  # noqa: BLE001 -- no path or exception text leaves the node
        raise HTTPException(503, "owner_scope_unavailable", headers=_NO_STORE) from None
    if scope is None:
        return _refusal(409, NOT_PAIRED, NOT_PAIRED_SENTENCE)
    return JSONResponse(scope, headers=_NO_STORE)


@router.get("/sources/{source_id}")
async def source_status(source_id: str, principal=Depends(resolve_request_principal)):
    _owner(principal, reads=True)
    sid = _ai_source(source_id)
    from topos.ingestion.ai_tool_sync import describe_ai_tool_source

    try:
        status = await asyncio.to_thread(lambda: describe_ai_tool_source(_conn(), sid))
    except Exception:  # noqa: BLE001
        raise HTTPException(503, "owner_sources_unavailable", headers=_NO_STORE) from None
    return JSONResponse(status, headers=_NO_STORE)


@router.post("/sources/{source_id}/preview")
async def preview(source_id: str, payload: Optional[dict] = Body(None), principal=Depends(resolve_request_principal)):
    _owner(principal)
    sid = _ai_source(source_id)
    body = _body(payload, _PREVIEW_FIELDS)
    skip = _skip_list(body)
    from topos.ingestion.ai_tool_sync import READER_UNAVAILABLE, preview_ai_tool_source

    try:
        counts = await asyncio.to_thread(lambda: preview_ai_tool_source(_conn(), sid, skip=skip))
    except (ImportError, AttributeError):
        return _refusal(503, READER_UNAVAILABLE, "This build can't read these transcripts yet.")
    except Exception:  # noqa: BLE001 -- no path, row or exception text leaves the node
        raise HTTPException(503, "owner_sources_preview_unavailable", headers=_NO_STORE) from None
    return JSONResponse(counts, headers=_NO_STORE)


@router.post("/sources/{source_id}/enable")
async def enable(source_id: str, payload: Optional[dict] = Body(None), principal=Depends(resolve_request_principal)):
    _owner(principal)
    sid = _ai_source(source_id)
    body = _body(payload, _ENABLE_FIELDS)
    skip = _skip_list(body)
    backfill = body.get("backfill", "all")
    from topos.ingestion.ai_tool_sync import BACKFILL_CHOICES, AiToolSyncRefused, enable_ai_tool_source

    if not isinstance(backfill, str) or backfill not in BACKFILL_CHOICES:
        raise HTTPException(400, "owner_sources_payload_invalid", headers=_NO_STORE)

    def run() -> Dict[str, Any]:
        return enable_ai_tool_source(_conn(), sid, skip=skip, backfill=backfill)

    try:
        result = await asyncio.to_thread(run)
    except AiToolSyncRefused as exc:
        return _refusal(exc.status, exc.code, exc.message)
    except ValueError as exc:
        raise HTTPException(400, str(exc), headers=_NO_STORE) from None
    except Exception as exc:  # noqa: BLE001 -- the kind only; no path, row or exception text leaves the node
        logger.warning("enable %s failed: %s", sid, type(exc).__name__)
        raise HTTPException(503, "owner_sources_enable_unavailable", headers=_NO_STORE) from None
    from topos.core.state import get_db_connection
    from topos.pipeline.job_runner import start_pipeline_worker

    start_pipeline_worker(get_db_connection)
    return JSONResponse(result, headers=_NO_STORE)


async def _set_paused(source_id: str, paused: bool) -> JSONResponse:
    from topos.ingestion.ai_tool_sync import NOT_PAIRED, NOT_PAIRED_SENTENCE, set_source_state, source_state

    def run() -> Dict[str, Any]:
        conn = _conn()
        set_source_state(conn, source_id, paused=paused)
        return source_state(conn, source_id)

    try:
        state = await asyncio.to_thread(run)
    except Exception:  # noqa: BLE001
        raise HTTPException(503, "owner_sources_unavailable", headers=_NO_STORE) from None
    if state.get("dataset_id") is None:
        return _refusal(409, NOT_PAIRED, NOT_PAIRED_SENTENCE)
    return JSONResponse(
        {"status": "ok", "source_id": source_id, "enabled": state["enabled"], "paused": state["paused"],
         "paused_at": state["paused_at"]},
        headers=_NO_STORE,
    )


@router.post("/sources/{source_id}/pause")
async def pause(source_id: str, principal=Depends(resolve_request_principal)):
    _owner(principal)
    return await _set_paused(_ai_source(source_id), True)


@router.post("/sources/{source_id}/resume")
async def resume(source_id: str, principal=Depends(resolve_request_principal)):
    _owner(principal)
    return await _set_paused(_ai_source(source_id), False)


@router.post("/sources/{source_id}/reclassify-touches")
async def reclassify_touches(source_id: str, payload: Optional[dict] = Body(None),
                             principal=Depends(resolve_request_principal)):
    _owner(principal)
    sid = _ai_source(source_id)
    body = _body(payload, _RECLASSIFY_FIELDS)
    apply = body.get("apply", False)
    if not isinstance(apply, bool):
        raise HTTPException(400, "owner_sources_payload_invalid", headers=_NO_STORE)
    from topos.ingestion.ai_tool_touch_repair import reclassify_touches as run_repair

    try:
        result = await asyncio.to_thread(lambda: run_repair(_conn(), sid, apply=apply))
    except Exception as exc:  # noqa: BLE001 -- the kind only; no path, row or exception text leaves the node
        logger.warning("reclassify-touches %s failed: %s", sid, type(exc).__name__)
        raise HTTPException(503, "owner_sources_reclassify_unavailable", headers=_NO_STORE) from None
    if result.get("catchup_jobs_queued"):
        from topos.core.state import get_db_connection
        from topos.pipeline.job_runner import start_pipeline_worker

        start_pipeline_worker(get_db_connection)
    return JSONResponse(result, headers=_NO_STORE)


@router.post("/sources/{source_id}/sync")
async def sync(source_id: str, payload: Optional[dict] = Body(None), principal=Depends(resolve_request_principal)):
    _owner(principal)
    sid = _ai_source(source_id)
    body = _body(payload, _SYNC_FIELDS)
    session_path = body.get("session_path")
    if session_path is not None and not isinstance(session_path, str):
        raise HTTPException(400, "owner_sources_payload_invalid", headers=_NO_STORE)
    from topos.core.state import get_db_connection
    from topos.ingestion.ai_tool_sync import resolve_owner_scope, sync_refusal
    from topos.ingestion.local_sync_jobs import enqueue_local_sync

    def check() -> Dict[str, Any]:
        conn = _conn()
        scope = resolve_owner_scope(conn)
        refusal = sync_refusal(conn, sid, scope=scope)
        return {"scope": scope, "refusal": refusal}

    try:
        checked = await asyncio.to_thread(check)
    except Exception:  # noqa: BLE001
        raise HTTPException(503, "owner_sources_unavailable", headers=_NO_STORE) from None
    if checked["refusal"] is not None:
        return _refusal(409, str(checked["refusal"]["code"]), str(checked["refusal"]["error"]))
    options: Dict[str, Any] = {"mode": "since_last", "trigger": "hook" if session_path else "manual"}
    if session_path:
        options["session_path"] = session_path.strip()
    outcome = await enqueue_local_sync(
        source_id=sid, dataset_id=checked["scope"]["dataset_id"], sync_options=options,
        conn_factory=get_db_connection,
    )
    if outcome.get("status") != "ok":
        return _refusal(409, str(outcome.get("code") or "sync_not_started"), str(outcome.get("error") or "The sync could not be started."))
    return JSONResponse(
        {"status": "ok", "source_id": sid, "job_id": outcome.get("job_id"),
         "already_running": bool(outcome.get("already_running"))},
        headers=_NO_STORE,
    )
