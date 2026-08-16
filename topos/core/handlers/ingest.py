"""App ingest and ingestion lifecycle message handlers."""
from __future__ import annotations

import asyncio

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    Optional,
    REGISTRY,
    RawFileStore,
    asyncio,
    base64,
    ingest_file_payload,
    ingest_ui_payload,
    logger,
    resolve_file_format,
    settings,
)
from .registry import handles


def _owner_user_id_from_dataset_id(dataset_id: Optional[str]) -> Optional[str]:
    raw = str(dataset_id or "").strip()
    if not raw or ":" not in raw:
        return None
    owner = raw.split(":", 1)[0].strip()
    return owner or None

async def _download_ingestion_payload(file_url: str) -> bytes:
    """Download ingestion payload from either HTTPS or gs:// URL."""
    if str(file_url).startswith("gs://"):
        from google.cloud import storage

        # gs://bucket/path -> bucket, blob path
        remainder = str(file_url)[len("gs://") :]
        if "/" not in remainder:
            raise ValueError(f"Invalid gs:// URL (missing object path): {file_url}")
        bucket_name, blob_path = remainder.split("/", 1)
        if not bucket_name or not blob_path:
            raise ValueError(f"Invalid gs:// URL: {file_url}")

        def _download_bytes() -> bytes:
            client = storage.Client()
            blob = client.bucket(bucket_name).blob(blob_path)
            return blob.download_as_bytes()

        return await asyncio.to_thread(_download_bytes)

    import httpx

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(file_url)
        resp.raise_for_status()
        return resp.content

