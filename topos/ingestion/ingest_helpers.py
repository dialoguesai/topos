from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from .log_preview import field_preview
from .manager import IngestionManager
from .triggers.file_trigger import FileTrigger
from ..storage.raw.file_store import RawFileStore

logger = logging.getLogger("topos.ingestion.ingest_helpers")


async def _run_browser_url_classification_enrichment(
    *,
    db_conn,
    source_id: str,
    source,
    normalized_payload: dict,
) -> None:
    """Run URL classification enrichment for browser plugin sources."""
    configured_jobs = list(getattr(source, "raw_enrichment_jobs", []) or [])
    if "url_classification" not in configured_jobs:
        logger.debug(
            "[PIPELINE:ENRICHMENT] Browser URL classification skipped: source=%s configured_raw_jobs=%s",
            source_id,
            configured_jobs,
        )
        return

    url = normalized_payload.get("url")
    if not isinstance(url, str) or not url.strip():
        logger.debug(
            "[PIPELINE:ENRICHMENT] Browser URL classification skipped: source=%s missing url",
            source_id,
        )
        return

    title = normalized_payload.get("title")
    record_id = normalized_payload.get("record_id") or ""
    dataset_id = normalized_payload.get("dataset_id")

    try:
        from ..engine import Engine, build_url_classification_task
        from ..storage.raw.browser_flat_tables import write_browser_url_classification

        task_id = f"url_cls_{source_id}_{record_id}" if record_id else f"url_cls_{source_id}_{uuid.uuid4().hex[:12]}"
        task = build_url_classification_task(
            task_id=task_id,
            url=url,
            title=title if isinstance(title, str) else None,
            source_id=source_id,
            record_ids=[record_id] if record_id else [],
        )
        engine = Engine()
        result = await asyncio.to_thread(engine.run, task)
        if result.status != "completed" or result.error:
            logger.warning(
                "[PIPELINE:ENRICHMENT] Browser URL classification Engine result failed: %s",
                result.error or result.status,
            )
            return
        out = result.output
        write_browser_url_classification(
            db_conn,
            source_table=source_id,
            record_id=record_id,
            dataset_id=dataset_id,
            url=url,
            title=title if isinstance(title, str) else None,
            category=out.get("category"),
            confidence=out.get("confidence"),
            model_name=out.get("model"),
        )
        logger.debug(
            "[PIPELINE:ENRICHMENT] Browser URL classification stored: source=%s record_id=%s category=%s confidence=%.4f",
            source_id,
            (record_id or "")[:24],
            out.get("category"),
            float(out.get("confidence", 0.0) or 0.0),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "[PIPELINE:DIRECT] Browser URL classification failed (non-fatal): %s",
            e,
        )


async def ingest_file_payload(
    *,
    dataset_id: str,
    schema_id: str,
    file_path: Optional[str] = None,
    file_bytes: Optional[bytes] = None,
    file_format: str = "jsonl",
    job_id: Optional[str] = None,
    source_id: Optional[str] = None,
    source_definition: Optional[Dict[str, Any]] = None,
    progress_api_url: Optional[str] = None,
    progress_api_key: Optional[str] = None,
) -> dict:
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required"}
    if not file_path and file_bytes is None:
        return {"status": "error", "error": "file_path or file_bytes required"}

    if isinstance(source_definition, dict) and source_definition:
        try:
            from ..sources.runtime_install import install_source_definition

            install_source_definition(source_definition)
            source_id = source_id or str(source_definition.get("source_id") or "").strip() or source_id
        except Exception as exc:
            logger.warning("[PIPELINE:RAW] Failed to install runtime source definition: %s", exc)

    # If source_id not provided, find it from schema_id and source_type="file"
    if not source_id:
        from ..sources.registry import REGISTRY
        for source in REGISTRY.values():
            if source.schema_id == schema_id and source.source_type == "file":
                source_id = source.source_id
                logger.debug(
                    "[PIPELINE:RAW] Found source_id=%s for schema_id=%s (file type)",
                    source_id,
                    schema_id,
                )
                break
        if not source_id:
            logger.warning(
                "[PIPELINE:RAW] No file source found for schema_id=%s, proceeding without source_id",
                schema_id,
            )

    file_store = RawFileStore()
    trigger = FileTrigger(file_store=file_store)
    job_id = job_id or str(uuid.uuid4())
    logger.debug(
        "[PIPELINE:RAW] Creating file ingestion job: job_id=%s, dataset_id=%s, schema_id=%s, source_id=%s, file_path=%s, file_size=%s",
        job_id,
        dataset_id,
        schema_id,
        source_id,
        file_path,
        len(file_bytes) if file_bytes else None,
    )
    if file_path:
        job = trigger.create_job(job_id, dataset_id, schema_id, file_path, file_format=file_format)
    else:
        job = trigger.create_job_from_bytes(
            job_id, dataset_id, schema_id, file_bytes or b"", file_format=file_format
        )
    manager = IngestionManager(file_store=file_store)
    result = await manager.process_job(
        job, 
        source_id=source_id,
        progress_api_url=progress_api_url,
        progress_api_key=progress_api_key,
    )
    logger.debug(
        "[PIPELINE:RAW] %s: File ingestion complete: job_id=%s, records_processed=%s, errors=%s",
        manager,
        job_id,
        result.get("records_processed"),
        result.get("errors_count"),
    )
    return {"status": "ok", **result}


