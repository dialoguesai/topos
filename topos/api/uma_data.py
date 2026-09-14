# Topos UMA scoped data endpoints (US-3.8) and filter enforcement (US-3.6)
# Sprint 05: scope resolution — return data only for tables allowed by RPT scopes.
# See: control_plane/uma/uma_sprints/SPRINT_3_USER_SHARING.md, sprints_roles_scopes_stage_1/SPRINT_05_TOPOS_SCOPE_RESOLUTION.md

from __future__ import annotations

import hashlib

from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, HTTPException, Query, Request, status

from ..core.state import get_db_connection
from ..uma_rpt import RPTValidationError, get_control_plane_http_base, introspect_for_resource
from ..uma_filters import UMAFilterError, apply_filter_manifest, extract_field_transforms, extract_filter_manifest, get_limit_cap, query_filter_restriction_reason
from ..uma_contact_enrichment import apply_message_contact_pipeline, strip_contact_runtime_filters
from ..uma_resource_id import parse_dataset_id_from_uma_dataset_resource_id
from ..engine.usage_observation import emit_usage_observation
from ..uma_authority import bound_uma_scope, dataset_scope_predicate, message_stream_granted, local_node_resource_scope, require_local_resource_binding, raw_table_projection_allowed

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
    owner_user_id: Optional[str] = None,
    *, whole_engine_scope: bool = False,
) -> List[Dict[str, Any]]:
    """Read only explicitly granted message tables, scoped before pagination."""
    if not allowed_tables:
        return []
    sources: List[str] = []
    if {"messages", "conversation_messages"} & allowed_tables:
        if _table_exists(conn, "conversation_messages"):
            sources.append("conversation_messages")
        elif _table_exists(conn, "messages"):
            sources.append("messages")
    if {"ai_chat", "ai_messages", "ai_chat_messages"} & allowed_tables and _table_exists(conn, "ai_chat_messages"):
        sources.append("ai_chat_messages")
    merged: List[Dict[str, Any]] = []
    from ..disclosure.tier import apply_disclosure_tier_to_rows
    for table in sources:
        scope, scope_params = dataset_scope_predicate(
            _sqlite_table_columns(conn, table), dataset_id, owner_user_id,
            whole_engine_scope=whole_engine_scope,
        )
        order_column = _message_time_order_column(conn, table)
        # Fetch at most the requested prefix from each eligible table; no
        # out-of-dataset row can consume the limit or alter the returned count.
        cursor = conn.execute(
            f'SELECT * FROM "{table}" WHERE {scope} ORDER BY "{order_column}" DESC, message_id DESC LIMIT ?',
            scope_params + (limit + offset,),
        )
        columns = [column[0] for column in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        disclosure_table = "conversation_messages" if table == "messages" else table
        merged.extend(apply_disclosure_tier_to_rows(rows, table=disclosure_table, tier="default_disclosure"))
    merged.sort(key=lambda row: (row.get("event_at") or row.get("ts") or "", row.get("message_id") or ""), reverse=True)
    return merged[offset:offset + limit]


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
    Exact message-family scopes select the tables; resource authority selects rows.
    """
    resource_id = resource_id.strip()
    payload = await require_uma_rpt(request, resource_id)
    try:
        # Validate both introspected hints and any narrower query hint.
        bound_uma_scope({**payload, "resource_id": resource_id})
        effective_dataset_id, bound_owner = bound_uma_scope({
            **payload, "resource_id": resource_id, "dataset_id": dataset_id,
        })
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    allowed_scopes = payload.get("allowed_scopes") or []
    # This endpoint can combine the two streams only when both are granted.
    allowed_tables: Set[str] = set()
    if message_stream_granted(allowed_scopes, "conversation"):
        allowed_tables.update({"conversation_messages", "messages"})
    if message_stream_granted(allowed_scopes, "ai_chat"):
        allowed_tables.add("ai_chat_messages")
    if not allowed_tables:
        raise HTTPException(status_code=403, detail="message_scope_required")
    conn = get_db_connection()
    if not conn:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database not initialized",
        )
    try:
        require_local_resource_binding(conn, resource_id, bound_owner)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    filters = (request.state.uma_introspection or {}).get("filters")
    from ..features.lifecycle.record_protection import protection_fingerprint

    protection_revision = protection_fingerprint(conn)
    manifest = extract_filter_manifest(filters if isinstance(filters, dict) else None)
    allowed_tables = {table for table in allowed_tables if raw_table_projection_allowed(allowed_scopes, manifest, table)}
    if not allowed_tables:
        raise HTTPException(status_code=403, detail="raw_table_projection_not_granted")
    if query_filter_restriction_reason(filters, "raw") == "empty_allowlist":
        return {"messages": [], "count": 0, "message_owner": {}}
    ai_only = bool(
        allowed_tables & {"ai_chat_messages", "ai_messages", "ai_chat"}
    ) and not bool(allowed_tables & {"messages", "conversation_messages"})
    conv_only = bool(allowed_tables & {"messages", "conversation_messages"}) and not bool(
        allowed_tables & {"ai_chat_messages", "ai_messages", "ai_chat"}
    )
    logical_table = "ai_chat_messages" if ai_only else "conversation_messages" if conv_only else None
    limited = get_limit_cap(limit, manifest, logical_table)
    try:
        items = _get_messages_from_db(
            conn, effective_dataset_id, limited, offset,
            allowed_tables=allowed_tables, owner_user_id=bound_owner,
            whole_engine_scope=local_node_resource_scope(conn, resource_id, rpt_validated=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
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
    if protection_fingerprint(conn) != protection_revision:
        raise HTTPException(status_code=409, detail="authorization_changed")
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
    """No safe grantable projection exists for raw operation logs."""
    await require_uma_rpt(request, resource_id.strip())
    raise HTTPException(status_code=403, detail="shared_oplog_unavailable")
