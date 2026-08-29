from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, Query, Request  # noqa: F401 Body used in put_signal_settings

from ..auth import require_api_key
from ..core.state import get_db_connection
from ..ingestion.ingest_helpers import ingest_file_payload, ingest_ui_payload, resolve_file_format
from ..ingestion.local_sync import run_imessage_sync, run_signal_sync, run_signal_upload
from ..sources.definitions import DELIVERY_LOCAL_SYNC, accepts_app_ingest, is_file_delivery
from ..sources.registry import REGISTRY
from ..storage.signal_identity import get_signal_identity, put_signal_identity
from ..storage.source_settings import get_source_settings, put_source_settings, update_sync_result
from ..analytics.messenger_communities import compute_and_persist_messenger_analytics
from ..engine.usage_observation import emit_usage_observation
from ..engine.usage_guard import submit_usage_guard_check

router = APIRouter()
_GOOGLE_CONTACT_IMPORT_SESSIONS: dict[str, dict] = {}


def _resolve_contact_import_targets(payload: Optional[dict]) -> tuple[List[str], Optional[str]]:
    requested = (payload or {}).get("apply_to_sources")
    if requested is None:
        requested = ["imessage", "signal"]
    if not isinstance(requested, list) or not requested:
        return [], "apply_to_sources must be a non-empty list"
    valid_local_sync = {
        sid for sid, definition in REGISTRY.items()
        if getattr(definition, "source_type", None) == "local_sync"
    }
    targets = [str(s or "").strip() for s in requested if str(s or "").strip()]
    if not targets:
        return [], "apply_to_sources has no valid source ids"
    invalid = [sid for sid in targets if sid not in valid_local_sync]
    if invalid:
        return [], f"invalid local_sync source ids: {', '.join(invalid)}"
    return sorted(set(targets)), None


@router.post("/sources/{source_id}/ingest", dependencies=[Depends(require_api_key)])
async def ingest_source(
    source_id: str,
    request: Request,
    dataset_id: Optional[str] = None,
    user_id: Optional[str] = None,  # noqa: ARG001
    file_path: Optional[str] = None,
    payload: Optional[dict] = Body(default=None),
):
    source = REGISTRY.get(source_id)
    if not source:
        return {"status": "error", "error": "unknown source_id"}

    if is_file_delivery(source):
        file = None
        if request.headers.get("content-type", "").startswith("multipart/"):
            form = await request.form()
            file = form.get("file")
            dataset_id = dataset_id or form.get("dataset_id")
            file_path = file_path or form.get("file_path")
        if not file and not file_path:
            return {"status": "error", "error": "file or file_path required"}
        if not dataset_id:
            return {"status": "error", "error": "dataset_id required"}
        if file_path:
            estimated_bytes = 0
            try:
                estimated_bytes = int(os.path.getsize(file_path))
            except Exception:
                estimated_bytes = 0
            guard = await submit_usage_guard_check(
                usage_kind="file_transfer_bytes",
                units=estimated_bytes,
            )
            if not bool(guard.get("allowed")):
                return {
                    "status": "error",
                    "error": "usage_guard_denied",
                    "denial": guard.get("denial") or {},
                }
            return await ingest_file_payload(
                dataset_id=dataset_id,
                schema_id=source.schema_id,
                file_path=file_path,
                source_id=source_id,
                file_format=resolve_file_format(source_definition=source, file_path=file_path),
            )
        payload_bytes = await file.read()
        guard = await submit_usage_guard_check(
            usage_kind="file_transfer_bytes",
            units=len(payload_bytes),
        )
        if not bool(guard.get("allowed")):
            return {
                "status": "error",
                "error": "usage_guard_denied",
                "denial": guard.get("denial") or {},
            }
        return await ingest_file_payload(
            dataset_id=dataset_id,
            schema_id=source.schema_id,
            file_bytes=payload_bytes,
            source_id=source_id,
            file_format=resolve_file_format(source_definition=source),
        )

    if accepts_app_ingest(source):
        if payload is None:
            try:
                payload = await request.json()
            except Exception:
                payload = None
        if not dataset_id or not payload:
            return {"status": "error", "error": "dataset_id and payload required"}
        return await ingest_ui_payload(
            dataset_id=dataset_id,
            schema_id=source.schema_id,
            payload=payload,
            source_id=source_id,  # Pass source_id to enable direct processing
        )

    if source.delivery == DELIVERY_LOCAL_SYNC:
        # iMessage, Signal: ingestion is via sync endpoint (POST /sources/{source_id}/sync), not file/body upload
        return {
            "status": "error",
            "error": "local_sync sources use sync endpoint; use POST /sources/{source_id}/sync to sync",
        }

    return {"status": "error", "error": "unsupported source type"}


