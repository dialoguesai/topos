"""Source enrichment message handlers."""
from __future__ import annotations

import asyncio

import topos.core.handlers as hub

from .common import (
    Any,
    Dict,
    Optional,
    REGISTRY,
    logger,
    uuid,
)
from .registry import handles


def _progress_dict(job: Dict[str, Any]) -> Dict[str, Any]:
    progress = dict(job.get("progress") or {})
    status = str(job.get("status") or progress.get("status") or "processing")
    if status == "done":
        status = "completed"
    elif status == "queued":
        status = "processing"
    return {
        "job_id": job.get("job_id"),
        "status": status,
        "progress_percent": progress.get("progress_percent", 100.0 if status == "completed" else 0.0),
        "messages_processed": progress.get("messages_processed", 0),
        "messages_skipped": progress.get("messages_skipped", 0),
        "messages_total": progress.get("messages_total", 0),
        "records_created": progress.get("records_created", {}),
        "errors": progress.get("errors", []),
        "jobs_complete": progress.get("jobs_complete", 0),
        "jobs_total": progress.get("jobs_total", 0),
        "jobs_progress_percent": progress.get("jobs_progress_percent", 0.0),
        "current_job_name": progress.get("current_job_name"),
        "current_job_progress_percent": progress.get("current_job_progress_percent", 0.0),
    }


