"""Scoped query orchestration message handlers."""
from __future__ import annotations

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    Optional,
    logger,
)
from .registry import handles


@handles("query", "query_live")
async def handle_query(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    raw_manifest = payload.get("manifest") or {}
    scope_id = str(payload.get("scope_id") or raw_manifest.get("scope_id") or "")
    intent = str(payload.get("intent") or payload.get("query") or "")
    logger.info(
        "query request: scope=%r mode=%r intent_chars=%d requester=%r",
        scope_id,
        str(payload.get("access_mode") or "summary"),
        len(intent),
        str(payload.get("requester_id") or "mcp"),
    )
    try:
        from ...query.manifest_validation import ManifestValidationError, resolve_scope_manifest
        from ...query.runtime import get_query_orchestrator

        filter_manifest = payload.get("filter_manifest") if isinstance(payload.get("filter_manifest"), dict) else None
        try:
            manifest = resolve_scope_manifest(
                scope_id,
                client_manifest=raw_manifest if raw_manifest else None,
                filter_manifest=filter_manifest,
            )
        except ManifestValidationError as exc:
            return {
                "id": req_id,
                "status": "ok",
                "payload": {
                    "turn_outcome": "denied",
                    "deny_reason": exc.code,
                    "public_result": None,
                    "session_id": payload.get("query_session_id") or payload.get("session_id"),
                },
            }
        # Client-supplied reference instant (offset-aware ISO): "yesterday"
        # resolves against the USER's calendar day, not this server's UTC day.
        # Unparseable/absent → None → planner wall clock.
        query_now = None
        raw_now = str(payload.get("now") or "").strip()
        if raw_now:
            try:
                from datetime import datetime

                query_now = datetime.fromisoformat(raw_now.replace("Z", "+00:00"))
            except ValueError:
                logger.debug("unparseable query now=%r ignored", raw_now)
        result = await get_query_orchestrator(conn=hub.get_db_connection()).execute(
            query_text=intent,
            scope_id=scope_id,
            access_mode=str(payload.get("access_mode") or "summary"),
            manifest=manifest,
            now=query_now,
            query_session_id=payload.get("query_session_id") or payload.get("session_id"),
            filter_manifest=filter_manifest,
            field_transforms=payload.get("field_transforms"),
            requester_id=str(payload.get("requester_id") or "mcp"),
            owner_id=str(payload.get("owner_user_id") or payload.get("owner_id") or "owner"),
            is_grantee_request=bool(payload.get("is_grantee_request")),
            disclosure_ceiling=str(payload.get("disclosure_ceiling") or "default"),
            explicit_disclosure_tier=str(payload.get("disclosure_tier")).strip()
            if payload.get("disclosure_tier")
            else None,
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except KeyError as exc:
        return {"id": req_id, "status": "error", "error": f"Missing field: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}