@router.get("/sources/{source_id}/settings", dependencies=[Depends(require_api_key)])
async def get_source_settings_endpoint(
    source_id: str,
    dataset_id: Optional[str] = Query(default=None),
):
    """Get source settings: enabled, last_sync_at, last_error (for local_sync: imessage, signal)."""
    source = REGISTRY.get(source_id)
    if not source:
        return {"status": "error", "error": "unknown source_id"}
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required"}
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    settings = get_source_settings(conn, dataset_id, source_id)
    return {"status": "ok", "dataset_id": dataset_id, "source_id": source_id, **settings}


@router.put("/sources/{source_id}/settings", dependencies=[Depends(require_api_key)])
async def put_source_settings_endpoint(
    source_id: str,
    dataset_id: Optional[str] = Query(default=None),
    body: Optional[dict] = Body(default=None),
):
    """Set source settings (e.g. enabled, exclude_spam). Valid for local_sync sources (imessage, signal)."""
    source = REGISTRY.get(source_id)
    if not source:
        return {"status": "error", "error": "unknown source_id"}
    if source.delivery != DELIVERY_LOCAL_SYNC:
        return {"status": "error", "error": "settings only apply to local_sync sources"}
    dataset_id = dataset_id or (body.get("dataset_id") if body else None)
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required"}
    body = body or {}
    enabled = body.get("enabled")
    posture_provided = "posture" in body
    exclude_spam_provided = "exclude_spam" in body
    if enabled is None and not posture_provided and not exclude_spam_provided:
        return {"status": "error", "error": "enabled, posture, or exclude_spam required in body"}
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    put_kwargs: dict = {"enabled": enabled}
    if posture_provided:
        put_kwargs["posture"] = body.get("posture")
    if exclude_spam_provided:
        put_kwargs["exclude_spam"] = bool(body.get("exclude_spam"))
    try:
        put_source_settings(conn, dataset_id, source_id, **put_kwargs)
    except ValueError as exc:
        return {"status": "error", "error": str(exc)}
    settings = get_source_settings(conn, dataset_id, source_id) or {}
    return {"status": "ok", "dataset_id": dataset_id, "source_id": source_id, **settings}


@router.post("/sources/{source_id}/sync", dependencies=[Depends(require_api_key)])
async def sync_source(
    source_id: str,
    dataset_id: Optional[str] = Query(default=None, description="Dataset/owner scope for checkpoint and messages"),
    body: Optional[dict] = Body(default=None),
):
    """Run sync for local_sync sources (e.g. iMessage). Reads since last checkpoint, writes to conversation_messages."""
    source = REGISTRY.get(source_id)
    if not source:
        return {"status": "error", "error": "unknown source_id"}
    if source.delivery != DELIVERY_LOCAL_SYNC:
        return {"status": "error", "error": "sync endpoint only applies to local_sync sources"}

    if not dataset_id:
        return {"status": "error", "error": "dataset_id required (query param)"}

    conn = get_db_connection()
    sync_options = (body or {}).get("sync_options")
    if source_id == "imessage" and not sync_options:
        sync_options = {"mode": "3m"}
    if source_id == "imessage":
        result = await asyncio.to_thread(run_imessage_sync, dataset_id, sync_options=sync_options)
    elif source_id == "signal":
        result = await asyncio.to_thread(run_signal_sync, dataset_id, sync_options=sync_options)
    else:
        return {"status": "error", "error": f"sync not implemented for source_id={source_id}"}

    if conn and result.get("status") == "ok":
        update_sync_result(
            conn, dataset_id, source_id,
            success=True,
            last_sync_at=datetime.now(timezone.utc).isoformat(),
        )
        # Sprint 03 trigger: refresh messenger analytics after successful sync.
        try:
            await asyncio.to_thread(
                compute_and_persist_messenger_analytics,
                dataset_id=dataset_id,
                conn=conn,
                source_ids=[source_id],
                period_granularity="month",
            )
        except Exception:
            # Non-fatal; sync result should still return success.
            pass
    elif conn and result.get("status") == "error":
        update_sync_result(
            conn, dataset_id, source_id,
            success=False,
            last_error=result.get("error", "Sync failed"),
        )
    return result


