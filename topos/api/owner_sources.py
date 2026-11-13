"""Owner socket: the sources a person turns on by id — the AI coding tools (CONTRACTS.md §2.8),
iMessage and Signal Desktop, and the browser extensions' sources.

On the node's owner socket only (the 0600 UDS; no bearer, no relay), like ``source_retention``.
Every answer is counts, flags, ids and stamps; no transcript text leaves the node.

``GET /api/local/owner/scope`` → ``{user_id, topos_id, dataset_id}`` from the bound identity (else
the stored ``topos_id``), or 409 ``not_paired``.
``GET /api/local/owner/rescope-report`` → ``{identity_mode, rescope_report}``: the counts-only report of
the move from the local identity to an account, or ``null`` when the node never moved.
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

**By id, beyond the coding tools** (``sources/setup_catalog.py``). ``GET /api/local/owner/sources`` lists
every source that can be turned on from a terminal with its state. The five verbs above also answer for
``imessage`` and ``signal`` (``ingestion/messenger_setup.py``): ``pause`` stops reading and is open;
``preview {backfill}`` (counts only, nothing written), ``enable {backfill}``, ``resume`` and ``sync`` open
other people's words, so the socket refuses them 403 ``owner_confirmation_required`` without the Topos app or
a confirmation the person gave on a terminal (``owner_door_routes.MESSENGER_RULES``). A refusal that needs the
person (Full Disk Access, no Signal reader, a Keychain request not allowed) is 409 with its code and the
plain sentence. The extensions' sources (``browser_visits``, ``browser_events``, ``chatgpt_ui_conversation``)
have a status and nothing to enable here: an extension is connected by ``topos-node pushers add`` on a
terminal, and the verbs answer 409 ``extension_source`` saying so.
"""

from __future__ import annotations

import asyncio
import json
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
_MESSENGER_FIELDS = frozenset({"backfill"})
EXTENSION_SOURCE = "extension_source"
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


def _kind(source_id: str) -> str:
    """``coding_history``, ``messages`` or ``extension`` for a source the setup catalog names; 404 otherwise."""
    from topos.sources.setup_catalog import kind_of

    kind = kind_of(str(source_id or "").strip())
    if kind is None:
        raise HTTPException(404, "unknown_source", headers=_NO_STORE)
    return kind


#: The messenger verbs that open a message store: the person's, on a terminal, or the app's.
_MESSENGER_CONFIRMED_VERBS = frozenset({"preview", "enable", "resume", "sync"})


def _confirmed_owner_socket() -> bool:
    """The request came over the owner socket from a peer that may change things: the app, the dev proxy on
    a node that allows it, or a confirmation a person just gave on a terminal. The socket layer's gate already
    asked this for the route (``owner_door_routes.MESSENGER_RULES``); asking again here keeps a messenger
    source closed with the split flag off, where that gate does not run (the fact-pack backfill does the same,
    ``api/signal._confirmed_owner_socket``)."""
    from topos.uds import current_socket_peer, current_transport, peer_may_change

    peer = current_socket_peer()
    return current_transport() == "uds" and peer is not None and peer_may_change(peer)


def _extension_refusal(source_id: str) -> JSONResponse:
    from topos.sources.setup_catalog import BY_ID

    entry = BY_ID[source_id]
    return _refusal(
        409, EXTENSION_SOURCE,
        f"{entry.name} is written by a browser extension. Add the extension, then run "
        f"`topos-node pushers add {entry.app} --sources {','.join(entry.app_sources)}` in your own terminal.",
    )


def _messenger_backfill(body: Dict[str, Any]) -> str:
    from topos.ingestion.messenger_setup import BACKFILL_CHOICES

    backfill = body.get("backfill", "30d")
    if not isinstance(backfill, str) or backfill not in BACKFILL_CHOICES:
        raise HTTPException(400, "owner_sources_payload_invalid", headers=_NO_STORE)
    return backfill


