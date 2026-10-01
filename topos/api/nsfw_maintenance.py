"""Owner socket maintenance of the ingest-time NSFW tags: re-apply the classifier cutoff to rows already flagged.

``POST /v1/privacy/nsfw-recheck`` on the node's owner socket only (the 0600 UDS; no bearer, no relay). Body, all
optional: ``{"dry_run": true|false, "threshold": <number in [0, 1)>, "tables": [<table>, ...]}``. ``dry_run``
defaults to true, so a bare POST only counts. ``threshold`` is a what-if for a dry run alone: a write always
applies the configured ``nsfw_classifier_threshold``, the cutoff ingest applies, so stored tags and new ones follow
one rule. ``tables`` limits the pass to some of ``journal_entries``, ``conversation_messages`` and
``ai_chat_messages`` (all three by default). The answer is counts (``topos.disclosure.nsfw_recheck``); no id, score
or text.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import JSONResponse

from topos.auth import resolve_request_principal

router = APIRouter(prefix="/v1/privacy", tags=["privacy-owner-maintenance"])

_NO_STORE = {"Cache-Control": "no-store"}
_FIELDS = frozenset({"dry_run", "threshold", "tables"})


def _invalid() -> HTTPException:
    return HTTPException(400, "nsfw_recheck_payload_invalid", headers=_NO_STORE)


def _valid_threshold(value: Any) -> bool:
    return (not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
            and 0 <= value < 1)


def _valid_tables(value: Any) -> bool:
    from topos.disclosure.nsfw_recheck import TABLES

    return (type(value) is list and bool(value) and all(type(item) is str for item in value)
            and len(set(value)) == len(value) and set(value) <= set(TABLES))


@router.post("/nsfw-recheck")
async def nsfw_recheck(payload: Optional[dict] = Body(None), principal=Depends(resolve_request_principal)):
    from topos.principal import OWNER_APP

    if principal is None or principal.cls != OWNER_APP or principal.channel != "uds":
        raise HTTPException(403, "owner_socket_required", headers=_NO_STORE)
    body = {} if payload is None else payload
    if not set(body) <= _FIELDS:
        raise _invalid()
    dry_run = body.get("dry_run", True)
    threshold = body.get("threshold")
    if type(dry_run) is not bool:
        raise _invalid()
    if threshold is not None and (not dry_run or not _valid_threshold(threshold)):
        raise _invalid()
    tables = body.get("tables")
    if tables is not None and not _valid_tables(tables):
        raise _invalid()

    def apply():
        from topos.core.state import get_db_connection
        from topos.disclosure.nsfw_recheck import recheck_nsfw_tags

        conn = get_db_connection()
        if conn is None:
            raise RuntimeError("database unavailable")
        return recheck_nsfw_tags(conn, threshold=threshold, dry_run=dry_run, tables=tables)

    try:
        result = await asyncio.to_thread(apply)
    except Exception:  # noqa: BLE001 -- no path, row or exception text leaves the node
        raise HTTPException(503, "nsfw_recheck_unavailable", headers=_NO_STORE) from None
    return JSONResponse(result, headers=_NO_STORE)