@router.post("/sources/signal/upload", dependencies=[Depends(require_api_key)])
async def upload_signal_export(
    request: Request,
    dataset_id: Optional[str] = Query(default=None),
    my_phone_number: Optional[str] = Query(default=None),
    owner_user_id: Optional[str] = Query(default=None),
):
    """Upload a Signal export file (JSON). Optional my_phone_number / owner_user_id for identity."""
    if not dataset_id:
        try:
            form = await request.form()
            dataset_id = dataset_id or form.get("dataset_id")
            if isinstance(dataset_id, bytes):
                dataset_id = dataset_id.decode("utf-8")
            my_phone_number = my_phone_number or form.get("my_phone_number")
            if isinstance(my_phone_number, bytes):
                my_phone_number = my_phone_number.decode("utf-8")
        except Exception:
            pass
        if not dataset_id:
            return {"status": "error", "error": "dataset_id required"}
    file_bytes = None
    if request.headers.get("content-type", "").startswith("multipart/"):
        form = await request.form()
        f = form.get("file")
        if f:
            file_bytes = await f.read()
    if not file_bytes:
        return {"status": "error", "error": "file required (multipart/form-data)"}
    result = await asyncio.to_thread(
        run_signal_upload,
        dataset_id,
        file_bytes,
        my_phone_number=my_phone_number,
        owner_user_id=owner_user_id,
    )
    return result


@router.get("/sources/signal/settings", dependencies=[Depends(require_api_key)])
async def get_signal_settings(
    dataset_id: Optional[str] = Query(default=None, description="Dataset scope for Signal identity"),
):
    """Get Signal identity (my_phone_number, my_signal_id) for the given dataset."""
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required"}
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    identity = get_signal_identity(conn, dataset_id)
    if identity is None:
        return {"status": "ok", "dataset_id": dataset_id, "my_phone_number": None, "my_signal_id": None}
    return {"status": "ok", "dataset_id": dataset_id, **identity}


@router.put("/sources/signal/settings", dependencies=[Depends(require_api_key)])
async def put_signal_settings(
    dataset_id: Optional[str] = Query(default=None),
    my_phone_number: Optional[str] = Query(default=None),
    my_signal_id: Optional[str] = Query(default=None),
    body: Optional[dict] = Body(default=None),
):
    """Set Signal identity for the dataset. Prefer phone number (e.g. E.164) for 'I am'; do not use Signal username (unreliable)."""
    if not dataset_id and body:
        dataset_id = body.get("dataset_id")
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required"}
    phone = my_phone_number or (body.get("my_phone_number") if body else None)
    sid = my_signal_id or (body.get("my_signal_id") if body else None)
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    put_signal_identity(conn, dataset_id, my_phone_number=phone, my_signal_id=sid)
    return {"status": "ok", "dataset_id": dataset_id}


@router.get("/sources/{source_id}/contacts", dependencies=[Depends(require_api_key)])
async def get_source_contacts(
    source_id: str,
    dataset_id: Optional[str] = Query(default=None),
):
    """List canonical contacts for local_sync source."""
    source = REGISTRY.get(source_id)
    if not source:
        return {"status": "error", "error": "unknown source_id"}
    if source.delivery != DELIVERY_LOCAL_SYNC:
        return {"status": "error", "error": "contacts only apply to local_sync sources"}
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required"}
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    from ..analytics.messenger_labels import (
        enrich_contact_rows_with_resolved_display_names,
        enrich_conversation_thread_previews,
    )
    from ..storage.canonical import ConversationsTablesManager
    manager = ConversationsTablesManager(conn)
    contacts = manager.list_contacts(dataset_id=dataset_id, source_id=source_id, limit=200)
    enrich_contact_rows_with_resolved_display_names(conn, dataset_id=dataset_id, contacts=contacts)
    for c in contacts:
        identifier = c.get("identifier")
        if identifier:
            pid = str(identifier)
            c["sample_messages"] = manager.get_contact_message_samples(
                dataset_id=dataset_id,
                source_id=source_id,
                identifier=identifier,
                limit=5,
            )
            previews = manager.get_contact_conversation_thread_previews(
                dataset_id=dataset_id,
                source_id=source_id,
                profile_identifier=pid,
            )
            enrich_conversation_thread_previews(
                conn,
                dataset_id=dataset_id,
                profile_identifier=pid,
                previews=previews,
            )
            c["conversation_thread_previews"] = previews
        else:
            c["conversation_thread_previews"] = []
    return {"status": "ok", "dataset_id": dataset_id, "source_id": source_id, "contacts": contacts}