@handles("app_ingest")
async def handle_app_ingest(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    async def _run_app_ingest():
        # Sprint 4 US-4.4: Ingest records using UMA write permission (Control Plane forwards here).
        payload = message.get("payload") or {}
        user_id = payload.get("user_id")
        dataset_id = payload.get("dataset_id")
        source_id = payload.get("source_id") or "chatgpt_ui_conversation"
        schema_id = payload.get("schema_id") or "chatgpt.conversation.v1"
        write_id = str(payload.get("write_id") or "").strip() or None

        if write_id:
            from ...ingestion.usage_inbox_dedupe import (
                ensure_derivation_enqueued,
                get_prior_delivery,
                is_derivation_complete,
                record_delivery,
            )

            # Each of these opens the dedupe schema (DDL under the write gate —
            # a blocking OS lock), so they run off the event-loop thread. They
            # resolve their own connection internally, which is what makes the
            # hop safe: get_db_connection is thread-local.
            prior = await asyncio.to_thread(get_prior_delivery, write_id)
            if prior is not None:
                if not await asyncio.to_thread(is_derivation_complete, write_id):
                    await asyncio.to_thread(
                        ensure_derivation_enqueued,
                        write_id,
                        source_id=source_id,
                    )
                    from ...pipeline.job_runner import start_pipeline_worker

                    start_pipeline_worker(hub.get_db_connection)
                response_payload = {
                    "write_id": write_id,
                    "records_processed": prior["records_processed"],
                    "records_total": prior["records_total"],
                    "errors": [],
                    "deduplicated": True,
                }
                return {"id": req_id, "status": "ok", "payload": response_payload}

        _LEGACY_CHAT_SOURCE_ID = "chatgpt_ui_conversation"
        if source_id != _LEGACY_CHAT_SOURCE_ID:
            source_def = REGISTRY.get(source_id)
            if not source_def:
                return {
                    "id": req_id,
                    "status": "error",
                    "error_code": "engine_error",
                    "error": (
                        f"Source '{source_id}' is not installed on this engine. "
                        "Install the source before ingesting."
                    ),
                }
            from ...sources.definitions import accepts_app_ingest

            if not accepts_app_ingest(source_def):
                return {
                    "id": req_id,
                    "status": "error",
                    "error_code": "engine_error",
                    "error": (
                        f"Source '{source_id}' has delivery={source_def.delivery!r}; "
                        "app_ingest requires a streamed source (delivery client_push or owner_ui)."
                    ),
                }
            if getattr(source_def, "schema_id", None):
                schema_id = str(source_def.schema_id)
        else:
            source_def = REGISTRY.get(source_id)
            if source_def and getattr(source_def, "schema_id", None):
                schema_id = str(source_def.schema_id)
        records = payload.get("records") or []
        if payload.get("encrypted"):
            try:
                from ...ingestion.inbox_crypto import decrypt_inbox_records_if_needed

                records = decrypt_inbox_records_if_needed(records)
            except Exception as exc:
                return {
                    "id": req_id,
                    "status": "error",
                    "error_code": "engine_error",
                    "error": f"Failed to decrypt inbox payload: {exc}",
                }
        if not user_id or not dataset_id:
            return {
                "id": req_id,
                "status": "error",
                "error_code": "schema_validation",
                "error": "user_id and dataset_id required",
            }
        if not records:
            return {
                "id": req_id,
                "status": "error",
                "error_code": "schema_validation",
                "error": "records required and must be non-empty",
            }
        from ...ingestion.ingest_helpers import ingest_ui_payload
        from ...pipeline.job_store import enqueue_job
        from ...pipeline.job_runner import start_pipeline_worker
        processed = 0
        errors = []
        records_total = len(records)
        enrichment_contexts: list[dict] = []
        # Ack fast: canonicalize + flat write only; enrichment/signals run in background.
        defer_enrichment = True
        for i, rec in enumerate(records):
            if not isinstance(rec, dict):
                err = "record must be a dict"
                errors.append({"index": i, "error": err})
                logger.warning(
                    "[PIPELINE:APP_INGEST] Record failed: source_id=%s index=%s error=%s",
                    source_id,
                    i,
                    err,
                )
                continue
            try:
                result = await ingest_ui_payload(
                    dataset_id=dataset_id,
                    schema_id=schema_id,
                    payload=rec,
                    source_id=source_id,
                    defer_enrichment=defer_enrichment,
                )
                if result.get("status") == "ok":
                    processed += 1
                    ctx = result.get("_enrichment_ctx")
                    if isinstance(ctx, dict):
                        enrichment_contexts.append(ctx)
                else:
                    err = result.get("error", "ingest failed")
                    errors.append({"index": i, "error": err})
                    logger.warning(
                        "[PIPELINE:APP_INGEST] Record failed: source_id=%s index=%s error=%s",
                        source_id,
                        i,
                        err,
                    )
            except Exception as exc:
                errors.append({"index": i, "error": str(exc)})
                logger.warning(
                    "[PIPELINE:APP_INGEST] Record failed: source_id=%s index=%s error=%s",
                    source_id,
                    i,
                    exc,
                    exc_info=True,
                )
        if errors:
            logger.warning(
                "[PIPELINE:APP_INGEST] Ingest completed with failures: source_id=%s processed=%d total=%d error_count=%d first_error=%s",
                source_id,
                processed,
                records_total,
                len(errors),
                errors[0].get("error"),
            )
        if user_id:
            resource_id = (payload.get("resource_id") or "").strip() or f"dataset:{user_id}:{dataset_id or 'default'}"

            def _record_app_ingest_uma() -> None:
                # record_uma_request takes the write gate (and on first use runs
                # the uma_access_requests ensure/migrate DDL) — keep it off the
                # event-loop thread. Own connection: get_db_connection is
                # thread-local.
                own = hub.get_db_connection()
                if own is None:
                    return
                hub.record_uma_request(
                    own,
                    owner_user_id=user_id,
                    resource_id=resource_id,
                    request_type="write",
                    endpoint="app_ingest",
                    requesting_user_id=(payload.get("requesting_user_id") or None),
                    app_id=payload.get("app_id"),
                    requesting_user_email=(payload.get("requesting_user_email") or "").strip() or None,
                    access_channel=(payload.get("access_channel") or "internal").strip() or "internal",
                )

            await asyncio.to_thread(_record_app_ingest_uma)
        response_payload = {
            "records_processed": processed,
            "records_total": records_total,
            "errors": errors,
        }
        if processed > 0 and enrichment_contexts:
            db_conn = hub.get_db_connection()
            if db_conn:
                merged_records: list = []
                sync_batch_id = None
                for ctx in enrichment_contexts:
                    merged_records.extend(ctx.get("canonical_records") or [])
                    sync_batch_id = sync_batch_id or ctx.get("sync_batch_id")
                job_payload = {
                    "source_id": source_id,
                    "sync_batch_id": sync_batch_id,
                    "canonical_records": merged_records,
                    "write_id": write_id,
                }
                idempotency = f"inbox_derivation:{write_id}" if write_id else f"inbox:{source_id}:{sync_batch_id}"

                def _enqueue_inbox_job() -> None:
                    # enqueue_job takes the write gate — a blocking OS lock —
                    # so it must not run on the event-loop thread (one of the
                    # 2026-08-07 freeze acquisition sites). Own connection:
                    # get_db_connection is thread-local.
                    own = hub.get_db_connection()
                    if own is None:
                        return
                    enqueue_job(
                        own,
                        kind="inbox_deferred_enrichment",
                        payload=job_payload,
                        source_id=source_id,
                        write_id=write_id,
                        sync_batch_id=str(sync_batch_id) if sync_batch_id else None,
                        idempotency_key=idempotency,
                    )

                await asyncio.to_thread(_enqueue_inbox_job)
                start_pipeline_worker(hub.get_db_connection)
        if write_id and processed > 0:
            from ...ingestion.usage_inbox_dedupe import record_delivery

            # Writes + purge under the write gate; own connection resolved in
            # the worker (see _enqueue_inbox_job above).
            await asyncio.to_thread(
                record_delivery,
                write_id,
                records_processed=processed,
                records_total=records_total,
            )
            response_payload["write_id"] = write_id
        elif write_id:
            response_payload["write_id"] = write_id
        if processed == 0:
            return {
                "id": req_id,
                "status": "error",
                "error_code": "schema_validation",
                "error": f"All {records_total} record(s) failed ingestion for source_id={source_id}",
                "payload": response_payload,
            }
        return {
            "id": req_id,
            "status": "ok",
            "payload": response_payload,
        }

    payload_for_drain = message.get("payload") or {}
    inbox_write_id = str(payload_for_drain.get("write_id") or "").strip() or None
    if inbox_write_id:
        from ...ingestion.inbox_drain import run_inbox_app_ingest

        return await run_inbox_app_ingest(_run_app_ingest, write_id=inbox_write_id)
    return await _run_app_ingest()

@handles("check_inbox_write")
async def handle_check_inbox_write(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    write_id = str(payload.get("write_id") or "").strip()
    if not write_id:
        return {
            "id": req_id,
            "status": "error",
            "error_code": "schema_validation",
            "error": "write_id required",
        }
    from ...ingestion.usage_inbox_dedupe import get_prior_delivery

    # Opens the dedupe schema under the write gate — off the loop thread.
    prior = await asyncio.to_thread(get_prior_delivery, write_id)
    response_payload = {
        "write_id": write_id,
        "delivered": prior is not None,
    }
    if prior is not None:
        response_payload["records_processed"] = prior["records_processed"]
        response_payload["records_total"] = prior["records_total"]
    return {"id": req_id, "status": "ok", "payload": response_payload}

@handles("start_ingestion")
async def handle_start_ingestion(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    import sys
    import uuid

    print(f"\033[93m[CRITICAL TOPOS HANDLER] start_ingestion ENTERED: req_id={req_id}\033[0m", file=sys.stderr, flush=True)
    
    payload = message.get("payload") or {}
    dataset_id = payload.get("dataset_id")
    owner_user_id = _owner_user_id_from_dataset_id(dataset_id)
    schema_id = payload.get("schema_id") or "chatgpt.conversation.v1"
    file_url = payload.get("file_url")
    file_base64 = payload.get("file_base64")
    file_path = payload.get("file_path")
    job_id = payload.get("job_id")
    source_id = payload.get("source_id")  # Optional source_id from control plane
    source_definition = payload.get("source_definition")
    file_format = resolve_file_format(
        source_definition=source_definition,
        file_path=file_path,
        default=str(payload.get("file_format") or "jsonl"),
    )
    
    print(f"\033[93m[CRITICAL TOPOS HANDLER] start_ingestion params: job_id={job_id}, dataset_id={dataset_id}, schema_id={schema_id}\033[0m", file=sys.stderr, flush=True)
    
    if not job_id:
        print(f"\033[93m[CRITICAL TOPOS HANDLER] start_ingestion ERROR: Missing job_id\033[0m", file=sys.stderr, flush=True)
        return {"id": req_id, "status": "error", "error": "job_id required"}
    
    try:
        from ...pipeline.job_store import enqueue_job, get_job
        from ...pipeline.job_runner import start_pipeline_worker

        progress_api_url = None
        progress_api_key = str(payload.get("progress_api_key") or settings.topos_key or "")

        control_plane_url = settings.topos_control_plane_url
        if control_plane_url:
            if control_plane_url.startswith("wss://"):
                progress_api_url = control_plane_url.replace("wss://", "https://").split("/ws/")[0]
            elif control_plane_url.startswith("ws://"):
                progress_api_url = control_plane_url.replace("ws://", "http://").split("/ws/")[0]
            else:
                progress_api_url = control_plane_url

        db_conn = hub.get_db_connection()
        if not db_conn:
            return {"id": req_id, "status": "error", "error": "Database connection not available"}

        existing = get_job(db_conn, str(job_id))
        if existing and existing.get("status") == "done":
            return {
                "id": req_id,
                "status": "ok",
                "payload": {"job_id": job_id, "status": "completed", "deduplicated": True},
            }

        job_payload: Dict[str, Any] = {
            "dataset_id": dataset_id or "",
            "schema_id": schema_id,
            "file_format": file_format,
            "job_id": job_id,
            "source_id": source_id,
            "source_definition": source_definition,
            "progress_api_url": progress_api_url,
            "progress_api_key": progress_api_key,
            "owner_user_id": owner_user_id,
        }
        if isinstance(file_base64, str) and file_base64:
            job_payload["file_base64"] = file_base64
        elif file_url:
            job_payload["file_url"] = str(file_url)
        else:
            job_payload["file_path"] = file_path

        def _enqueue_file_job() -> None:
            # Write-gate acquisition: keep it off the event-loop thread.
            own = hub.get_db_connection()
            if own is None:
                return
            enqueue_job(
                own,
                kind="file_ingestion",
                payload=job_payload,
                job_id=str(job_id),
                source_id=source_id,
                idempotency_key=f"file_ingestion:{job_id}",
            )

        await asyncio.to_thread(_enqueue_file_job)
        start_pipeline_worker(hub.get_db_connection)

        return {"id": req_id, "status": "ok", "payload": {"job_id": job_id, "status": "processing"}}
    except Exception as exc:  # noqa: BLE001
        print(f"\033[91m[CRITICAL TOPOS HANDLER] start_ingestion exception: {exc}\033[0m", file=sys.stderr, flush=True)
        import traceback
        print(f"\033[91m[CRITICAL TOPOS HANDLER] Traceback:\n{traceback.format_exc()}\033[0m", file=sys.stderr, flush=True)
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("ingestion_reprocess")
async def handle_ingestion_reprocess(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...ingestion.reprocess import reprocess_source

        limit_raw = payload.get("limit")
        limit = int(limit_raw) if limit_raw is not None and str(limit_raw).strip() != "" else None
        result = await reprocess_source(
            source_id=str(payload["source_id"]),
            dataset_id=str(payload["dataset_id"]),
            from_stage=payload.get("from_stage") or "raw",
            sync_batch_id=payload.get("sync_batch_id"),
            force=bool(payload.get("force", False)),
            limit=limit,
            run_enrichment=bool(payload.get("run_enrichment", True)),
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except KeyError as exc:
        return {"id": req_id, "status": "error", "error": f"Missing field: {exc}"}
    except ValueError as exc:
        return {"id": req_id, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_ingestion_audit")
async def handle_get_ingestion_audit(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    sync_batch_id = str(payload.get("sync_batch_id") or "").strip()
    if not sync_batch_id:
        return {"id": req_id, "status": "error", "error": "sync_batch_id required"}
    try:
        from ...pipeline.audit import SQLiteIngestAuditStore

        conn = hub.get_db_connection()
        if not conn:
            return {"id": req_id, "status": "error", "error": "Database connection not available"}
        store = SQLiteIngestAuditStore(conn)
        stages = store.query_by_batch(sync_batch_id)
        return {
            "id": req_id,
            "status": "ok",
            "payload": {"sync_batch_id": sync_batch_id, "stages": stages},
        }
    except Exception as exc:  # noqa: BLE001
        return {"id": req_id, "status": "error", "error": str(exc)}

@handles("get_ingestion_datasets")
async def handle_get_ingestion_datasets(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    logger.debug("[PIPELINE:QUERY] get_ingestion_datasets requested")
    try:
        file_store = RawFileStore()
        datasets = file_store.list_datasets()
        logger.debug(
            "[PIPELINE:QUERY] get_ingestion_datasets returned %d datasets",
            len(datasets),
        )
        return {"id": req_id, "status": "ok", "payload": {"datasets": datasets}}
    except Exception as exc:  # noqa: BLE001
        logger.debug("[PIPELINE:QUERY] get_ingestion_datasets error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}
