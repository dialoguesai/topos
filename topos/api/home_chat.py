"""Home chat session REST API (functional node storage, not canonical AIChat)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, status

from ..auth import require_api_key
from ..home_chat import store as home_chat_store

logger = logging.getLogger("topos.api.home_chat")

router = APIRouter(tags=["home-chat"])

MAX_BODY_BYTES = 1_048_576


def _get_conn():
    from ..core.state import get_db_connection

    conn = get_db_connection()
    if not conn:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Database not available")
    return conn


def _map_store_error(err: Exception) -> HTTPException:
    code = str(err)
    if isinstance(err, PermissionError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    mapping = {
        "INVALID_SESSION_ID": (status.HTTP_400_BAD_REQUEST, "Invalid session id."),
        "INVALID_ENGINE_ID": (status.HTTP_400_BAD_REQUEST, "Invalid engine id."),
        "INVALID_HISTORY": (status.HTTP_400_BAD_REQUEST, "Invalid history payload."),
        "HISTORY_TOO_LARGE": (status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "History payload too large."),
        "STALE_REVISION": (status.HTTP_409_CONFLICT, "Stale revision."),
    }
    status_code, message = mapping.get(code, (status.HTTP_400_BAD_REQUEST, "Invalid request."))
    return HTTPException(status_code=status_code, detail={"error": message, "code": code})


@router.get("/v1/home-chat/sessions", dependencies=[Depends(require_api_key)])
async def list_home_chat_sessions(
    engine_id: str = Query(..., min_length=1),
    user_id: str = Query(..., min_length=1),
) -> Dict[str, List[Dict[str, Any]]]:
    conn = _get_conn()
    rows = home_chat_store.list_sessions(conn, user_id=user_id.strip(), engine_id=engine_id.strip())
    return {"sessions": rows}


@router.get("/v1/home-chat/sessions/{session_id}", dependencies=[Depends(require_api_key)])
async def get_home_chat_session(
    session_id: str,
    user_id: str = Query(..., min_length=1),
) -> Dict[str, Any]:
    conn = _get_conn()
    row = home_chat_store.get_session(conn, user_id=user_id.strip(), session_id=session_id.strip())
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
    return row


@router.put("/v1/home-chat/sessions/{session_id}", dependencies=[Depends(require_api_key)])
async def upsert_home_chat_session(
    request: Request,
    session_id: str,
    user_id: str = Query(..., min_length=1),
    body: Dict[str, Any] = Body(default_factory=dict),
) -> Dict[str, Any]:
    if str(body.get("sessionId") or "") != session_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="sessionId mismatch")
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_BYTES:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail="Payload too large")
    conn = _get_conn()
    try:
        return home_chat_store.upsert_session(
            conn,
            user_id=user_id.strip(),
            payload={**body, "sessionId": session_id},
        )
    except (ValueError, PermissionError) as exc:
        raise _map_store_error(exc) from exc


@router.delete("/v1/home-chat/sessions/{session_id}", dependencies=[Depends(require_api_key)], status_code=status.HTTP_204_NO_CONTENT)
async def delete_home_chat_session(
    session_id: str,
    user_id: str = Query(..., min_length=1),
) -> None:
    conn = _get_conn()
    deleted = home_chat_store.delete_session(conn, user_id=user_id.strip(), session_id=session_id.strip())
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Session not found")
