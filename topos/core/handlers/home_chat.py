"""Home chat session message handlers."""
from __future__ import annotations

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    Optional,
    run_db_read,
)
from .registry import handles


@handles("list_home_chat_sessions")
async def handle_list_home_chat_sessions(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...home_chat import store as hc_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    user_id = str(pl.get("user_id") or "").strip()
    engine_id = str(pl.get("engine_id") or "").strip()
    if not user_id or not engine_id:
        return {"id": req_id, "status": "error", "error": "user_id and engine_id required"}
    rows = await run_db_read(hc_store.list_sessions, user_id=user_id, engine_id=engine_id)
    return {"id": req_id, "status": "ok", "payload": {"sessions": rows}}

@handles("get_home_chat_session")
async def handle_get_home_chat_session(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...home_chat import store as hc_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    user_id = str(pl.get("user_id") or "").strip()
    session_id = str(pl.get("session_id") or "").strip()
    if not user_id or not session_id:
        return {"id": req_id, "status": "error", "error": "user_id and session_id required"}
    row = await run_db_read(hc_store.get_session, user_id=user_id, session_id=session_id)
    if not row:
        return {"id": req_id, "status": "error", "error": "Session not found"}
    return {"id": req_id, "status": "ok", "payload": row}

@handles("upsert_home_chat_session")
async def handle_upsert_home_chat_session(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...home_chat import store as hc_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    user_id = str(pl.get("user_id") or "").strip()
    body = pl.get("body") if isinstance(pl.get("body"), dict) else {}
    session_id = str(pl.get("session_id") or body.get("sessionId") or "").strip()
    if not user_id or not session_id:
        return {"id": req_id, "status": "error", "error": "user_id and session_id required"}
    try:
        row = hc_store.upsert_session(
            conn,
            user_id=user_id,
            payload={**body, "sessionId": session_id},
        )
        return {"id": req_id, "status": "ok", "payload": row}
    except PermissionError:
        return {"id": req_id, "status": "error", "error": "FORBIDDEN", "error_code": "FORBIDDEN"}
    except ValueError as exc:
        code = str(exc)
        return {"id": req_id, "status": "error", "error": code, "error_code": code}

@handles("delete_home_chat_session")
async def handle_delete_home_chat_session(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...home_chat import store as hc_store

    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    pl = message.get("payload") or {}
    user_id = str(pl.get("user_id") or "").strip()
    session_id = str(pl.get("session_id") or "").strip()
    if not user_id or not session_id:
        return {"id": req_id, "status": "error", "error": "user_id and session_id required"}
    deleted = hc_store.delete_session(conn, user_id=user_id, session_id=session_id)
    if not deleted:
        return {"id": req_id, "status": "error", "error": "Session not found"}
    return {"id": req_id, "status": "ok", "payload": {"deleted": True}}
