"""Home chat session message handlers."""
from __future__ import annotations

import logging

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    Optional,
    run_db_read,
)
from .registry import handles

logger = logging.getLogger("topos.core.handlers.home_chat")


def _refusal(req_id: Any, exc: ValueError, *, op: str, session_id: str) -> Dict[str, Any]:
    """The store's refusal as a typed error, never a raise: an exception here reaches
    the relay's catch-all, which logs "Handler raised exception" with a traceback and
    answers with no ``error_code``."""
    code = str(exc)
    shape = getattr(exc, "shape", None)
    if shape is not None:
        # The shape only (JSON types, integer version, size): the history is the
        # owner's conversation and never goes into a log line.
        logger.warning("home chat %s refused: session=%s code=%s shape=%s", op, session_id, code, shape)
    return {"id": req_id, "status": "error", "error": code, "error_code": code}


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
    try:
        row = await run_db_read(hc_store.get_session, user_id=user_id, session_id=session_id)
    except ValueError as exc:
        return _refusal(req_id, exc, op="get", session_id=session_id)
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
        return _refusal(req_id, exc, op="upsert", session_id=session_id)

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


@handles("home_chat_memory")
async def handle_home_chat_memory(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from ...home_chat.memory import memory_action
    pl = message.get("payload") or {}
    conn = hub.get_db_connection()
    if not conn:
        return {"id": message.get("id"), "status": "error", "error": "Database not available"}
    uid, eid = str(pl.get("user_id") or "").strip(), str(pl.get("engine_id") or "").strip()
    if not uid or not eid:
        return {"id": message.get("id"), "status": "error", "error": "user_id and engine_id required"}
    try:
        result = memory_action(conn, user_id=uid, engine_id=eid, action=pl.get("action"), payload=pl.get("body") or {})
        return {"id": message.get("id"), "status": "ok", "payload": result}
    except ValueError as exc:
        return {"id": message.get("id"), "status": "error", "error": str(exc), "error_code": str(exc)}


# --- HT lane: one content-free timing row per owner home-chat turn (topos/home_chat/turn_timings.py) -------------
# Both types run off the event loop: the write takes the write gate, and the gate is exactly what an index rebuild
# holds for a minute at a time. Both require the caller's id, so the relay's owner rule (rule (b) in __init__.py)
# always has a name to compare on a bound node, the same protection as the session types above.


def _timings_caller_refusal(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    pl = message.get("payload") or {}
    if isinstance(pl, dict) and str(pl.get("user_id") or "").strip():
        return None
    return {"id": message.get("id"), "status": "error", "error": "user_id required", "error_code": "USER_ID_REQUIRED"}


@handles("record_home_chat_turn_timings", off_loop=True)
async def handle_record_home_chat_turn_timings(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from ...__version__ import __version__
    from ...home_chat import turn_timings

    refusal = _timings_caller_refusal(message)
    if refusal:
        return refusal
    conn = hub.get_db_connection()
    if not conn:
        return {"id": message.get("id"), "status": "error", "error": "Database not available"}
    try:
        result = turn_timings.record_batch(conn, message["payload"].get("records"), node_version=__version__)
    except ValueError as exc:
        # The batch's shape only; a record's values are never logged or echoed.
        return {"id": message.get("id"), "status": "error", "error": str(exc), "error_code": str(exc)}
    if result["refused"]:
        logger.warning("home chat timings refused %d record(s): fields=%s", result["refused"],
                       ",".join(result["refused_fields"]))
    return {"id": message.get("id"), "status": "ok", "payload": result}


@handles("get_home_chat_turn_timings_summary", off_loop=True)
async def handle_get_home_chat_turn_timings_summary(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from ...home_chat import turn_timings

    refusal = _timings_caller_refusal(message)
    if refusal:
        return refusal
    conn = hub.get_db_connection()
    if not conn:
        return {"id": message.get("id"), "status": "error", "error": "Database not available"}
    days = message["payload"].get("days")
    days = days if isinstance(days, int) and not isinstance(days, bool) else 30
    return {"id": message.get("id"), "status": "ok", "payload": turn_timings.summarize(conn, days=days)}