async def _messenger(run, *, what: str) -> JSONResponse:
    """Run one messenger_setup call off the loop and turn its refusal into the door's 409."""
    from topos.ingestion.messenger_setup import MessengerRefused

    try:
        result = await asyncio.to_thread(run)
    except MessengerRefused as exc:
        return _refusal(exc.status, exc.code, exc.message)
    except ValueError as exc:
        raise HTTPException(400, str(exc), headers=_NO_STORE) from None
    except Exception as exc:  # noqa: BLE001 -- the kind only; no path, row or exception text leaves the node
        logger.warning("%s failed: %s", what, type(exc).__name__)
        raise HTTPException(503, "owner_sources_unavailable", headers=_NO_STORE) from None
    return JSONResponse(result, headers=_NO_STORE)


def _start_worker() -> None:
    from topos.core.state import get_db_connection
    from topos.pipeline.job_runner import start_pipeline_worker

    start_pipeline_worker(get_db_connection)


def _describe_extension(conn, source_id: str) -> Dict[str, Any]:
    """Whether the extension that writes this source is connected to this node. Flags and stamps only."""
    from topos import local_pushers
    from topos.sources.setup_catalog import BY_ID

    entry = BY_ID[source_id]
    row = next(
        (r for r in local_pushers.list_pushers(conn)
         if r.get("app") == entry.app and not r.get("revoked_at") and source_id in (r.get("sources") or [])),
        None,
    )
    return {
        "source_id": source_id, "display_name": entry.name, "kind": entry.kind, "app": entry.app,
        "app_sources": list(entry.app_sources), "install_url": entry.install_url,
        "connected": row is not None, "enabled": row is not None,
        "connected_at": row.get("created_at") if row else None,
        "last_write_at": row.get("last_used_at") if row else None,
    }


def _describe_any(conn, source_id: str, kind: str) -> Dict[str, Any]:
    from topos.sources.setup_catalog import KIND_CODING_HISTORY, KIND_MESSAGES

    if kind == KIND_CODING_HISTORY:
        from topos.ingestion.ai_tool_sync import describe_ai_tool_source

        return {"kind": kind, **describe_ai_tool_source(conn, source_id)}
    if kind == KIND_MESSAGES:
        from topos.ingestion.messenger_setup import describe_messenger_source

        return describe_messenger_source(conn, source_id)
    return _describe_extension(conn, source_id)


def _list_entry(conn, entry) -> Dict[str, Any]:
    """One row of ``GET /sources``: the catalog's words and the few state fields a setup step reads."""
    from topos.sources.setup_catalog import KIND_CODING_HISTORY, KIND_MESSAGES

    row: Dict[str, Any] = entry.public()
    try:
        if entry.kind == KIND_CODING_HISTORY:
            from topos.ingestion.ai_tool_sync import source_state

            state = source_state(conn, entry.source_id)
            row.update({k: state.get(k) for k in ("enabled", "paused", "last_sync_at", "last_error")})
        elif entry.kind == KIND_MESSAGES:
            from topos.ingestion.messenger_setup import messenger_state, readiness

            state = messenger_state(conn, entry.source_id)
            ready = readiness(entry.source_id)
            row.update({k: state.get(k) for k in ("enabled", "paused", "last_sync_at", "last_error")})
            row.update({"ready": ready["ready"], "needs": ready["code"]})
        else:
            state = _describe_extension(conn, entry.source_id)
            row.update({k: state.get(k) for k in ("enabled", "connected", "connected_at", "last_write_at")})
    except Exception as exc:  # noqa: BLE001 -- one unreadable source never empties the list
        logger.warning("sources list: %s unreadable (%s)", entry.source_id, type(exc).__name__)
        row.update({"enabled": None, "state_unavailable": True})
    return row


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