async def ingest_ui_payload(
    *,
    dataset_id: str,
    schema_id: str,
    payload: dict,
    job_id: Optional[str] = None,
    source_id: Optional[str] = None,
) -> dict:
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required"}
    if not payload:
        return {"status": "error", "error": "payload required"}

    # If source_id is provided and it's a UI stream source, process directly without creating JSONL
    if source_id:
        from ..sources.registry import REGISTRY
        source = REGISTRY.get(source_id)
        if source and source.source_type == "ui_stream":
            return await _ingest_ui_payload_direct(
                dataset_id=dataset_id,
                schema_id=schema_id,
                payload=payload,
                job_id=job_id,
                source_id=source_id,
            )

    # Legacy path: create JSONL file (for backward compatibility or file-based sources)
    file_store = RawFileStore()
    job_id = job_id or str(uuid.uuid4())
    sender_type = payload.get("sender_type", "human")
    role = "user" if sender_type == "human" else sender_type
    content = payload.get("content", "")
    created_at = payload.get("created_at")
    if created_at is None:
        ts = payload.get("ts")
        if isinstance(ts, (int, float)):
            created_at = ts
        elif isinstance(ts, str):
            created_at = ts
        else:
            created_at = datetime.now(timezone.utc).timestamp()
    record = {
        "id": payload.get("message_id") or job_id,
        "thread_id": payload.get("conversation_id") or dataset_id,
        "role": role,
        "content": content,
        "created_at": created_at,
    }
    logger.debug(
        "[PIPELINE:RAW] Appending UI message to raw store: job_id=%s, dataset_id=%s, message_id=%s, content_preview=%s",
        job_id,
        dataset_id,
        record["id"],
        field_preview(content),
    )
    file_store.append_record(dataset_id, schema_id, record)
    trigger = FileTrigger(file_store=file_store)
    job = trigger.create_job(
        job_id,
        dataset_id,
        schema_id,
        file_store.get_file_path(dataset_id, schema_id).as_posix(),
    )
    logger.debug("[PIPELINE:RAW] Starting ingestion job: job_id=%s, dataset_id=%s, schema_id=%s", job_id, dataset_id, schema_id)
    manager = IngestionManager(file_store=file_store)
    result = await manager.process_job(job)
    logger.debug(
        "[PIPELINE:RAW] %s: UI ingestion complete: job_id=%s, records_processed=%s, errors=%s",
        manager,
        job_id,
        result.get("records_processed"),
        result.get("errors_count"),
    )
    return {"status": "ok", **result}


