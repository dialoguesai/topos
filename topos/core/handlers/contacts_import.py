"""Apple and Google contacts import message handlers."""
from __future__ import annotations

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    List,
    Optional,
    REGISTRY,
    asyncio,
    datetime,
    logger,
    os,
    timezone,
    uuid,
)
from .registry import handles


_GOOGLE_CONTACT_IMPORT_SESSIONS: Dict[str, Dict[str, Any]] = {}

def _resolve_contact_import_targets(payload: Dict[str, Any]) -> tuple[str, List[str], Optional[str]]:
    dataset_id = (payload.get("dataset_id") or "").strip()
    if not dataset_id:
        return "", [], "dataset_id required"
    requested = payload.get("apply_to_sources")
    if requested is None:
        requested = ["imessage", "signal"]
    if not isinstance(requested, list) or not requested:
        return "", [], "apply_to_sources must be a non-empty list"
    valid_local_sync = {
        sid for sid, definition in REGISTRY.items()
        if getattr(definition, "source_type", None) == "local_sync"
    }
    targets = [str(s or "").strip() for s in requested if str(s or "").strip()]
    if not targets:
        return "", [], "apply_to_sources has no valid source ids"
    invalid = [sid for sid in targets if sid not in valid_local_sync]
    if invalid:
        return "", [], f"invalid local_sync source ids: {', '.join(invalid)}"
    return dataset_id, sorted(set(targets)), None

