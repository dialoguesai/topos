"""Source install, settings, contacts, sync, and signal upload handlers."""
from __future__ import annotations

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    Optional,
    REGISTRY,
    base64,
    datetime,
    enrich_contact_rows_with_resolved_display_names,
    enrich_conversation_thread_previews,
    get_signal_identity,
    get_source_settings,
    logger,
    put_signal_identity,
    put_source_settings,
    timezone,
    update_sync_result,
)
from .registry import handles


@handles("post_source_install")
async def handle_post_source_install(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...api.source_install import _install_source_core

    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    try:
        result = await _install_source_core(payload)
        return {"id": req_id, "status": "ok", "payload": result}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except RuntimeError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_source_install_status")
async def handle_get_source_install_status(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...api.source_install import _list_install_status_core

    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    try:
        result = await _list_install_status_core(payload)
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("delete_source_install")
async def handle_delete_source_install(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...api.source_install import _uninstall_source_core

    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    try:
        result = await _uninstall_source_core(payload)
        return {"id": req_id, "status": "ok", "payload": result}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except RuntimeError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("post_source_scrub")
async def handle_post_source_scrub(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...api.source_scrub import _scrub_source_core
    from ...sources.scrub_service import ScrubInProgressError

    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    try:
        result = await _scrub_source_core(payload)
        return {"id": req_id, "status": "ok", "payload": result}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except ScrubInProgressError as exc:
        return {"id": req_id, "status": "error", "error": str(exc), "code": 409}
    except RuntimeError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("patch_source_install")
async def handle_patch_source_install(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...api.source_install import _patch_source_install_core

    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    try:
        result = await _patch_source_install_core(payload)
        return {"id": req_id, "status": "ok", "payload": result}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except RuntimeError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("post_source_test_ingestion")
async def handle_post_source_test_ingestion(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...api.source_install import _test_ingestion_core

    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    try:
        result = await _test_ingestion_core(payload)
        return {"id": req_id, "status": "ok", "payload": result}
    except (ValueError, LookupError) as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("post_source_test_enrichment", "post_source_test_enrichment_trigger")
async def handle_post_source_test_enrichment(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    from ...api.source_install import _test_enrichment_core

    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    try:
        result = await _test_enrichment_core(payload)
        return {"id": req_id, "status": "ok", "payload": result}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_sources")
async def handle_get_sources(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...api.source_install import _list_sources_core

        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        result = await _list_sources_core(payload)
        sources = result.get("sources") if isinstance(result, dict) else []
        logger.debug("[PIPELINE:QUERY] get_sources returned %d scoped sources", len(sources) if isinstance(sources, list) else 0)
        return {"id": req_id, "status": "ok", "payload": result}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[PIPELINE:QUERY] get_sources error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_sources_overview")
async def handle_get_sources_overview(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    try:
        from ...api.sources_overview import _get_sources_overview_core

        payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
        result = await _get_sources_overview_core(payload)
        return {"id": req_id, "status": "ok", "payload": result}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[PIPELINE:QUERY] get_sources_overview error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_source_settings")
async def handle_get_source_settings(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = (payload.get("source_id") or "").strip()
    dataset_id = (payload.get("dataset_id") or "").strip()
    if not source_id or not dataset_id:
        return {"id": req_id, "status": "error", "error": "source_id and dataset_id required"}
    if not REGISTRY.get(source_id):
        return {"id": req_id, "status": "error", "error": "unknown source_id"}
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    settings_data = get_source_settings(conn, dataset_id, source_id)
    return {"id": req_id, "status": "ok", "payload": {"status": "ok", "dataset_id": dataset_id, "source_id": source_id, **settings_data}}

@handles("put_source_settings")
async def handle_put_source_settings(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = (payload.get("source_id") or "").strip()
    dataset_id = (payload.get("dataset_id") or "").strip()
    enabled = payload.get("enabled")
    # P1.4: posture is the per-connector OVERRIDE (personal|mixed|ambient) or
    # null to inherit the registry default. Only applied when the key is
    # present in the body — absent means "leave unchanged".
    posture_provided = "posture" in payload
    posture = payload.get("posture")
    if not source_id or not dataset_id:
        return {"id": req_id, "status": "error", "error": "source_id and dataset_id required"}
    source = REGISTRY.get(source_id)
    if not source:
        return {"id": req_id, "status": "error", "error": "unknown source_id"}
    # enabled/sync settings are local_sync-only; posture applies to ANY
    # connector (browser/chat/etc.). Require the local_sync gate only when an
    # enabled toggle is being set.
    if enabled is not None and getattr(source, "source_type", None) != "local_sync":
        return {"id": req_id, "status": "error", "error": "enabled only applies to local_sync sources"}
    if enabled is None and not posture_provided:
        return {"id": req_id, "status": "error", "error": "enabled or posture required in body"}
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    try:
        if posture_provided:
            put_source_settings(conn, dataset_id, source_id, enabled=enabled, posture=posture)
        else:
            put_source_settings(conn, dataset_id, source_id, enabled=enabled)
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    # Echo the effective settings back (posture reflects what was persisted /
    # inherited) so the caller and UI see the resolved state.
    settings_data = get_source_settings(conn, dataset_id, source_id) or {}
    result_payload = {"status": "ok", "dataset_id": dataset_id, "source_id": source_id}
    if enabled is not None:
        result_payload["enabled"] = bool(enabled)
    else:
        result_payload["enabled"] = bool(settings_data.get("enabled", True))
    result_payload["posture"] = settings_data.get("posture")
    return {"id": req_id, "status": "ok", "payload": result_payload}

@handles("get_source_contacts")
async def handle_get_source_contacts(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = (payload.get("source_id") or "").strip()
    dataset_id = (payload.get("dataset_id") or "").strip()
    if not source_id or not dataset_id:
        return {"id": req_id, "status": "error", "error": "source_id and dataset_id required"}
    source = REGISTRY.get(source_id)
    if not source or getattr(source, "source_type", None) != "local_sync":
        return {"id": req_id, "status": "error", "error": "contacts only apply to local_sync sources"}
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    from ...storage.canonical import ConversationsTablesManager
    manager = ConversationsTablesManager(conn)
    contacts = manager.list_contacts(dataset_id=dataset_id, source_id=source_id, limit=int(payload.get("limit") or 200))
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
    return {
        "id": req_id,
        "status": "ok",
        "payload": {
            "status": "ok",
            "dataset_id": dataset_id,
            "source_id": source_id,
            "contacts": contacts,
        },
    }

@handles("put_source_contact")
async def handle_put_source_contact(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = (payload.get("source_id") or "").strip()
    dataset_id = (payload.get("dataset_id") or "").strip()
    contact_id = (payload.get("contact_id") or "").strip()
    display_name = payload.get("display_name")
    sharing_policy = payload.get("sharing_policy")
    if not source_id or not dataset_id or not contact_id:
        return {"id": req_id, "status": "error", "error": "source_id, dataset_id, contact_id required"}
    source = REGISTRY.get(source_id)
    if not source or getattr(source, "source_type", None) != "local_sync":
        return {"id": req_id, "status": "error", "error": "contacts only apply to local_sync sources"}
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    from ...storage.canonical import ConversationsTablesManager
    manager = ConversationsTablesManager(conn)
    manager.update_contact_display_name(
        dataset_id=dataset_id,
        source_id=source_id,
        contact_id=contact_id,
        display_name=display_name,
    )
    if isinstance(sharing_policy, dict) and sharing_policy:
        manager.update_contact_sharing_policy(
            dataset_id=dataset_id,
            contact_id=contact_id,
            sharing_policy=sharing_policy,
        )
    return {
        "id": req_id,
        "status": "ok",
        "payload": {
            "status": "ok",
            "dataset_id": dataset_id,
            "source_id": source_id,
            "contact_id": contact_id,
            "display_name": display_name,
            "sharing_policy": sharing_policy if isinstance(sharing_policy, dict) else None,
        },
    }

@handles("auto_resolve_source_contacts")
async def handle_auto_resolve_source_contacts(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = (payload.get("source_id") or "").strip()
    dataset_id = (payload.get("dataset_id") or "").strip()
    if not source_id or not dataset_id:
        return {"id": req_id, "status": "error", "error": "source_id and dataset_id required"}
    source = REGISTRY.get(source_id)
    if not source or getattr(source, "source_type", None) != "local_sync":
        return {"id": req_id, "status": "error", "error": "contacts only apply to local_sync sources"}
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    from ...storage.canonical import ConversationsTablesManager
    manager = ConversationsTablesManager(conn)
    updated = manager.auto_resolve_contact_names(dataset_id=dataset_id, source_id=source_id)
    return {
        "id": req_id,
        "status": "ok",
        "payload": {
            "status": "ok",
            "dataset_id": dataset_id,
            "source_id": source_id,
            "updated_contacts": updated,
        },
    }

@handles("source_sync")
async def handle_source_sync(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    import asyncio as _asyncio
    payload = message.get("payload") or {}
    source_id = (payload.get("source_id") or "").strip()
    dataset_id = (payload.get("dataset_id") or "").strip()
    sync_options = payload.get("sync_options")
    if source_id == "imessage" and not sync_options:
        sync_options = {"mode": "3m"}
    logger.debug(
        "[PIPELINE:SYNC] Engine received source_sync request: source_id=%s dataset_id=%s sync_options=%s (req_id=%s)",
        source_id,
        dataset_id[:24] + "..." if dataset_id and len(dataset_id) > 24 else dataset_id,
        sync_options,
        req_id[:8] if req_id else "?",
    )
    if not source_id or not dataset_id:
        return {"id": req_id, "status": "error", "error": "source_id and dataset_id required"}
    source = REGISTRY.get(source_id)
    if not source or getattr(source, "source_type", None) != "local_sync":
        return {"id": req_id, "status": "error", "error": "sync only applies to local_sync sources"}
    from ...ingestion.local_sync import run_imessage_sync, run_signal_sync
    conn = hub.get_db_connection()
    try:
        if source_id == "imessage":
            result = await _asyncio.to_thread(run_imessage_sync, dataset_id, sync_options=sync_options)
        elif source_id == "signal":
            result = await _asyncio.to_thread(run_signal_sync, dataset_id, sync_options=sync_options)
        else:
            return {"id": req_id, "status": "error", "error": f"sync not implemented for source_id={source_id}"}
        status = result.get("status", "error")
        error = result.get("error")
        if status == "error" and error and ("84" in str(error) or "EOVERFLOW" in str(error).upper()):
            logger.warning(
                "[PIPELINE:SYNC] source_sync returned errno 84 / EOVERFLOW: source_id=%s error=%s",
                source_id,
                error,
                exc_info=False,
            )
        logger.debug("[PIPELINE:SYNC] source_sync completed: source_id=%s status=%s", source_id, status)
        if conn and status == "ok":
            update_sync_result(conn, dataset_id, source_id, success=True, last_sync_at=datetime.now(timezone.utc).isoformat())
        elif conn and status == "error":
            update_sync_result(conn, dataset_id, source_id, success=False, last_error=error or "Sync failed")
        return {"id": req_id, "status": status, "payload": result, "error": error}
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[PIPELINE:SYNC] source_sync raised exception: source_id=%s exc=%s",
            source_id,
            exc,
            exc_info=True,
        )
        if conn:
            update_sync_result(conn, dataset_id, source_id, success=False, last_error=str(exc))
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_signal_settings")
async def handle_get_signal_settings(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    dataset_id = (payload.get("dataset_id") or "").strip()
    if not dataset_id:
        return {"id": req_id, "status": "error", "error": "dataset_id required"}
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    identity = get_signal_identity(conn, dataset_id)
    if identity is None:
        return {"id": req_id, "status": "ok", "payload": {"status": "ok", "dataset_id": dataset_id, "my_phone_number": None, "my_signal_id": None}}
    return {"id": req_id, "status": "ok", "payload": {"status": "ok", "dataset_id": dataset_id, **identity}}

@handles("put_signal_settings")
async def handle_put_signal_settings(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    dataset_id = (payload.get("dataset_id") or "").strip()
    my_phone_number = payload.get("my_phone_number")
    my_signal_id = payload.get("my_signal_id")
    if not dataset_id:
        return {"id": req_id, "status": "error", "error": "dataset_id required"}
    conn = hub.get_db_connection()
    if not conn:
        return {"id": req_id, "status": "error", "error": "Database not available"}
    put_signal_identity(conn, dataset_id, my_phone_number=my_phone_number, my_signal_id=my_signal_id)
    return {"id": req_id, "status": "ok", "payload": {"status": "ok", "dataset_id": dataset_id}}

@handles("signal_upload")
async def handle_signal_upload(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    import asyncio as _asyncio
    payload = message.get("payload") or {}
    dataset_id = (payload.get("dataset_id") or "").strip()
    file_b64 = payload.get("file_base64")
    my_phone_number = payload.get("my_phone_number")
    owner_user_id = payload.get("owner_user_id")
    if not dataset_id:
        return {"id": req_id, "status": "error", "error": "dataset_id required"}
    if not file_b64:
        return {"id": req_id, "status": "error", "error": "file required (file_base64 in payload)"}
    try:
        file_bytes = base64.b64decode(file_b64)
    except Exception as e:
        return {"id": req_id, "status": "error", "error": f"Invalid file_base64: {e}"}
    from ...ingestion.local_sync import run_signal_upload
    try:
        result = await _asyncio.to_thread(run_signal_upload, dataset_id, file_bytes, my_phone_number=my_phone_number, owner_user_id=owner_user_id)
        return {"id": req_id, "status": result.get("status", "ok"), "payload": result, "error": result.get("error")}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}