@router.put("/sources/{source_id}/contacts/{contact_id}", dependencies=[Depends(require_api_key)])
async def put_source_contact(
    source_id: str,
    contact_id: str,
    dataset_id: Optional[str] = Query(default=None),
    body: Optional[dict] = Body(default=None),
):
    """Set display name for canonical contact."""
    source = REGISTRY.get(source_id)
    if not source:
        return {"status": "error", "error": "unknown source_id"}
    if source.delivery != DELIVERY_LOCAL_SYNC:
        return {"status": "error", "error": "contacts only apply to local_sync sources"}
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required"}
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    from ..storage.canonical import ConversationsTablesManager
    manager = ConversationsTablesManager(conn)
    manager.update_contact_display_name(
        dataset_id=dataset_id,
        source_id=source_id,
        contact_id=contact_id,
        display_name=(body or {}).get("display_name"),
    )
    return {"status": "ok", "dataset_id": dataset_id, "source_id": source_id, "contact_id": contact_id}


@router.post("/sources/{source_id}/contacts/auto-resolve", dependencies=[Depends(require_api_key)])
async def auto_resolve_source_contacts(
    source_id: str,
    dataset_id: Optional[str] = Query(default=None),
):
    """Auto-resolve contact display names from message metadata."""
    source = REGISTRY.get(source_id)
    if not source:
        return {"status": "error", "error": "unknown source_id"}
    if source.delivery != DELIVERY_LOCAL_SYNC:
        return {"status": "error", "error": "contacts only apply to local_sync sources"}
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required"}
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    from ..storage.canonical import ConversationsTablesManager
    manager = ConversationsTablesManager(conn)
    updated = manager.auto_resolve_contact_names(dataset_id=dataset_id, source_id=source_id)
    return {"status": "ok", "dataset_id": dataset_id, "source_id": source_id, "updated_contacts": updated}