@router.get("/rescope-report")
async def owner_rescope_report(principal=Depends(resolve_request_principal)):
    """The report of the one move from the local identity to an account (``local_identity.rescope_to_account``):
    row updates per table, the renamed raw folders, whether the Topos id changed, and when. Counts and flags
    only; no id and no row leaves the node. A node that never moved answers ``rescope_report: null`` with its
    mode, so a caller can tell "not moved yet" from "no such door" (a 404). The install QA's connect-later
    check reads this instead of opening the database file."""
    _owner(principal, reads=True)
    from topos.core.state import get_engine_config_value
    from topos.local_identity import RESCOPE_REPORT_KEY, identity_mode

    def _read():
        conn = _conn()
        return identity_mode(conn), get_engine_config_value(conn, RESCOPE_REPORT_KEY)

    try:
        mode, stored = await asyncio.to_thread(_read)
    except Exception:  # noqa: BLE001 -- no path or exception text leaves the node
        raise HTTPException(503, "owner_scope_unavailable", headers=_NO_STORE) from None
    report = None
    if stored:
        try:
            parsed = json.loads(stored) if isinstance(stored, str) else stored
            report = parsed if isinstance(parsed, dict) else None
        except ValueError:
            report = None
    return JSONResponse({"identity_mode": mode, "rescope_report": report}, headers=_NO_STORE)


@router.get("/sources")
async def sources_list(principal=Depends(resolve_request_principal)):
    """Every source that can be turned on from a terminal, by id, with its state. Owner only: the list says
    which of the owner's apps and message stores exist on this computer."""
    _owner(principal)
    from topos.sources.setup_catalog import CATALOG

    def run() -> Dict[str, Any]:
        conn = _conn()
        return {"sources": [_list_entry(conn, entry) for entry in CATALOG]}

    try:
        listing = await asyncio.to_thread(run)
    except Exception:  # noqa: BLE001
        raise HTTPException(503, "owner_sources_unavailable", headers=_NO_STORE) from None
    return JSONResponse(listing, headers=_NO_STORE)


@router.get("/sources/{source_id}")
async def source_status(source_id: str, principal=Depends(resolve_request_principal)):
    kind = _kind(source_id)
    from topos.sources.setup_catalog import KIND_CODING_HISTORY

    # The enrolled coding agent reads the status of the transcript sources (CONTRACTS §1.2); whether the
    # owner's Messages or an extension is connected is the owner's to read.
    _owner(principal, reads=kind == KIND_CODING_HISTORY)
    sid = str(source_id).strip()
    try:
        status = await asyncio.to_thread(lambda: _describe_any(_conn(), sid, kind))
    except Exception:  # noqa: BLE001
        raise HTTPException(503, "owner_sources_unavailable", headers=_NO_STORE) from None
    return JSONResponse(status, headers=_NO_STORE)


async def _other_kind(source_id: str, action: str, payload: Optional[dict]) -> Optional[JSONResponse]:
    """The verb for a messenger or an extension source, or None for a coding tool (and for an id the catalog
    does not name, which the coding-tool path answers 404).

    ``preview``, ``enable``, ``resume`` and ``sync`` of a messenger source reach here only past the socket's
    dangerous-door gate (``owner_door_routes.MESSENGER_RULES``): the Topos app, or a confirmation the person gave on a terminal.
    """
    from topos.sources.setup_catalog import KIND_EXTENSION, KIND_MESSAGES, kind_of

    sid = str(source_id or "").strip()
    kind = kind_of(sid)
    if kind == KIND_EXTENSION:
        return _extension_refusal(sid)
    if kind != KIND_MESSAGES:
        return None
    if action in _MESSENGER_CONFIRMED_VERBS and not _confirmed_owner_socket():
        from topos.uds import OWNER_CONFIRMATION_MESSAGE

        return _refusal(403, "owner_confirmation_required", OWNER_CONFIRMATION_MESSAGE)
    from topos.ingestion import messenger_setup

    if action == "preview":
        backfill = _messenger_backfill(_body(payload, _MESSENGER_FIELDS))
        return await _messenger(lambda: messenger_setup.preview_messenger_source(_conn(), sid, backfill=backfill),
                                what=f"preview {sid}")
    if action == "enable":
        backfill = _messenger_backfill(_body(payload, _MESSENGER_FIELDS))
        response = await _messenger(lambda: messenger_setup.enable_messenger_source(_conn(), sid, backfill=backfill),
                                    what=f"enable {sid}")
        if response.status_code == 200:
            _start_worker()
        return response
    if action == "pause":
        return await _messenger(lambda: messenger_setup.set_messenger_enabled(_conn(), sid, False), what=f"pause {sid}")
    if action == "resume":
        return await _messenger(lambda: messenger_setup.set_messenger_enabled(_conn(), sid, True), what=f"resume {sid}")
    _body(payload, frozenset())
    response = await _messenger(lambda: messenger_setup.sync_messenger_source(_conn(), sid), what=f"sync {sid}")
    if response.status_code == 200:
        _start_worker()
    return response


