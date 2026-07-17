"""Control-plane ws handlers for the tool-RAG substrate (PLAN_AGENT_QUALITY M2/M3).

The react-app reaches the node through the CP's HTTP→ws bridge, so the tool
index/retrieve endpoints need message-type handlers in addition to the direct
FastAPI routes in ``topos/api/tool_index.py``. Both wrap the same core module.

Embedding is blocking (HF inference), so the work runs in a thread — the shared
sqlite connection is opened with ``check_same_thread=False``.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from .common import logger
from .registry import handles


def _conn():
    from ..state import get_db_connection

    conn = get_db_connection()
    if conn is None:
        raise RuntimeError("database unavailable")
    return conn


@handles("tools_index")
async def handle_tools_index(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    tools = payload.get("tools") or []
    prune_missing = bool(payload.get("prune_missing"))
    specs = [
        {
            "name": str((t or {}).get("name") or ""),
            "description": str((t or {}).get("description") or ""),
            "connector_id": (t or {}).get("connector_id"),
        }
        for t in tools
        if isinstance(t, dict) and str((t or {}).get("name") or "").strip()
    ]
    if not specs and not prune_missing:
        return {"id": req_id, "status": "error", "error": "tools must be a non-empty list"}
    try:
        from ...features.signal.tool_index import index_tools

        result = await asyncio.to_thread(
            index_tools, _conn(), specs, prune_missing=prune_missing
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[TOOL_INDEX] tools_index error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("tools_retrieve")
async def handle_tools_retrieve(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    query = str(payload.get("query") or "").strip()
    if not query:
        return {"id": req_id, "status": "error", "error": "query is required"}
    # Per-connector identity-tool override (PLAN_HELP_NUDGE A2).
    raw_identity = payload.get("identity_tools")
    identity_tools = (
        [str(n) for n in raw_identity if str(n or "").strip()]
        if isinstance(raw_identity, list)
        else []
    )
    try:
        from ...features.signal.tool_index import retrieve_tools

        result = await asyncio.to_thread(
            retrieve_tools,
            _conn(),
            query,
            connector_scope=payload.get("connector_scope"),
            k=int(payload.get("k") or 8),
            extra_identity_names=identity_tools,
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[TOOL_INDEX] tools_retrieve error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("tools_index_status")
async def handle_tools_index_status(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...features.signal.tool_index import index_status

        result = await asyncio.to_thread(index_status, _conn())
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[TOOL_INDEX] tools_index_status error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}
