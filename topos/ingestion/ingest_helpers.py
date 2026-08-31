from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .canonical_pipeline import canonicalize_normalized_batch, run_post_canonical_pipeline
from .log_preview import field_preview
from .manager import IngestionManager
from .triggers.file_trigger import FileTrigger
from ..storage.raw.file_store import RawFileStore

logger = logging.getLogger("topos.ingestion.ingest_helpers")


def _ui_stream_passes_payload_through(source: Any, source_id: str) -> bool:
    """True when ui_stream ingest should forward the client payload to the source parser."""
    if source_id.startswith("browser_"):
        return True
    if str(getattr(source, "source_type", None) or "") != "ui_stream":
        return False
    # Chat-style UI streams expect message-shaped payloads from the client.
    chat_parser_ids = frozenset({"chatgpt.conversation.v1", "chatgpt.conversation.v2"})
    parser_id = str(getattr(source, "parser_id", None) or "").strip()
    schema_id = str(getattr(source, "schema_id", None) or "").strip()
    if parser_id in chat_parser_ids or schema_id in chat_parser_ids:
        return False
    return True


def resolve_file_format(
    *,
    source_definition: Optional[Any] = None,
    file_path: Optional[str] = None,
    default: str = "jsonl",
) -> str:
    """Pick parser file format from source file_ingest_shape or filename extension."""
    if source_definition is not None:
        shape = getattr(source_definition, "file_ingest_shape", None)
        if isinstance(shape, dict):
            fmt = str(shape.get("format") or "").strip().lower()
            if fmt:
                return fmt
    raw_path = str(file_path or "").strip()
    path = raw_path.lower()
    if path.endswith(".csv"):
        return "csv"
    if path.endswith(".json"):
        return "json"
    if path.endswith((".jsonl", ".ndjson")):
        return "jsonl"
    # A container is not a format: an archive or an export folder resolves to
    # the conversations.json inside it (see ``ingestion.local_exports``), so the
    # format is that member's, not the container's. Without this the extension
    # matches nothing, the default wins, and a JSON array gets parsed as JSONL —
    # which fails deep in the parser rather than here.
    if path.endswith(".zip"):
        return "json"
    if raw_path:
        try:
            if os.path.isdir(raw_path):
                return "json"
        except OSError:
            pass
    return default


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
    ingest_options: Optional[Dict[str, Any]] = None,
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

    # If source_id not provided, find it from schema_id and delivery=owner_upload
    if not source_id:
        from ..sources.definitions import is_file_delivery
        from ..sources.registry import REGISTRY
        for source in REGISTRY.values():
            if source.schema_id == schema_id and is_file_delivery(source):
                source_id = source.source_id
                logger.info(
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
    logger.info(
        "[PIPELINE:RAW] Creating file ingestion job: job_id=%s, dataset_id=%s, schema_id=%s, source_id=%s, file_path=%s, file_size=%s",
        job_id,
        dataset_id,
        schema_id,
        source_id,
        file_path,
        len(file_bytes) if file_bytes else None,
    )
    if file_path:
        job = trigger.create_job(
            job_id, dataset_id, schema_id, file_path, file_format=file_format, ingest_options=ingest_options
        )
    else:
        job = trigger.create_job_from_bytes(
            job_id,
            dataset_id,
            schema_id,
            file_bytes or b"",
            file_format=file_format,
            ingest_options=ingest_options,
        )
    manager = IngestionManager(file_store=file_store)
    result = await manager.process_job(
        job, 
        source_id=source_id,
        progress_api_url=progress_api_url,
        progress_api_key=progress_api_key,
    )
    logger.info(
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
    defer_enrichment: bool = False,
) -> dict:
    if not dataset_id:
        return {"status": "error", "error": "dataset_id required"}
    if not payload:
        return {"status": "error", "error": "payload required"}

    # If source_id is provided and it's a UI stream source, process directly without creating JSONL
    _LEGACY_CHAT_SOURCE_ID = "chatgpt_ui_conversation"
    if source_id:
        from ..sources.definitions import accepts_app_ingest
        from ..sources.registry import REGISTRY
        source = REGISTRY.get(source_id)
        if source and accepts_app_ingest(source):
            effective_schema = str(source.schema_id or schema_id or "").strip() or schema_id
            return await _ingest_ui_payload_direct(
                dataset_id=dataset_id,
                schema_id=effective_schema,
                payload=payload,
                job_id=job_id,
                source_id=source_id,
                defer_enrichment=defer_enrichment,
            )
        if source_id != _LEGACY_CHAT_SOURCE_ID:
            if not source:
                return {
                    "status": "error",
                    "error": (
                        f"Source '{source_id}' is not installed on this engine. "
                        "Install the source before ingesting."
                    ),
                }
            return {
                "status": "error",
                "error": (
                    f"Source '{source_id}' has source_type={source.source_type!r}; "
                    "app_ingest requires ui_stream sources."
                ),
            }

    # Legacy path: ChatGPT UI only (backward compatibility when source_id omitted or chatgpt default)
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
    logger.info(
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
    logger.info("[PIPELINE:RAW] Starting ingestion job: job_id=%s, dataset_id=%s, schema_id=%s", job_id, dataset_id, schema_id)
    manager = IngestionManager(file_store=file_store)
    result = await manager.process_job(job)
    logger.info(
        "[PIPELINE:RAW] %s: UI ingestion complete: job_id=%s, records_processed=%s, errors=%s",
        manager,
        job_id,
        result.get("records_processed"),
        result.get("errors_count"),
    )
    return {"status": "ok", **result}


async def run_ui_payload_enrichment(enrichment_ctx: dict) -> dict:
    """Run post-canonical enrichment for a deferred inbox ingest."""
    from ..sources.registry import REGISTRY

    source_id = str(enrichment_ctx.get("source_id") or "").strip()
    source = REGISTRY.get(source_id)
    if not source:
        return {"status": "error", "error": f"Unknown source_id: {source_id}"}
    canonical_records = enrichment_ctx.get("canonical_records") or []
    if not canonical_records and enrichment_ctx.get("recover"):
        from ..core.state import get_db_connection
        from ..ingestion.canonical_pipeline import load_canonical_records_for_signal

        db_conn = get_db_connection()
        if db_conn:
            canonical_records = load_canonical_records_for_signal(db_conn, source)
    if not canonical_records:
        return {"status": "ok", "enrichment_jobs_run": 0}

    # No tables_manager passed: run_post_canonical_pipeline builds one on a
    # worker thread (its constructor ensures DDL under the write gate, which
    # must never be taken on the event-loop thread).
    sync_batch_id = str(enrichment_ctx.get("sync_batch_id") or uuid.uuid4())
    pipeline_outcome = await run_post_canonical_pipeline(
        source_def=source,
        canonical_records=canonical_records,
        sync_batch_id=sync_batch_id,
    )
    enrich_out = pipeline_outcome.get("canonical_enrichment") or {}
    signal_out = pipeline_outcome.get("signal_derivation") or {}
    signal_errors = list(signal_out.get("errors") or []) if isinstance(signal_out, dict) else []
    enrich_errors = list(enrich_out.get("errors") or []) if isinstance(enrich_out, dict) else []
    status = "error" if (signal_errors or enrich_errors) else "ok"
    result = {
        "status": status,
        "signal_jobs_run": (signal_out.get("jobs_run") if isinstance(signal_out, dict) else 0) or 0,
        "enrichment_jobs_run": (enrich_out.get("jobs_run") if isinstance(enrich_out, dict) else 0) or 0,
        "errors": signal_errors + enrich_errors,
    }
    if status == "error":
        result["error"] = "; ".join(
            str(e.get("error") if isinstance(e, dict) else e) for e in (signal_errors + enrich_errors)[:5]
        ) or "signal_derivation_failed"
    return result


_inbox_enrichment_sems: dict[int, asyncio.Semaphore] = {}


def _inbox_enrichment_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    key = id(loop)
    sem = _inbox_enrichment_sems.get(key)
    if sem is None:
        sem = asyncio.Semaphore(1)
        _inbox_enrichment_sems[key] = sem
    return sem


async def run_inbox_deferred_enrichment(enrichment_ctx: dict) -> dict:
    """Serialize heavy inbox background enrichment to avoid concurrent model loads."""
    async with _inbox_enrichment_semaphore():
        return await run_ui_payload_enrichment(enrichment_ctx)


async def _ingest_ui_payload_direct(
    *,
    dataset_id: str,
    schema_id: str,
    payload: dict,
    job_id: Optional[str] = None,
    source_id: str,
    defer_enrichment: bool = False,
) -> dict:
    """Process UI payload directly to database without creating JSONL files."""
    from .parsers import PARSER_REGISTRY
    from .sources.base import RawRecord

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
    
    # Get parser (prefer installed source ids, then request schema_id)
    parser_cls = None
    for parser_key in (source.parser_id, source.schema_id, schema_id):
        key = str(parser_key or "").strip()
        if not key:
            continue
        parser_cls = PARSER_REGISTRY.get(key)
        if parser_cls:
            break
    if not parser_cls:
        return {
            "status": "error",
            "error": (
                f"No parser found for source_id={source_id!r} "
                f"(parser_id={source.parser_id!r}, schema_id={schema_id!r}). "
                "Restart the engine or reinstall the source if a bundled parser was removed."
            ),
        }
    
    # Prepare raw record: browser_* and journal/time-log ui_stream sources pass payload through;
    # chat UI streams (e.g. chatgpt_ui_conversation) use message-shaped fields.
    if _ui_stream_passes_payload_through(source, source_id):
        raw_payload = dict(payload)
        if source_id.startswith("browser_"):
            event_or_url = raw_payload.get("event_type") or raw_payload.get("url") or "browser"
            ts = raw_payload.get("visited_at") or raw_payload.get("starred_at") or raw_payload.get("created_at") or ""
            record_id = f"{event_or_url}_{str(ts)[:24]}_{job_id[:8]}"[:256]
            raw_payload.setdefault("id", record_id)
        else:
            record_id = str(
                payload.get("id")
                or payload.get("num")
                or payload.get("message_id")
                or job_id
            )
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

    sync_batch_id = str(job_id)

    # The whole raw→canonical stretch takes the DB write gate — a blocking OS
    # lock — at every stage, so it runs on a worker thread: holding the gate on
    # the event-loop thread stalls every coroutine, including the control-plane
    # keepalive. The worker uses its own thread-local connection.
    result = await asyncio.to_thread(
        _ingest_ui_payload_direct_db,
        source=source,
        source_id=source_id,
        schema_id=schema_id,
        dataset_id=dataset_id,
        parser_cls=parser_cls,
        raw_record=raw_record,
        sync_batch_id=sync_batch_id,
    )
    canonical_result = result.pop("_canonical_result", None)
    if canonical_result is None:
        return result

    if defer_enrichment:
        return {
            "status": "ok",
            "job_id": job_id,
            "records_processed": 1,
            "errors_count": len(canonical_result.errors),
            "canonical_events_created": canonical_result.events_created,
            "canonical_messages_created": canonical_result.messages_created,
            "timeline_rows_written": canonical_result.timeline_rows_written,
            "enrichment_pending": True,
            "_enrichment_ctx": {
                "source_id": source_id,
                "sync_batch_id": sync_batch_id,
                "canonical_records": canonical_result.canonical_records,
            },
        }

    pipeline_outcome = await run_post_canonical_pipeline(
        source_def=source,
        canonical_records=canonical_result.canonical_records,
        sync_batch_id=sync_batch_id,
    )
    signal_out = pipeline_outcome.get("signal_derivation") or {}
    enrich_out = pipeline_outcome.get("canonical_enrichment") or {}

    return {
        "status": "ok",
        "job_id": job_id,
        "records_processed": 1,
        "errors_count": len(canonical_result.errors),
        "canonical_events_created": canonical_result.events_created,
        "canonical_messages_created": canonical_result.messages_created,
        "timeline_rows_written": canonical_result.timeline_rows_written,
        "signal_jobs_run": (signal_out.get("jobs_run") if isinstance(signal_out, dict) else 0) or 0,
        "enrichment_jobs_run": (enrich_out.get("jobs_run") if isinstance(enrich_out, dict) else 0) or 0,
    }


def _ingest_ui_payload_direct_db(
    *,
    source: Any,
    source_id: str,
    schema_id: str,
    dataset_id: str,
    parser_cls: Any,
    raw_record: Any,
    sync_batch_id: str,
) -> Dict[str, Any]:
    """Raw retention → parse → source/flat tables → canonicalization.

    Runs on a worker thread (see the asyncio.to_thread call site) with the
    thread's own connection. On success the returned dict carries the
    CanonicalizeResult under ``_canonical_result``; error dicts match the async
    caller's response contract.
    """
    from ..core.state import get_db_connection

    record_id = raw_record.record_id
    raw_payload = raw_record.payload

    db_conn = get_db_connection()
    if not db_conn:
        return {"status": "error", "error": "Database connection not available"}

    # Write raw record to raw retention table (architecture requirement)
    # This preserves original payload before parsing/canonicalization
    try:
        from ..storage.raw.raw_tables_manager import RawTablesManager
        raw_tables_manager = RawTablesManager(db_conn)
        raw_source_type = (
            "chat_messages"
            if not _ui_stream_passes_payload_through(source, source_id)
            else str(getattr(source, "source_type", None) or "ui_stream")
        )
        raw_tables_manager.write_raw_record(
            source_id=source_id,
            source_record_id=record_id,
            payload=raw_payload,
            source_type=raw_source_type,
        )
        logger.info(
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
    effective_schema_id = str(getattr(source, "schema_id", None) or schema_id or "").strip()
    parser = parser_cls(dataset_id=dataset_id, _schema_id=effective_schema_id)
    validation = parser.validate(raw_record)
    if not validation.is_valid:
        logger.error("[PIPELINE:DIRECT] Validation failed: %s", validation.errors)
        return {"status": "error", "error": f"Validation failed: {validation.errors}"}
    
    normalized = parser.parse(raw_record)
    if _ui_stream_passes_payload_through(source, source_id) and not source_id.startswith("browser_"):
        logger.debug(
            "[PIPELINE:DIRECT] Record normalized: entry_id=%s, content_preview=%s",
            normalized.payload.get("entry_id"),
            field_preview(normalized.payload.get("content")),
        )
    else:
        logger.debug(
            "[PIPELINE:DIRECT] Record normalized: message_id=%s, sender_type=%s, content_preview=%s",
            normalized.payload.get("message_id"),
            normalized.payload.get("sender_type"),
            field_preview(normalized.payload.get("content")),
        )

    try:
        from .manager import _persist_source_data_tables

        _persist_source_data_tables(
            db_conn=db_conn,
            source_def=source,
            dataset_id=dataset_id,
            normalized_records=[normalized],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[PIPELINE:DIRECT] Failed to write source data table row (non-fatal): %s", e)
    
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

    canonical_result = canonicalize_normalized_batch(
        db_conn,
        source,
        [normalized],
        dataset_id=dataset_id,
        sync_batch_id=sync_batch_id,
    )
    if canonical_result.errors:
        logger.warning(
            "[PIPELINE:DIRECT] Canonicalization errors: source_id=%s errors=%s",
            source_id,
            canonical_result.errors,
        )

    return {"status": "ok", "_canonical_result": canonical_result}