@router.post("/sources/{source_id}/preview")
async def preview(source_id: str, payload: Optional[dict] = Body(None), principal=Depends(resolve_request_principal)):
    _owner(principal)
    routed = await _other_kind(source_id, "preview", payload)
    if routed is not None:
        return routed
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
    routed = await _other_kind(source_id, "enable", payload)
    if routed is not None:
        return routed
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
    routed = await _other_kind(source_id, "pause", None)
    if routed is not None:
        return routed
    return await _set_paused(_ai_source(source_id), True)


@router.post("/sources/{source_id}/resume")
async def resume(source_id: str, principal=Depends(resolve_request_principal)):
    _owner(principal)
    routed = await _other_kind(source_id, "resume", None)
    if routed is not None:
        return routed
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
    routed = await _other_kind(source_id, "sync", payload)
    if routed is not None:
        return routed
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


@router.post("/sources/prepare")
async def prepare_private_source(payload: dict = Body(...), principal=Depends(resolve_request_principal)):
    """Validate supplied data without installing, reading files, or contacting a registry."""
    _owner(principal, reads=True)
    try:
        result, _scope = await _private_source_plan(payload)
    except (ValueError, TypeError, KeyError) as exc:
        # Validation is a successful read with diagnostics about the caller's supplied definition.
        # Keep these separate from transport/auth refusals, whose bodies the CLI never prints.
        return JSONResponse({"status": "invalid", "errors": [str(exc)]}, headers=_NO_STORE)
    return JSONResponse(result, headers=_NO_STORE)


@router.get("/private-sources/{source_id}")
async def private_source_status(source_id: str, principal=Depends(resolve_request_principal)):
    """Read this node's persisted definition; callers cannot select another owner's scope."""
    _owner(principal, reads=True)
    from topos.sources.local_create import ID_RE
    from topos.sources.install_service import list_installs, _list_user_tables, INSTALL_TABLE
    from topos.ingestion.ai_tool_sync import resolve_owner_scope

    if not ID_RE.fullmatch(source_id):
        raise HTTPException(400, "Invalid source id", headers=_NO_STORE)
    def read():
        conn = _conn()
        scope = resolve_owner_scope(conn)
        if not scope:
            raise HTTPException(409, "Topos has no local identity yet", headers=_NO_STORE)
        if INSTALL_TABLE not in _list_user_tables(conn):
            return []
        return [row.to_dict() for row in list_installs(scope=scope, source_id=source_id, initialize_schema=False) if row.is_active]
    active = await asyncio.to_thread(read)
    return JSONResponse({"source_id": source_id, "installed": bool(active), "installs": active}, headers=_NO_STORE)