@router.post("/contacts/import/apple", dependencies=[Depends(require_api_key)])
async def import_apple_contacts(
    dataset_id: Optional[str] = Query(default=None),
    body: Optional[dict] = Body(default=None),
):
    """Import Apple Contacts locally on engine host (global capability)."""
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required"}
    target_sources, err = _resolve_contact_import_targets(body or {})
    if err:
        return {"status": "error", "error": err}
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    try:
        from ..ingestion.sources.contact_importers import import_apple_contacts_local
        from ..storage.canonical import ConversationsTablesManager

        contacts = await asyncio.to_thread(import_apple_contacts_local)
        manager = ConversationsTablesManager(conn)
        import_run_id = str(uuid.uuid4())
        aggregate = manager.import_contacts_batch(
            dataset_id=dataset_id,
            contacts=contacts,
            target_sources=target_sources,
            import_source="apple_contacts",
            import_run_id=import_run_id,
        )
        return {
            "status": "ok",
            "dataset_id": dataset_id,
            "applied_sources": target_sources,
            "import_source": "apple_contacts",
            "contacts_discovered": len(contacts),
            **aggregate,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


@router.post("/contacts/import/google/start", dependencies=[Depends(require_api_key)])
async def start_google_contacts_import(
    dataset_id: Optional[str] = Query(default=None),
    body: Optional[dict] = Body(default=None),
):
    """Start Google Contacts device authorization flow."""
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required"}
    target_sources, err = _resolve_contact_import_targets(body or {})
    if err:
        return {"status": "error", "error": err}
    google_client_id = ((body or {}).get("google_client_id") or os.getenv("GOOGLE_OAUTH_CLIENT_ID") or "").strip()
    if not google_client_id:
        return {"status": "error", "error": "google_client_id required (or set GOOGLE_OAUTH_CLIENT_ID)"}
    try:
        from ..ingestion.sources.contact_importers import start_google_device_auth

        auth = await asyncio.to_thread(start_google_device_auth, google_client_id)
        if auth.get("error"):
            return {"status": "error", "error": auth.get("error_description") or auth.get("error")}
        session_id = str(uuid.uuid4())
        _GOOGLE_CONTACT_IMPORT_SESSIONS[session_id] = {
            "dataset_id": dataset_id,
            "apply_to_sources": target_sources,
            "google_client_id": google_client_id,
            "device_code": auth.get("device_code"),
            "interval": int(auth.get("interval") or 5),
        }
        await emit_usage_observation(
            action="contacts.google.connect.started",
            quantity=1,
            producer="api.ingestion_sources",
            canonical_action_identity={
                "dataset_id": dataset_id,
                "session_id": session_id,
                "provider": "google_contacts",
            },
            topos_id=dataset_id,
            trust_class="observe_only",
            metadata={"endpoint": "/contacts/import/google/start"},
        )
        return {
            "status": "ok",
            "session_id": session_id,
            "applied_sources": target_sources,
            "user_code": auth.get("user_code"),
            "verification_url": auth.get("verification_url") or auth.get("verification_uri"),
            "expires_in": auth.get("expires_in"),
            "message": auth.get("message"),
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


@router.post("/contacts/import/google/token", dependencies=[Depends(require_api_key)])
async def import_google_contacts_token(
    dataset_id: Optional[str] = Query(default=None),
    body: Optional[dict] = Body(default=None),
):
    """Import Google contacts directly from OAuth access token (UI sign-in flow)."""
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required"}
    target_sources, err = _resolve_contact_import_targets(body or {})
    if err:
        return {"status": "error", "error": err}
    access_token = str((body or {}).get("access_token") or "").strip()
    if not access_token:
        return {"status": "error", "error": "access_token required"}
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    try:
        from ..ingestion.sources.contact_importers import import_google_contacts
        from ..storage.canonical import ConversationsTablesManager

        contacts = await asyncio.to_thread(import_google_contacts, access_token)
        manager = ConversationsTablesManager(conn)
        import_run_id = str(uuid.uuid4())
        aggregate = manager.import_contacts_batch(
            dataset_id=dataset_id,
            contacts=contacts,
            target_sources=target_sources,
            import_source="google_contacts_oauth",
            import_run_id=import_run_id,
        )
        return {
            "status": "ok",
            "dataset_id": dataset_id,
            "applied_sources": target_sources,
            "import_source": "google_contacts_oauth",
            "contacts_discovered": len(contacts),
            **aggregate,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


@router.post("/contacts/import/google/finish", dependencies=[Depends(require_api_key)])
async def finish_google_contacts_import(
    dataset_id: Optional[str] = Query(default=None),
    body: Optional[dict] = Body(default=None),
):
    """Finish Google Contacts import once user authorizes device code."""
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required"}
    session_id = ((body or {}).get("session_id") or "").strip()
    if not session_id:
        return {"status": "error", "error": "session_id required"}
    session = _GOOGLE_CONTACT_IMPORT_SESSIONS.get(session_id)
    if not session:
        return {"status": "error", "error": "google import session not found or expired"}
    if session.get("dataset_id") != dataset_id:
        return {"status": "error", "error": "session does not match dataset_id"}
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "error": "Database not available"}
    try:
        from ..ingestion.sources.contact_importers import finish_google_device_auth, import_google_contacts
        from ..storage.canonical import ConversationsTablesManager

        token = await asyncio.to_thread(
            finish_google_device_auth,
            client_id=session.get("google_client_id"),
            device_code=session.get("device_code"),
            interval_seconds=int(session.get("interval") or 5),
            timeout_seconds=int((body or {}).get("timeout_seconds") or 120),
        )
        if token.get("error"):
            return {"status": "error", "error": token.get("error_description") or token.get("error")}
        access_token = token.get("access_token")
        if not access_token:
            return {"status": "error", "error": "Google token exchange did not return access_token"}
        contacts = await asyncio.to_thread(import_google_contacts, access_token)
        manager = ConversationsTablesManager(conn)
        target_sources = session.get("apply_to_sources") or []
        import_run_id = str(uuid.uuid4())
        aggregate = manager.import_contacts_batch(
            dataset_id=dataset_id,
            contacts=contacts,
            target_sources=target_sources,
            import_source="google_contacts",
            import_run_id=import_run_id,
        )
        _GOOGLE_CONTACT_IMPORT_SESSIONS.pop(session_id, None)
        return {
            "status": "ok",
            "dataset_id": dataset_id,
            "applied_sources": target_sources,
            "import_source": "google_contacts",
            "contacts_discovered": len(contacts),
            **aggregate,
        }
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