@handles("enrichment_process_source")
async def handle_enrichment_process_source(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None

    payload = message.get("payload") or {}
    source_id = payload.get("source_id")
    dataset_id = payload.get("dataset_id")
    job_names = payload.get("job_names")
    force_reprocess = payload.get("force_reprocess", False)

    logger.debug(
        "[PIPELINE:ENRICHMENT] enrichment_process_source received: source_id=%s, dataset_id=%s, job_names=%s, force_reprocess=%s",
        source_id,
        dataset_id,
        job_names,
        force_reprocess,
    )

    if not source_id:
        return {"id": req_id, "status": "error", "error": "source_id required"}

    try:
        from ...enrichment.source_overrides import effective_canonical_enrichment_jobs
        from ...pipeline.job_runner import start_pipeline_worker
        from ...pipeline.job_store import enqueue_job, update_job_progress

        source_def = REGISTRY.get(source_id)
        if not source_def:
            return {"id": req_id, "status": "error", "error": f"Source {source_id} not found"}

        jobs_to_run = job_names or effective_canonical_enrichment_jobs(source_def)
        if not jobs_to_run:
            return {
                "id": req_id,
                "status": "ok",
                "payload": {
                    "status": "ok",
                    "message": "No enrichment jobs configured for this source",
                    "messages_processed": 0,
                    "records_created": {},
                },
            }

        db_conn = hub.get_db_connection()
        if not db_conn:
            return {"id": req_id, "status": "error", "error": "Database connection not available"}

        job_id = str(uuid.uuid4())
        job_payload = {
            "source_id": source_id,
            "dataset_id": dataset_id,
            "job_names": jobs_to_run,
            "force_reprocess": force_reprocess,
        }
        def _enqueue_and_stamp() -> None:
            # Both calls take the write gate — a blocking OS lock — so they
            # must not run on the event-loop thread (2026-08-07 freeze sites).
            own = hub.get_db_connection()
            if own is None:
                return
            enqueue_job(
                own,
                kind="enrichment_process_source",
                payload=job_payload,
                job_id=job_id,
                source_id=source_id,
                idempotency_key=f"enrichment_process_source:{job_id}",
            )
            update_job_progress(
                own,
                job_id,
                {
                    "status": "processing",
                    "progress_percent": 0.0,
                    "messages_processed": 0,
                    "messages_skipped": 0,
                    "messages_total": 0,
                    "jobs_total": len(jobs_to_run),
                },
            )

        await asyncio.to_thread(_enqueue_and_stamp)
        start_pipeline_worker(hub.get_db_connection)

        return {
            "id": req_id,
            "status": "ok",
            "payload": {
                "job_id": job_id,
                "status": "processing",
                "source_id": source_id,
                "messages_total": 0,
                "message": "Processing started. Use enrichment_progress to track progress.",
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] enrichment_process_source error: %s", exc, exc_info=True)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("enrichment_progress")
async def handle_enrichment_progress(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    job_id = payload.get("job_id")

    if not job_id:
        return {"id": req_id, "status": "error", "error": "Missing job_id"}

    try:
        from ...pipeline.job_store import get_job

        db_conn = hub.get_db_connection()
        if not db_conn:
            return {"id": req_id, "status": "error", "error": "Database connection not available"}
        job = get_job(db_conn, str(job_id))
        if not job:
            return {"id": req_id, "status": "error", "error": f"Job {job_id} not found"}
        return {"id": req_id, "status": "ok", "payload": _progress_dict(job)}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] enrichment_progress error: %s", exc, exc_info=True)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("enrichment_status_source")
async def handle_enrichment_status_source(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = payload.get("source_id")
    dataset_id = payload.get("dataset_id")

    if not source_id:
        return {"id": req_id, "status": "error", "error": "source_id required"}

    try:
        from ...api.enrichment import _get_enrichment_status_core

        result = await _get_enrichment_status_core(
            source_id=source_id,
            dataset_id=dataset_id,
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] enrichment_status_source error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("source_enrichments_list")
async def handle_source_enrichments_list(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = payload.get("source_id")
    if not source_id:
        return {"id": req_id, "status": "error", "error": "source_id required"}
    try:
        from ...api.enrichment import list_source_enrichments

        result = await list_source_enrichments(source_id=source_id)
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] source_enrichments_list error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("source_enrichment_backfill")
async def handle_source_enrichment_backfill(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = payload.get("source_id")
    enrichment_name = payload.get("enrichment_name")
    only_missing = payload.get("only_missing", True)
    limit = payload.get("limit")
    if not source_id:
        return {"id": req_id, "status": "error", "error": "source_id required"}
    if not enrichment_name:
        return {"id": req_id, "status": "error", "error": "enrichment_name required"}
    try:
        from ...api.enrichment import backfill_source_enrichment

        result = await backfill_source_enrichment(
            source_id=source_id,
            enrichment_name=enrichment_name,
            only_missing=bool(only_missing),
            limit=limit,
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] source_enrichment_backfill error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("enrichment_catalog")
async def handle_enrichment_catalog(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    try:
        from ...api.enrichment import _enrichment_catalog_core

        result = _enrichment_catalog_core(source_id=payload.get("source_id"))
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] enrichment_catalog error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("enrichment_preview")
async def handle_enrichment_preview(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = payload.get("source_id")
    if not source_id:
        return {"id": req_id, "status": "error", "error": "source_id required"}
    try:
        from ...api.enrichment import _enrichment_preview_core

        result = _enrichment_preview_core(
            source_id=source_id, limit=int(payload.get("limit") or 20)
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] enrichment_preview error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("enrichment_coverage")
async def handle_enrichment_coverage(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = payload.get("source_id")
    if not source_id:
        return {"id": req_id, "status": "error", "error": "source_id required"}
    try:
        from ...api.enrichment import _enrichment_coverage_core

        result = _enrichment_coverage_core(source_id=source_id)
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] enrichment_coverage error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("source_enrichment_delete")
async def handle_source_enrichment_delete(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = payload.get("source_id")
    enrichment_name = payload.get("enrichment_name")
    if not source_id:
        return {"id": req_id, "status": "error", "error": "source_id required"}
    if not enrichment_name:
        return {"id": req_id, "status": "error", "error": "enrichment_name required"}
    try:
        from ...api.enrichment import _delete_enrichment_data_core

        result = _delete_enrichment_data_core(source_id=source_id, job_name=enrichment_name)
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] source_enrichment_delete error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("source_enrichment_toggle")
async def handle_source_enrichment_toggle(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = payload.get("source_id")
    enrichment_name = payload.get("enrichment_name")
    if not source_id:
        return {"id": req_id, "status": "error", "error": "source_id required"}
    if not enrichment_name:
        return {"id": req_id, "status": "error", "error": "enrichment_name required"}
    enabled = payload.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        return {"id": req_id, "status": "error", "error": "'enabled' must be true, false, or null"}
    try:
        from ...api.enrichment import _toggle_source_enrichment_core

        result = _toggle_source_enrichment_core(source_id, enrichment_name, enabled)
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] source_enrichment_toggle error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}


@handles("source_enrichment_test")
async def handle_source_enrichment_test(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    req_id = message.get("id")
    if not req_id:
        return None
    payload = message.get("payload") or {}
    source_id = payload.get("source_id")
    enrichment_name = payload.get("enrichment_name")
    data_packet = payload.get("data_packet") or {}
    if not source_id:
        return {"id": req_id, "status": "error", "error": "source_id required"}
    if not enrichment_name:
        return {"id": req_id, "status": "error", "error": "enrichment_name required"}
    try:
        from ...api.enrichment import test_source_enrichment

        result = await test_source_enrichment(
            source_id=source_id,
            enrichment_name=enrichment_name,
            data_packet=data_packet,
        )
        return {"id": req_id, "status": "ok", "payload": result}
    except Exception as exc:  # noqa: BLE001
        logger.error("[PIPELINE:ENRICHMENT] source_enrichment_test error: %s", exc)
        return {"id": req_id, "status": "error", "error": str(exc)}

