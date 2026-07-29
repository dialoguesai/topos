# Topos UMA scoped data endpoints (US-3.8) and filter enforcement (US-3.6)
# Sprint 05: scope resolution — return data only for tables allowed by RPT scopes.
# See: control_plane/uma/uma_sprints/SPRINT_3_USER_SHARING.md, sprints_roles_scopes_stage_1/SPRINT_05_TOPOS_SCOPE_RESOLUTION.md

from __future__ import annotations

import hashlib

from typing import Any, Dict, List, Optional, Set, Tuple

from fastapi import APIRouter, HTTPException, Query, Request, status

from ..core.state import get_db_connection
from ..uma_rpt import RPTValidationError, get_control_plane_http_base, introspect_for_resource
from ..uma_filters import UMAFilterError, apply_filter_manifest, apply_filters, extract_field_transforms, extract_filter_manifest, get_limit_cap
from ..scope_resolution import resolve_scopes_to_tables, may_access_table
from ..uma_contact_enrichment import apply_message_contact_pipeline, strip_contact_runtime_filters
from ..uma_resource_id import parse_dataset_id_from_uma_dataset_resource_id
from ..engine.usage_observation import emit_usage_observation

router = APIRouter(prefix="/v1/uma/resources", tags=["uma-data"])


def _table_exists(conn, table_name: str) -> bool:
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        )
        return cursor.fetchone() is not None
    except Exception:
        return False


def _sqlite_table_columns(conn, table: str) -> Set[str]:
    try:
        cur = conn.execute('PRAGMA table_info("{}")'.format(table.replace('"', "")))
        return {str(row[1]) for row in cur.fetchall()}
    except Exception:
        return set()


def _message_time_order_column(conn, table: str) -> str:
    """Prefer event_at when present; else ts (Stage 6 seed uses ts-only)."""
    cols = _sqlite_table_columns(conn, table)
    if "event_at" in cols:
        return "event_at"
    if "ts" in cols:
        return "ts"
    return "message_id"


