from __future__ import annotations

import uuid
from typing import Any, Dict, Optional

from ..core.handlers import handle_control_plane_request

MAX_PREVIEW_LIMIT = 25
_SCOPE_KEYS = ("dataset_id", "owner_user_id", "tenant_id")


def _scope_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {key: payload[key] for key in _SCOPE_KEYS if payload.get(key) is not None}


async def _control_plane_request(msg_type: str, request_payload: Dict[str, Any]) -> Dict[str, Any]:
    return await handle_control_plane_request(
        {
            "id": str(uuid.uuid4()),
            "type": msg_type,
            "payload": request_payload,
        }
    )


def _require_ok(result: Optional[Dict[str, Any]], fallback_error: str) -> Dict[str, Any]:
    if result and result.get("status") == "ok":
        return result
    raise RuntimeError(str((result or {}).get("error") or fallback_error))


async def _get_database_explorer_summary_core(payload: Dict[str, Any]) -> Dict[str, Any]:
    scope_payload = _scope_payload(payload)
    tables_result = _require_ok(
        await _control_plane_request("list_database_tables", scope_payload),
        "Failed to list database tables",
    )
    files_result = _require_ok(
        await _control_plane_request("list_jsonl_files", {}),
        "Failed to list jsonl files",
    )

    summary: Dict[str, Any] = {
        "status": "ok",
        "tables": (tables_result.get("payload") or {}),
        "jsonl_files": (files_result.get("payload") or {}),
    }

    preview_table = str(payload.get("preview_table") or "").strip()
    if preview_table:
        raw_limit = payload.get("preview_limit")
        try:
            preview_limit = int(raw_limit) if raw_limit is not None else MAX_PREVIEW_LIMIT
        except (TypeError, ValueError):
            preview_limit = MAX_PREVIEW_LIMIT
        preview_limit = max(1, min(preview_limit, MAX_PREVIEW_LIMIT))
        preview_payload: Dict[str, Any] = {
            "table_name": preview_table,
            "limit": preview_limit,
            "offset": 0,
            **scope_payload,
        }
        preview_result = await _control_plane_request("get_table_rows", preview_payload)
        if preview_result.get("status") == "ok":
            summary["preview"] = preview_result.get("payload") or {}

    return summary
