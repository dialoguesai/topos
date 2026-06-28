from __future__ import annotations

import logging
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_api_key
from ..sources.scrub_service import (
    REMOVE_SOURCE_OPTIONS,
    SCRUB_SOURCE_OPTIONS,
    ScrubInProgressError,
    normalize_scrub_payload,
)
from .source_install import _scope_from_payload

router = APIRouter()
logger = logging.getLogger("topos.api.source_scrub")


def _ok_envelope(request_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    return {"status": "ok", "request_id": request_id, **payload}


async def _scrub_source_core(payload: Dict[str, Any]) -> Dict[str, Any]:
    from ..sources.scrub_service import scrub_source_async

    source_id, options = normalize_scrub_payload(payload)
    return await scrub_source_async(
        source_id=source_id,
        scope=_scope_from_payload(payload),
        options=options,
    )


@router.post("/source-scrub", dependencies=[Depends(require_api_key)])
async def post_source_scrub(payload: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    request_id = str(uuid.uuid4())
    dry_run = bool(payload.get("dry_run")) or bool(
        (payload.get("options") or {}).get("dry_run") if isinstance(payload.get("options"), dict) else False
    )
    logger.info(
        "[SOURCE_SCRUB] request_id=%s source_id=%s dry_run=%s",
        request_id,
        str(payload.get("source_id") or ""),
        dry_run,
    )
    try:
        result = await _scrub_source_core(payload)
        return _ok_envelope(request_id, result)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ScrubInProgressError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        message = str(exc)
        if "pooled" in message.lower():
            raise HTTPException(status_code=503, detail=message) from exc
        raise HTTPException(status_code=503, detail=message) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


__all__ = [
    "REMOVE_SOURCE_OPTIONS",
    "SCRUB_SOURCE_OPTIONS",
    "_scrub_source_core",
    "post_source_scrub",
    "router",
]