def _get_messages_from_db(
    conn,
    dataset_id: Optional[str],
    limit: int,
    offset: int,
    allowed_tables: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Query only the message-like tables permitted by the granted scopes."""

    def _fetch_rows(query: str, params: Tuple[int, int]) -> List[Dict[str, Any]]:
        cursor = conn.execute(query, params)
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def _allowed_message_sources() -> List[str]:
        if not allowed_tables:
            candidates = []
            if _table_exists(conn, "conversation_messages"):
                candidates.append("conversation_messages")
            elif _table_exists(conn, "messages"):
                candidates.append("messages")
            if _table_exists(conn, "ai_chat_messages"):
                candidates.append("ai_chat_messages")
            return candidates

        candidates: List[str] = []
        if "messages" in allowed_tables:
            if _table_exists(conn, "conversation_messages"):
                candidates.append("conversation_messages")
            elif _table_exists(conn, "messages"):
                candidates.append("messages")
        if "conversation_messages" in allowed_tables and _table_exists(conn, "conversation_messages"):
            if "conversation_messages" not in candidates:
                candidates.append("conversation_messages")
        if (
            {"ai_chat", "ai_messages", "ai_chat_messages"} & set(allowed_tables)
            and _table_exists(conn, "ai_chat_messages")
        ):
            candidates.append("ai_chat_messages")
        return candidates

    sources = _allowed_message_sources()
    if not sources:
        return []

    # Keep single-table queries paginated in SQL; when multiple tables are allowed,
    # fetch and merge in Python so limit/offset apply to the combined result set.
    if sources == ["conversation_messages"]:
        oc = _message_time_order_column(conn, "conversation_messages")
        return _fetch_rows(
            f"SELECT * FROM conversation_messages ORDER BY {oc} DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
    if sources == ["messages"]:
        return _fetch_rows(
            "SELECT * FROM messages ORDER BY ts DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
    if sources == ["ai_chat_messages"]:
        oc = _message_time_order_column(conn, "ai_chat_messages")
        return _fetch_rows(
            f"SELECT * FROM ai_chat_messages ORDER BY {oc} DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )

    merged: List[Dict[str, Any]] = []
    if "conversation_messages" in sources:
        oc_cm = _message_time_order_column(conn, "conversation_messages")
        merged.extend(
            _fetch_rows(
                f"SELECT * FROM conversation_messages ORDER BY {oc_cm} DESC LIMIT ? OFFSET ?",
                (limit + offset, 0),
            )
        )
    if "messages" in sources:
        merged.extend(
            _fetch_rows(
                "SELECT * FROM messages ORDER BY ts DESC LIMIT ? OFFSET ?",
                (limit + offset, 0),
            )
        )
    if "ai_chat_messages" in sources:
        oc_ac = _message_time_order_column(conn, "ai_chat_messages")
        merged.extend(
            _fetch_rows(
                f"SELECT * FROM ai_chat_messages ORDER BY {oc_ac} DESC LIMIT ? OFFSET ?",
                (limit + offset, 0),
            )
        )
    merged.sort(key=lambda row: (row.get("event_at") or row.get("ts") or "", row.get("message_id") or ""), reverse=True)
    return merged[offset:offset + limit]


def _get_oplog_from_db(
    conn,
    dataset_id: Optional[str],
    limit: int,
    offset: int,
) -> List[Dict[str, Any]]:
    """Query oplog table if it exists; otherwise return empty list."""
    if not _table_exists(conn, "oplog"):
        return []
    query = "SELECT * FROM oplog ORDER BY hlc_ts LIMIT ? OFFSET ?"
    try:
        cursor = conn.execute(query, (limit, offset))
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    except Exception:
        return []


def _extract_bearer(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization")
    if not auth or not auth.startswith("Bearer "):
        return None
    return auth[7:].strip() or None


async def require_uma_rpt(request: Request, resource_id: str) -> Dict[str, Any]:
    """
    Dependency: validate RPT for this resource_id via Control Plane introspect_for_resource.
    Sets request.state.uma_introspection and returns it. Raises 401/403 if invalid.
    """
    base = get_control_plane_http_base()
    if not base:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="UMA not configured: TOPOS_CONTROL_PLANE_URL required for RPT validation",
        )
    token = _extract_bearer(request)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid Authorization: Bearer <RPT>",
        )
    try:
        payload = await introspect_for_resource(resource_id=resource_id, token=token)
    except RPTValidationError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    request.state.uma_introspection = payload
    await emit_usage_observation(
        action="uma.permission_ticket.validated",
        quantity=1,
        producer="api.uma_data",
        canonical_action_identity={
            "resource_id": resource_id,
            "rpt_token_sha": hashlib.sha256(token.encode("utf-8")).hexdigest(),
            "scope_count": len(payload.get("allowed_scopes") or []),
        },
        topos_id=parse_dataset_id_from_uma_dataset_resource_id(resource_id),
        trust_class="observe_only",
        metadata={"endpoint": "uma_introspect_for_resource"},
    )
    return payload


@router.get("/{resource_id}/data/messages")
async def get_uma_messages(
    request: Request,
    resource_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    dataset_id: Optional[str] = Query(None),
):
    """
    Return messages for the UMA resource, filtered by the permission's filters.
    Sprint 05: returns data only if RPT allows messages, ai_messages, or ai_chat scope.
    """
    resource_id = resource_id.strip()
    payload = await require_uma_rpt(request, resource_id)
    allowed_scopes = payload.get("allowed_scopes") or []
    allowed_tables = resolve_scopes_to_tables(allowed_scopes)
    if not allowed_tables:
        return {"messages": [], "count": 0}
    if not (
        may_access_table(allowed_tables, "messages")
        or may_access_table(allowed_tables, "conversation_messages")
        or may_access_table(allowed_tables, "ai_chat_messages")
        or may_access_table(allowed_tables, "ai_chat")
        or may_access_table(allowed_tables, "ai_messages")
    ):
        return {"messages": [], "count": 0}
    conn = get_db_connection()
    if not conn:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not initialized",
        )
    filters = (request.state.uma_introspection or {}).get("filters")
    manifest = extract_filter_manifest(filters if isinstance(filters, dict) else None)
    ai_only = bool(
        allowed_tables & {"ai_chat_messages", "ai_messages", "ai_chat"}
    ) and not bool(allowed_tables & {"messages", "conversation_messages"})
    conv_only = bool(allowed_tables & {"messages", "conversation_messages"}) and not bool(
        allowed_tables & {"ai_chat_messages", "ai_messages", "ai_chat"}
    )
    logical_table = "ai_chat_messages" if ai_only else "conversation_messages" if conv_only else None
    limited = get_limit_cap(limit, manifest, logical_table)
    effective_dataset_id = (dataset_id or "").strip() or parse_dataset_id_from_uma_dataset_resource_id(
        resource_id
    )
    items = _get_messages_from_db(conn, effective_dataset_id, limited, offset, allowed_tables=allowed_tables)
    try:
        # UMA data proxy = the grantee HTTP lane; filter unconditionally.
        from ..features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass

        items, uma_contact_sidecar = apply_message_contact_pipeline(
            items,
            blackhole_guard=BlackholeGuard(conn, caller_class=CallerClass.GRANTEE),
            conn=conn,
            dataset_id=effective_dataset_id,
            allowed_scopes=allowed_scopes,
            manifest=manifest,
            filters=filters if isinstance(filters, dict) else None,
        )
        manifest_for_generic = strip_contact_runtime_filters(manifest)
        fts = extract_field_transforms(filters if isinstance(filters, dict) else None)
        filtered = apply_filter_manifest(
            list(items),
            manifest_for_generic,
            field_transforms=fts,
            table_id=logical_table,
        )
    except UMAFilterError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return {
        "messages": filtered,
        "count": len(filtered),
        "message_owner": uma_contact_sidecar.get("message_owner") or {},
    }


@router.get("/{resource_id}/data/oplog")
async def get_uma_oplog(
    request: Request,
    resource_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    dataset_id: Optional[str] = Query(None),
):
    """
    Return oplog entries for the UMA resource, filtered by the permission's filters.
    Sprint 05: returns data only if RPT has at least one allowed scope (any read access).
    """
    resource_id = resource_id.strip()
    payload = await require_uma_rpt(request, resource_id)
    allowed_scopes = payload.get("allowed_scopes") or []
    allowed_tables = resolve_scopes_to_tables(allowed_scopes)
    if not allowed_tables:
        return {"ops": [], "count": 0}
    conn = get_db_connection()
    if not conn:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not initialized",
        )
    items = _get_oplog_from_db(conn, dataset_id, limit, offset)
    filters = (request.state.uma_introspection or {}).get("filters")
    try:
        filtered = apply_filters(items, filters)
    except UMAFilterError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return {"ops": filtered, "count": len(filtered)}
