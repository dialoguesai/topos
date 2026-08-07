"""Background worker for durable pipeline jobs."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

from .job_store import (
    claim_matching_queued_jobs,
    claim_next_job,
    complete_job,
    fail_job,
    recover_stale_jobs,
    record_derivation_completion,
    update_job_progress,
)

logger = logging.getLogger("topos.pipeline.job_runner")

_worker_task: Optional[asyncio.Task] = None
_worker_lock = threading.Lock()
_lease_owner = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"

ExecutorFn = Callable[[Dict[str, Any]], Awaitable[Dict[str, Any]]]


def _enabled() -> bool:
    return os.environ.get("TOPOS_PIPELINE_WORKER", "on").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )


async def _execute_inbox_deferred_enrichment(payload: Dict[str, Any]) -> Dict[str, Any]:
    from ..ingestion.ingest_helpers import run_inbox_deferred_enrichment

    return await run_inbox_deferred_enrichment(payload)


async def _execute_file_ingestion(payload: Dict[str, Any]) -> Dict[str, Any]:
    import base64

    from ..ingestion.ingest_helpers import ingest_file_payload

    progress_api_url = payload.get("progress_api_url")
    progress_api_key = payload.get("progress_api_key")
    job_id = payload.get("job_id")
    dataset_id = str(payload.get("dataset_id") or "")
    owner_user_id = payload.get("owner_user_id")

    file_bytes = payload.get("file_bytes")
    if not file_bytes and payload.get("file_base64"):
        file_bytes = base64.b64decode(str(payload["file_base64"]))
    if not file_bytes and payload.get("file_url"):
        from ..core.handlers.ingest import _download_ingestion_payload

        file_bytes = await _download_ingestion_payload(str(payload["file_url"]))

    if file_bytes is not None:
        result = await ingest_file_payload(
            dataset_id=dataset_id,
            schema_id=str(payload.get("schema_id") or ""),
            file_bytes=file_bytes,
            file_format=str(payload.get("file_format") or "jsonl"),
            job_id=job_id,
            source_id=payload.get("source_id"),
            source_definition=payload.get("source_definition"),
            progress_api_url=progress_api_url,
            progress_api_key=progress_api_key,
        )
    else:
        result = await ingest_file_payload(
            dataset_id=dataset_id,
            schema_id=str(payload.get("schema_id") or ""),
            file_path=payload.get("file_path"),
            file_format=str(payload.get("file_format") or "jsonl"),
            job_id=job_id,
            source_id=payload.get("source_id"),
            source_definition=payload.get("source_definition"),
            progress_api_url=progress_api_url,
            progress_api_key=progress_api_key,
        )

    if progress_api_url and progress_api_key:
        try:
            import httpx

            status = "completed" if str(result.get("status") or "ok") == "ok" else "failed"
            async with httpx.AsyncClient(timeout=10.0) as client:
                await client.post(
                    f"{progress_api_url}/v1/ingestion/progress",
                    json={
                        "job_id": job_id,
                        "user_id": owner_user_id,
                        "dataset_id": dataset_id,
                        "status": status,
                        "progress_percent": 100.0 if status == "completed" else 0.0,
                        "records_processed": result.get("records_processed", 0),
                        "records_total": result.get("records_total"),
                        "error_message": result.get("error"),
                    },
                    headers={"Authorization": f"Bearer {progress_api_key}"},
                )
        except Exception as exc:
            logger.debug("file ingestion progress post failed: %s", exc)
    return result


async def _execute_enrichment_process_source(payload: Dict[str, Any]) -> Dict[str, Any]:
    from ..api.enrichment import _process_enrichment_core

    return await _process_enrichment_core(
        source_id=str(payload.get("source_id") or ""),
        dataset_id=payload.get("dataset_id"),
        job_names=payload.get("job_names"),
        force_reprocess=bool(payload.get("force_reprocess")),
        include_signal=True,
        progress_updater=payload.get("_progress_updater"),
    )


async def _execute_topic_consolidation(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Full topic-cluster recompute, deferred out of the ingest path.

    Queued by ``topic_clusters_job`` when consolidation comes due. Running it
    inline cost 44s inside an 88-record batch while holding the write gate; here
    it runs alone, and only after the batch that triggered it has finished.
    """
    from ..core.state import get_db_connection
    from ..enrichment.pipeline_activity import is_derivation_in_flight
    from ..features.signal.topic_clustering import (
        _resolved_topic_cluster_source_ids,
        recompute_topic_clusters,
    )

    if is_derivation_in_flight():
        # Re-queued rather than run: a consolidation during a batch is exactly
        # what this executor exists to avoid.
        return {"status": "error", "error": "derivation in flight; will retry"}

    def _run() -> Dict[str, Any]:
        # Fetched INSIDE the worker thread: get_db_connection is thread-local,
        # and handing the loop thread's connection across threads is the
        # sharing that caused the 2026-07-30 transaction corruption.
        own = get_db_connection()
        if own is None:
            return {"status": "error", "error": "no database connection"}
        return recompute_topic_clusters(
            own,
            source_ids=list(_resolved_topic_cluster_source_ids()),
            sync_batch_id=None,
            min_records=3,
        )

    result = await asyncio.to_thread(_run)
    if str(result.get("status") or "") == "error":
        return result
    logger.info("topic consolidation complete: %s", result)
    return {"status": "ok", "result": result}