async def _ingest_ui_payload_direct(
    *,
    dataset_id: str,
    schema_id: str,
    payload: dict,
    job_id: Optional[str] = None,
    source_id: str,
) -> dict:
    """Process UI payload directly to database without creating JSONL files."""
    from .parsers import PARSER_REGISTRY
    from .sources.base import RawRecord
    from ..enrichment.orchestrator import EnrichmentOrchestrator
    from ..enrichment.derived_tables import DerivedTablesManager
    from ..core.state import get_db_connection
    
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required"}
    if not payload:
        return {"status": "error", "error": "payload required"}
    
    job_id = job_id or payload.get("message_id") or str(uuid.uuid4())
    
    # Get source definition
    from ..sources.registry import REGISTRY
    source = REGISTRY.get(source_id)
    if not source:
        return {"status": "error", "error": f"Unknown source_id: {source_id}"}
    
    # Get parser
    parser_cls = PARSER_REGISTRY.get(source.parser_id or schema_id)
    if not parser_cls:
        return {"status": "error", "error": f"No parser found for schema: {schema_id}"}
    
    # Prepare raw record: for browser_* sources pass payload through; for chat use message-style fields
    if source_id.startswith("browser_"):
        raw_payload = dict(payload)
        event_or_url = raw_payload.get("event_type") or raw_payload.get("url") or "browser"
        ts = raw_payload.get("visited_at") or raw_payload.get("starred_at") or raw_payload.get("created_at") or ""
        record_id = f"{event_or_url}_{str(ts)[:24]}_{job_id[:8]}"[:256]
        raw_payload.setdefault("id", record_id)
        raw_payload.setdefault("record_id", record_id)
    else:
        sender_type = payload.get("sender_type", "human")
        role = "user" if sender_type == "human" else sender_type
        content = payload.get("content", "")
        created_at = payload.get("created_at")
        if created_at is None:
            ts = payload.get("ts")
            if isinstance(ts, (int, float)):
                created_at = ts
            elif isinstance(ts, str):
                created_at = ts
            else:
                created_at = datetime.now(timezone.utc).timestamp()
        raw_payload = {
            "id": payload.get("message_id") or job_id,
            "thread_id": payload.get("conversation_id") or dataset_id,
            "role": role,
            "content": content,
            "created_at": created_at,
        }
        record_id = raw_payload["id"]
    raw_record = RawRecord(record_id=record_id, payload=raw_payload)
    
    # Get database connection early (needed for raw storage)
    db_conn = get_db_connection()
    if not db_conn:
        return {"status": "error", "error": "Database connection not available"}
    
    # Write raw record to raw retention table (architecture requirement)
    # This preserves original payload before parsing/canonicalization
    try:
        from ..storage.raw.raw_tables_manager import RawTablesManager
        raw_tables_manager = RawTablesManager(db_conn)
        raw_tables_manager.write_raw_record(
            source_id=source_id,
            source_record_id=record_id,
            payload=raw_payload,
            source_type="chat_messages",
        )
        logger.debug(
            "[PIPELINE:RAW] Stored raw payload: source=%s, record_id=%s",
            source_id,
            record_id[:8] if record_id else None,
        )
    except Exception as e:
        logger.warning(
            "[PIPELINE:RAW] Failed to store raw record (non-fatal): %s",
            e,
        )
        # Non-fatal: continue with parsing even if raw storage fails
    
    # Parse and validate
    # Instantiate parser with schema_id (for v2 support)
    parser = parser_cls(dataset_id=dataset_id, _schema_id=schema_id)
    validation = parser.validate(raw_record)
    if not validation.is_valid:
        logger.error("[PIPELINE:DIRECT] Validation failed: %s", validation.errors)
        return {"status": "error", "error": f"Validation failed: {validation.errors}"}
    
    normalized = parser.parse(raw_record)
    logger.debug(
        "[PIPELINE:DIRECT] Record normalized: message_id=%s, sender_type=%s, content_preview=%s",
        normalized.payload.get("message_id"),
        normalized.payload.get("sender_type"),
        field_preview(normalized.payload.get("content")),
    )
    
    # Browser plugin: raw retention (above) plus flat tables for Data Explorer / SQL.
    if source_id == "browser_visits":
        try:
            from ..storage.raw.browser_flat_tables import write_browser_visit

            write_browser_visit(db_conn, normalized.payload)
        except Exception as e:  # noqa: BLE001
            logger.warning("[PIPELINE:DIRECT] Failed to write browser_visits flat row (non-fatal): %s", e)
    elif source_id == "browser_events":
        try:
            from ..storage.raw.browser_flat_tables import write_browser_event
            write_browser_event(db_conn, normalized.payload)
        except Exception as e:  # noqa: BLE001
            logger.warning("[PIPELINE:DIRECT] Failed to write browser_events flat row (non-fatal): %s", e)

    if source_id.startswith("browser_"):
        await _run_browser_url_classification_enrichment(
            db_conn=db_conn,
            source_id=source_id,
            source=source,
            normalized_payload=normalized.payload,
        )
    
    tables_manager = DerivedTablesManager(conn=db_conn)
    
    # Note: Raw record storage happens before parsing (see above)
    # This ensures raw retention per architecture requirements
    
    # Canonicalize: conversations group -> conversation_messages; else engine ai_chat_*
    canonical_messages_dicts = []
    staging_record = {
        "message_id": normalized.payload.get("message_id"),
        "dataset_id": dataset_id,
        "thread_id": normalized.payload.get("thread_id") or normalized.payload.get("conversation_id") or dataset_id,
        "ts": normalized.payload.get("ts") or normalized.payload.get("created_at") or str(datetime.now(timezone.utc).timestamp()),
        "sender_type": normalized.payload.get("sender_type"),
        "content": normalized.payload.get("content"),
        "source_id": source_id,
    }
    if "_metadata" in normalized.payload:
        staging_record["_metadata"] = normalized.payload["_metadata"]

    if getattr(source, "canonical_group_id", None) == "conversations":
        # Conversations canonical: write only to conversation_messages / conversations (never ai_chat_*)
        try:
            from ..storage.canonical import ConversationsTablesManager
            conv_manager = ConversationsTablesManager(db_conn)
            conv_manager.upsert_message_batch([staging_record], dataset_id, source_id)
            canonical_messages_dicts = [{
                "message_id": staging_record.get("message_id"),
                "conversation_id": staging_record.get("thread_id") or staging_record.get("conversation_id") or dataset_id,
                "sender_type": staging_record.get("sender_type"),
                "sender_id": None,
                "ts": staging_record.get("ts"),
                "content": staging_record.get("content"),
                "source_id": source_id,
            }]
            logger.debug(
                "[PIPELINE:DIRECT] Conversations canonical: message_id=%s",
                staging_record.get("message_id"),
            )
        except Exception as e:
            logger.error(
                "[PIPELINE:DIRECT] Failed to write to conversation_messages: %s",
                e,
                exc_info=True,
            )
    elif source.canonical_mapper_id:
        try:
            from ..storage.canonical.ai_chat import CanonicalTablesManager, Canonicalizer

            canonical_tables_manager = CanonicalTablesManager(db_conn)
            canonicalizer = Canonicalizer(canonical_tables_manager)
            mapper_source = source.canonical_mapper_id
            canonical_result = canonicalizer.canonicalize_staging_batch(
                [staging_record],
                source=mapper_source,
                batch_size=1,
            )
            canonical_messages_dicts = canonical_result.get("canonical_messages", [])
            logger.debug(
                "[PIPELINE:DIRECT] Canonicalized record: message_id=%s, conversations=%d, messages=%d",
                staging_record.get("message_id"),
                canonical_result.get("conversations_created", 0),
                canonical_result.get("messages_created", 0),
            )
            if canonical_result.get("errors"):
                logger.warning(
                    "[PIPELINE:DIRECT] Canonicalization had errors: %s",
                    canonical_result.get("errors"),
                )
        except ImportError as e:
            logger.warning(
                "[PIPELINE:DIRECT] Canonicalization modules not available: %s. Skipping canonicalization.",
                e,
            )
        except Exception as e:
            logger.error(
                "[PIPELINE:DIRECT] Failed to canonicalize record: %s",
                e,
                exc_info=True,
            )
    
    # Run canonical enrichment only for sources that declare canonical jobs.
    enrichment_trigger = getattr(source, "enrichment_trigger", "manual")
    canonical_jobs = list(getattr(source, "canonical_enrichment_jobs", []) or [])
    canonical_message_count = len(canonical_messages_dicts) if canonical_messages_dicts else 0

    if canonical_jobs:
        logger.info(
            "[PIPELINE:DIRECT:ENRICHMENT] source_id=%s, enrichment_trigger=%s, canonical_messages=%d, jobs=%s",
            source_id,
            enrichment_trigger,
            canonical_message_count,
            canonical_jobs,
        )
        if enrichment_trigger == "manual":
            logger.info(
                "[PIPELINE:DIRECT:ENRICHMENT] ✅ SKIPPING enrichment (manual trigger): %d canonical messages will be enriched later via POST /v1/enrichment/process",
                canonical_message_count,
            )
        elif enrichment_trigger == "automatic" and canonical_messages_dicts:
            enrichment_orchestrator = EnrichmentOrchestrator(tables_manager=tables_manager)
            logger.info(
                "[PIPELINE:DIRECT:ENRICHMENT] Running enrichment (automatic trigger): %d messages, jobs=%s",
                canonical_message_count,
                canonical_jobs,
            )
            enrichment_result = await enrichment_orchestrator.run_canonical(
                canonical_messages_dicts,
                job_names=canonical_jobs,
            )
            logger.debug(
                "[PIPELINE:DIRECT] Enrichment complete: jobs_run=%s, records_created=%s",
                enrichment_result.get("jobs_run"),
                enrichment_result.get("records_created"),
            )
    else:
        logger.debug(
            "[PIPELINE:DIRECT:ENRICHMENT] No canonical enrichment jobs configured for source_id=%s; skipping canonical enrichment stage",
            source_id,
        )
    
    return {
        "status": "ok",
        "job_id": job_id,
        "records_processed": 1,
        "errors_count": 0,
    }