async def _private_source_plan(payload):
    from topos.sources.local_create import prepare, digest
    from topos.ingestion.ai_tool_sync import resolve_owner_scope

    plan = await prepare(payload)
    def scope_and_tables():
        from topos.sources.install_service import _list_user_tables
        from topos.sources.registry import REGISTRY
        conn = _conn()
        current = REGISTRY.get(plan["source_id"])
        owned = {t.get("table_id") for t in (current.tables or [])} if current else set()
        requested = {t["table_id"] for t in plan["source_definition_json"].get("tables") or []}
        if (requested - owned) & set(_list_user_tables(conn)):
            raise ValueError("A requested data table already exists; choose a new source/table name")
        return resolve_owner_scope(conn)
    scope = await asyncio.to_thread(scope_and_tables)
    if not scope:
        raise ValueError("Topos has no local identity yet; start it before adding a source")
    plan["target"] = {key: scope[key] for key in ("user_id", "topos_id", "dataset_id")}
    plan["plan_sha256"] = digest({"content": plan["plan_sha256"], "target": plan["target"]})
    return plan, scope


# Serialise this create-only workflow through install and optional import. The install service also
# holds its own lock, shared with the existing install doors, while checking for an existing definition.
_private_source_lock = asyncio.Lock()


@router.post("/sources/install-private")
async def install_private_source(payload: dict = Body(...), principal=Depends(resolve_request_principal)):
    """One owner-approved local definition, optionally followed by a bounded sample import.

    No registry publication, cloud key, caller-selected identity, or automatic producer enrollment.
    A failed import leaves the installed definition in place and reports both stages explicitly.
    """
    _owner(principal)
    if not _confirmed_owner_socket():
        raise HTTPException(403, "owner_confirmation_required", headers=_NO_STORE)
    from dataclasses import asdict
    from topos.sources import install_service
    from topos.sources.registry import REGISTRY
    from topos.sources.local_create import digest, sample_format

    async with _private_source_lock:
        try:
            plan, scope = await _private_source_plan(payload)
            if payload.get("plan_sha256") != plan["plan_sha256"]:
                raise ValueError("Plan changed; prepare it again before approval")
            definition = plan["source_definition_json"]
            sample = payload.get("sample")
            if sample is not None and definition["source_type"] != "file":
                raise ValueError("Install stream definitions without --sample; enroll the producer separately")
            def install():
                with install_service._LOCK:
                    # Never turn creation into an implicit replacement of an existing source.
                    current = REGISTRY.get(definition["source_id"])
                    if current is not None and digest(asdict(current)) != plan["definition_sha256"]:
                        raise ValueError("source_id already exists with a different definition; choose a new id")
                    return install_service.install_source(source_definition_json=definition, scope=scope)
            installed = await asyncio.to_thread(install)
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(400, str(exc), headers=_NO_STORE) from None
        receipt = {"status": "installed", "registered": False, "installed": True,
                   "source_id": installed.source_id, "install_id": installed.install_id,
                   "definition_sha256": plan["definition_sha256"], "plan_sha256": plan["plan_sha256"],
                   "sample_import": None, "warnings": plan["warnings"]}
        if sample is not None:
            from topos.ingestion.ingest_helpers import ingest_file_payload
            from topos.features.provenance.writer_class import WRITER_OWNER_IMPORT
            try:
                result = await ingest_file_payload(dataset_id=scope["dataset_id"],
                    schema_id=definition["schema_id"], source_id=installed.source_id,
                    file_bytes=sample["content"].encode("utf-8"),
                    file_format=sample_format(sample["file_name"]), writer_class=WRITER_OWNER_IMPORT)
                count = int(result.get("records_processed") or 0)
                errors = int(result.get("errors_count") or 0)
                ok = result.get("status") == "ok" and count > 0 and errors == 0
                receipt["sample_import"] = {"status": "ingested" if ok else "failed",
                    "records_processed": count, "errors_count": errors, "job_id": result.get("job_id")}
                receipt["status"] = "ingested" if ok else "import_failed"
            except Exception as exc:  # Definition was committed; never hide that partial success or echo sample data.
                logger.warning("Private source sample import failed: %s", type(exc).__name__)
                receipt["status"] = "import_failed"
                receipt["sample_import"] = {"status": "failed", "error": "sample_import_failed"}
        return JSONResponse(receipt, headers=_NO_STORE)
