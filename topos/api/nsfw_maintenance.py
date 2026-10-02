"""Owner socket maintenance of the NSFW tags: the explicit-wording sweep, by hand, with counts.

``POST /v1/privacy/nsfw-recheck`` on the node's owner socket only (the 0600 UDS; no bearer, no relay). Body, all
optional: ``{"dry_run": true|false, "tables": [<table>, ...]}``. ``dry_run`` defaults to true: a bare POST
evaluates every row of the three tables with the rule and answers what a write run would change, writing nothing.
``"dry_run": false`` runs the same sweep the node runs for itself at startup (``topos.disclosure.nsfw_tags``),
over every row, now. ``tables`` limits either to some of ``journal_entries``, ``conversation_messages`` and
``ai_chat_messages``. The answer is counts (``nsfw_tags.COUNTS`` per table and in total); no id, text or
matched word.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from topos.auth import resolve_request_principal

router = APIRouter(prefix="/v1/privacy", tags=["privacy-owner-maintenance"])

_NO_STORE = {"Cache-Control": "no-store"}
_FIELDS = frozenset({"dry_run", "tables"})


def _invalid() -> HTTPException:
    return HTTPException(400, "nsfw_recheck_payload_invalid", headers=_NO_STORE)


def _valid_tables(value: Any) -> bool:
    from topos.disclosure.nsfw_tags import TABLES

    return (type(value) is list and bool(value) and all(type(item) is str for item in value)
            and len(set(value)) == len(value) and set(value) <= set(TABLES))


@router.post("/nsfw-recheck")
async def nsfw_recheck(payload: Optional[dict] = Body(None), principal=Depends(resolve_request_principal)):
    from topos.principal import OWNER_APP

    if principal is None or principal.cls != OWNER_APP or principal.channel != "uds":
        raise HTTPException(403, "owner_socket_required", headers=_NO_STORE)
    body = {} if payload is None else payload
    if not isinstance(body, dict) or not set(body) <= _FIELDS:
        raise _invalid()
    dry_run = body.get("dry_run", True)
    tables = body.get("tables")
    if type(dry_run) is not bool or (tables is not None and not _valid_tables(tables)):
        raise _invalid()

    def apply():
        from topos.core.state import get_db_connection
        from topos.disclosure.nsfw_tags import run_sweep
        from topos.runtime_shutdown import is_shutdown_requested

        return run_sweep(get_db_connection, dry_run=dry_run, tables=tables, stop=is_shutdown_requested,
                         full=not dry_run)

    try:
        result = await asyncio.to_thread(apply)
    except Exception:  # noqa: BLE001 -- no path, row or exception text leaves the node
        raise HTTPException(503, "nsfw_recheck_unavailable", headers=_NO_STORE) from None
    return JSONResponse(result, headers=_NO_STORE)