EXECUTORS: Dict[str, ExecutorFn] = {
    "inbox_deferred_enrichment": _execute_inbox_deferred_enrichment,
    "file_ingestion": _execute_file_ingestion,
    "enrichment_process_source": _execute_enrichment_process_source,
    "topic_consolidation": _execute_topic_consolidation,
}


#: Upper bound on inbox jobs merged into one derive batch. A CP backlog after
#: downtime arrives as one job per delivery (usually one record each); merging
#: them runs the per-batch jobs (dimension briefs, topic work) once instead of
#: once per delivery. Leftovers beyond the cap stay queued for the next cycle.
_COALESCE_MAX_JOBS = 100


def _coalesce_inbox_jobs(
    conn, job: Dict[str, Any], payload: Dict[str, Any]
) -> tuple[list[Dict[str, Any]], Dict[str, Any]]:
    """Merge queued inbox jobs for the same source into this job's batch."""
    siblings = claim_matching_queued_jobs(
        conn,
        lease_owner=_lease_owner,
        kind=str(job["kind"]),
        source_id=job.get("source_id"),
        limit=_COALESCE_MAX_JOBS,
    )
    if not siblings:
        return [job], payload
    merged_records = list(payload.get("canonical_records") or [])
    for sibling in siblings:
        merged_records.extend((sibling.get("payload") or {}).get("canonical_records") or [])
    merged_payload = {**payload, "canonical_records": merged_records}
    logger.info(
        "coalesced %d inbox jobs into one derive batch: source=%s records=%d",
        len(siblings) + 1,
        job.get("source_id"),
        len(merged_records),
    )
    return [job, *siblings], merged_payload