@handles("import_apple_contacts", "import_contacts_apple_global")
async def handle_import_apple_contacts(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    dataset_id, target_sources, err = _resolve_contact_import_targets(payload)
    if err:
        return {"id": req_id, "status": "error", "error": err}
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    try:
        logger.info(
            "[CONTACT_IMPORT] engine request start: type=apple dataset_id=%s contact_namespaces=%s req_id=%s",
            dataset_id,
            target_sources,
            req_id[:8] if req_id else "?",
        )
        from ...ingestion.sources.contact_importers import import_apple_contacts_local
        from ...storage.canonical import ConversationsTablesManager

        contacts = await asyncio.to_thread(import_apple_contacts_local)
        manager = ConversationsTablesManager(conn)
        aggregate = manager.import_contacts_batch(
            dataset_id=dataset_id,
            contacts=contacts,
            target_sources=target_sources,
            import_source="apple_contacts",
            import_run_id=req_id,
        )
        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "status": "ok",
                "dataset_id": dataset_id,
                "applied_sources": target_sources,
                "import_source": "apple_contacts",
                "contacts_discovered": len(contacts),
                **aggregate,
            },
        }
    except Exception as exc:
        logger.exception(
            "[CONTACT_IMPORT] engine request failed: type=apple dataset_id=%s contact_namespaces=%s req_id=%s error=%s",
            dataset_id,
            target_sources,
            req_id[:8] if req_id else "?",
            str(exc),
        )
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("import_google_contacts_token", "import_google_contacts_token_global")
async def handle_import_google_contacts_token(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    dataset_id, target_sources, err = _resolve_contact_import_targets(payload)
    if err:
        return {"id": req_id, "status": "error", "error": err}
    access_token = str(payload.get("access_token") or "").strip()
    if not access_token:
        return {"id": req_id, "status": "error", "error": "access_token required"}
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    try:
        logger.info(
            "[CONTACT_IMPORT] engine request start: type=google_token dataset_id=%s contact_namespaces=%s req_id=%s",
            dataset_id,
            target_sources,
            req_id[:8] if req_id else "?",
        )
        from ...ingestion.sources.contact_importers import import_google_contacts
        from ...storage.canonical import ConversationsTablesManager

        contacts = await asyncio.to_thread(import_google_contacts, access_token)
        manager = ConversationsTablesManager(conn)
        aggregate = manager.import_contacts_batch(
            dataset_id=dataset_id,
            contacts=contacts,
            target_sources=target_sources,
            import_source="google_contacts_oauth",
            import_run_id=req_id,
        )
        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "status": "ok",
                "dataset_id": dataset_id,
                "applied_sources": target_sources,
                "import_source": "google_contacts_oauth",
                "contacts_discovered": len(contacts),
                **aggregate,
            },
        }
    except Exception as exc:
        logger.exception(
            "[CONTACT_IMPORT] engine request failed: type=google_token dataset_id=%s contact_namespaces=%s req_id=%s error=%s",
            dataset_id,
            target_sources,
            req_id[:8] if req_id else "?",
            str(exc),
        )
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("start_google_contacts_import", "start_google_contacts_import_global")
async def handle_start_google_contacts_import(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    dataset_id, target_sources, err = _resolve_contact_import_targets(payload)
    if err:
        return {"id": req_id, "status": "error", "error": err}
    google_client_id = (payload.get("google_client_id") or os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
    if not google_client_id:
        return {"id": req_id, "status": "error", "error": "google_client_id required (or set GOOGLE_OAUTH_CLIENT_ID)"}
    try:
        logger.info(
            "[CONTACT_IMPORT] engine request start: type=google_start dataset_id=%s contact_namespaces=%s req_id=%s",
            dataset_id,
            target_sources,
            req_id[:8] if req_id else "?",
        )
        from ...ingestion.sources.contact_importers import start_google_device_auth

        auth = await asyncio.to_thread(start_google_device_auth, google_client_id)
        if auth.get("error"):
            return {"id": req_id, "status": "error", "error": auth.get("error_description") or auth.get("error")}
        session_id = str(uuid.uuid4())
        _GOOGLE_CONTACT_IMPORT_SESSIONS[session_id] = {
            "dataset_id": dataset_id,
            "apply_to_sources": target_sources,
            "google_client_id": google_client_id,
            "device_code": auth.get("device_code"),
            "interval": int(auth.get("interval") or 5),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "status": "ok",
                "session_id": session_id,
                "applied_sources": target_sources,
                "user_code": auth.get("user_code"),
                "verification_url": auth.get("verification_url") or auth.get("verification_uri"),
                "expires_in": auth.get("expires_in"),
                "message": auth.get("message"),
            },
        }
    except Exception as exc:
        logger.exception(
            "[CONTACT_IMPORT] engine request failed: type=google_start dataset_id=%s contact_namespaces=%s req_id=%s error=%s",
            dataset_id,
            target_sources,
            req_id[:8] if req_id else "?",
            str(exc),
        )
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("finish_google_contacts_import", "finish_google_contacts_import_global")
async def handle_finish_google_contacts_import(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    session_id = (payload.get("session_id") or "").strip()
    if not session_id:
        return {"id": req_id, "status": "error", "error": "session_id required"}
    session = _GOOGLE_CONTACT_IMPORT_SESSIONS.get(session_id)
    if not session:
        return {"id": req_id, "status": "error", "error": "google import session not found or expired"}
    if (
        (payload.get("dataset_id") or "").strip() != (session.get("dataset_id") or "")
    ):
        return {"id": req_id, "status": "error", "error": "session does not match dataset_id"}
    dataset_id = session.get("dataset_id")
    target_sources = session.get("apply_to_sources") or []
    google_client_id = session.get("google_client_id")
    device_code = session.get("device_code")
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    try:
        logger.info(
            "[CONTACT_IMPORT] engine request start: type=google_finish dataset_id=%s contact_namespaces=%s session_id=%s req_id=%s",
            dataset_id,
            target_sources,
            session_id[:8],
            req_id[:8] if req_id else "?",
        )
        from ...ingestion.sources.contact_importers import finish_google_device_auth, import_google_contacts
        from ...storage.canonical import ConversationsTablesManager

        token = await asyncio.to_thread(
            finish_google_device_auth,
            client_id=google_client_id,
            device_code=device_code,
            interval_seconds=int(session.get("interval") or 5),
            timeout_seconds=int(payload.get("timeout_seconds") or 120),
        )
        if token.get("error"):
            return {"id": req_id, "status": "error", "error": token.get("error_description") or token.get("error")}
        access_token = token.get("access_token")
        if not access_token:
            return {"id": req_id, "status": "error", "error": "Google token exchange did not return access_token"}
        contacts = await asyncio.to_thread(import_google_contacts, access_token)
        manager = ConversationsTablesManager(conn)
        aggregate = manager.import_contacts_batch(
            dataset_id=dataset_id,
            contacts=contacts,
            target_sources=target_sources,
            import_source="google_contacts",
            import_run_id=req_id,
        )
        _GOOGLE_CONTACT_IMPORT_SESSIONS.pop(session_id, None)
        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "status": "ok",
                "dataset_id": dataset_id,
                "applied_sources": target_sources,
                "import_source": "google_contacts",
                "contacts_discovered": len(contacts),
                **aggregate,
            },
        }
    except Exception as exc:
        logger.exception(
            "[CONTACT_IMPORT] engine request failed: type=google_finish dataset_id=%s contact_namespaces=%s session_id=%s req_id=%s error=%s",
            dataset_id,
            target_sources,
            session_id[:8],
            req_id[:8] if req_id else "?",
            str(exc),
        )
        return {"id": req_id, "status": "error", "error": str(exc)}