async def process_job(conn, job: Dict[str, Any]) -> None:
    """Run one claimed job (plus any coalesced siblings) to completion."""
    job_id = str(job["job_id"])
    kind = str(job["kind"])

    executor = EXECUTORS.get(kind)
    if executor is None:
        fail_job(conn, job_id, error=f"Unknown job kind: {kind}")
        return

    def _progress_updater(progress: Dict[str, Any]) -> None:
        update_job_progress(conn, job_id, progress)

    payload = dict(job.get("payload") or {})
    jobs = [job]
    if kind == "inbox_deferred_enrichment":
        jobs, payload = _coalesce_inbox_jobs(conn, job, payload)
    if kind == "enrichment_process_source":
        payload["_progress_updater"] = _progress_updater

    try:
        result = await executor(payload)
        if str(result.get("status") or "ok") == "error":
            error = str(result.get("error") or result.get("message") or "failed")
            for entry in jobs:
                fail_job(conn, str(entry["job_id"]), error=error)
                update_job_progress(conn, str(entry["job_id"]), {"status": "failed", "result": result})
            return
        for entry in jobs:
            entry_id = str(entry["job_id"])
            complete_job(conn, entry_id, detail=result)
            update_job_progress(
                conn,
                entry_id,
                {
                    "status": "completed",
                    "messages_processed": result.get("messages_processed", 0),
                    "records_created": result.get("records_created", {}),
                    "errors": result.get("errors", []),
                },
            )
            if kind == "inbox_deferred_enrichment" and entry.get("write_id"):
                record_derivation_completion(
                    conn,
                    write_id=str(entry["write_id"]),
                    job_id=entry_id,
                    source_id=entry.get("source_id"),
                    sync_batch_id=entry.get("sync_batch_id"),
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("pipeline job failed job_id=%s kind=%s: %s", job_id, kind, exc, exc_info=exc)
        for entry in jobs:
            fail_job(conn, str(entry["job_id"]), error=str(exc))
            update_job_progress(conn, str(entry["job_id"]), {"status": "failed", "error": str(exc)})


#: Idle poll interval. Every tick claims against SQLite, so this is also how
#: often the worker competes for the write gate.
_IDLE_POLL_SECONDS = 0.25
#: Backoff ceiling once the queue has been empty for a while. An idle node has
#: no reason to hit the database four times a second.
_MAX_POLL_SECONDS = 5.0


async def _worker_loop(conn_factory: Callable[[], Any]) -> None:
    idle_delay = _IDLE_POLL_SECONDS
    while True:
        if not _enabled():
            await asyncio.sleep(1.0)
            continue
        conn = conn_factory()
        if conn is None:
            await asyncio.sleep(1.0)
            continue

        # claim_next_job takes the process-wide write gate — a BLOCKING
        # threading lock. Called directly it ran on the event loop, so every
        # 250ms the loop could stall behind whatever writer held the gate (a
        # batch write, or a 77s graph rebuild), taking the control-plane
        # keepalive down with it.
        #
        # The factory is re-invoked INSIDE the worker thread rather than
        # capturing the connection above. get_db_connection is thread-local, so
        # this hands the thread its own handle; passing the loop thread's
        # connection across would silently reinstate the cross-thread sharing
        # that caused the 2026-07-30 transaction corruption in the first place.
        def _claim() -> Optional[Dict[str, Any]]:
            own = conn_factory()
            if own is None:
                return None
            return claim_next_job(own, lease_owner=_lease_owner)

        job = await asyncio.to_thread(_claim)
        if job is None:
            await asyncio.sleep(idle_delay)
            # Ease off while nothing is queued; snap back the moment work lands.
            idle_delay = min(idle_delay * 1.5, _MAX_POLL_SECONDS)
            continue
        idle_delay = _IDLE_POLL_SECONDS
        try:
            await process_job(conn, job)
        except Exception as exc:  # noqa: BLE001
            logger.warning("pipeline worker loop error: %s", exc, exc_info=exc)
            try:
                fail_job(conn, str(job["job_id"]), error=str(exc))
            except Exception:
                pass


def start_pipeline_worker(conn_factory: Callable[[], Any]) -> None:
    """Start the async pipeline worker loop once per process."""
    global _worker_task
    if not _enabled():
        return
    with _worker_lock:
        if _worker_task is not None and not _worker_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        _worker_task = loop.create_task(_worker_loop(conn_factory))


def recover_pipeline_jobs(conn) -> int:
    if conn is None:
        return 0
    return recover_stale_jobs(conn)


async def process_pending_jobs_once(conn_factory: Callable[[], Any], *, limit: int = 10) -> int:
    """Process up to ``limit`` queued jobs synchronously (for tests and repair tools)."""
    processed = 0
    conn = conn_factory()
    if conn is None:
        return 0
    for _ in range(max(1, int(limit))):
        job = claim_next_job(conn, lease_owner=_lease_owner)
        if job is None:
            break
        await process_job(conn, job)
        processed += 1
    return processed
