"""Background worker for durable pipeline jobs."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..storage.db.write_gate import is_busy_error
# Safe at import time: derivation_recovery has no module-level heavy imports.
from ..enrichment.derivation_recovery import SIGNAL_DERIVE_RETRY_KIND
from .job_store import (
    claim_matching_queued_jobs,
    claim_next_job,
    complete_job,
    fail_job,
    recover_stale_jobs,
    record_derivation_completion,
    requeue_job,
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

    ingest_options = payload.get("ingest_options")
    ingest_options = ingest_options if isinstance(ingest_options, dict) else None

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
            ingest_options=ingest_options,
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
            ingest_options=ingest_options,
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


async def _execute_signal_derive_retry(payload: Dict[str, Any]) -> Dict[str, Any]:
    from ..enrichment.derivation_recovery import run_derivation_retry_job

    return await run_derivation_retry_job(payload)


EXECUTORS: Dict[str, ExecutorFn] = {
    "inbox_deferred_enrichment": _execute_inbox_deferred_enrichment,
    "file_ingestion": _execute_file_ingestion,
    "enrichment_process_source": _execute_enrichment_process_source,
    "topic_consolidation": _execute_topic_consolidation,
    SIGNAL_DERIVE_RETRY_KIND: _execute_signal_derive_retry,
}


def _executable_kinds() -> list[str]:
    """Kinds this worker may claim. Computed per claim so tests that patch
    EXECUTORS are honored. A queued row of any other kind (e.g. written by a
    newer node version) stays queued and visible instead of being claimed and
    immediately failed as unknown."""
    return sorted(EXECUTORS)


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


async def _run_db(conn_factory: Callable[[], Any], fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one job-store write on a worker thread with that thread's own connection.

    Every job-store helper takes the process-wide write gate — a blocking OS
    lock. Run on the event loop, that stalls every coroutine behind whatever
    writer currently holds it (on 2026-08-07: a graph rebuild holding it 156s,
    with the control-plane keepalive in the blast radius). The factory is
    invoked INSIDE the thread so the loop thread's connection never crosses
    threads — handing it across is the sharing that caused the 2026-07-30
    transaction corruption.
    """

    def _call() -> Any:
        own = conn_factory()
        if own is None:
            raise RuntimeError("no database connection for pipeline job store")
        return fn(own, *args, **kwargs)

    return await asyncio.to_thread(_call)


async def report_terminal_failure(
    payload: Dict[str, Any], job_ids: List[str], error: str
) -> None:
    """Tell the control plane a job died. Best-effort, never raises.

    Without this a crash is written only to the node's own database, so the last
    thing the control plane ever hears is the ``processing / 0%`` the run posts
    before it starts. The job then reads as *working* forever — a failed import
    and a slow one look identical, which is the worst state a progress display
    can be in, because there is nothing the user can do to tell them apart.

    Uses the same channel and credentials the progress updates already use, so
    a job that could report progress can always report its own death.
    """
    url = str(payload.get("progress_api_url") or "").strip()
    key = str(payload.get("progress_api_key") or "").strip()
    if not url or not key or not job_ids:
        return
    try:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            for job_id in job_ids:
                await client.post(
                    f"{url.rstrip('/')}/v1/ingestion/progress",
                    json={
                        "job_id": str(job_id),
                        "dataset_id": str(payload.get("dataset_id") or ""),
                        "status": "failed",
                        "current_step": "failed",
                        "error_message": error[:2000],
                    },
                    headers={"Authorization": f"Bearer {key}"},
                )
    except Exception as exc:  # noqa: BLE001 — reporting must never mask the failure
        logger.warning("could not report job failure upstream job_ids=%s: %s", job_ids, exc)


async def process_job(conn_factory: Callable[[], Any], job: Dict[str, Any]) -> None:
    """Run one claimed job (plus any coalesced siblings) to completion."""
    job_id = str(job["job_id"])
    kind = str(job["kind"])

    executor = EXECUTORS.get(kind)
    if executor is None:
        await _run_db(conn_factory, fail_job, job_id, error=f"Unknown job kind: {kind}")
        return

    def _progress_updater(progress: Dict[str, Any]) -> None:
        def _write() -> None:
            try:
                own = conn_factory()
                if own is not None:
                    update_job_progress(own, job_id, progress)
            except Exception as exc:  # noqa: BLE001 — progress is best-effort
                logger.debug("job progress write failed job_id=%s: %s", job_id, exc)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _write()
        else:
            # Called from async executor code: never take the write gate on
            # the event-loop thread.
            loop.run_in_executor(None, _write)

    payload = dict(job.get("payload") or {})
    jobs = [job]
    if kind == "inbox_deferred_enrichment":
        jobs, payload = await _run_db(conn_factory, _coalesce_inbox_jobs, job, payload)
    if kind == "enrichment_process_source":
        payload["_progress_updater"] = _progress_updater

    try:
        result = await executor(payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pipeline job failed job_id=%s kind=%s: %s", job_id, kind, exc, exc_info=exc)

        def _mark_crashed(own: Any, error: str = str(exc)) -> None:
            for entry in jobs:
                fail_job(own, str(entry["job_id"]), error=error)
                update_job_progress(own, str(entry["job_id"]), {"status": "failed", "error": error})

        await _run_db(conn_factory, _mark_crashed)
        # The local write above is invisible to the person watching. Tell the
        # control plane too, or the job reads as "processing" until someone
        # reads a log file.
        await report_terminal_failure(payload, [str(e["job_id"]) for e in jobs], str(exc))
        return

    if str(result.get("status") or "ok") == "requeue":
        # The executor declined to run right now (e.g. a derivation batch holds
        # the write gate). Hand the claim back untouched — this is a deferral,
        # not an attempt, so the row must not read as failed and must not burn
        # a retry.
        def _requeue(own: Any) -> None:
            for entry in jobs:
                requeue_job(own, str(entry["job_id"]))

        await _run_db(conn_factory, _requeue)
        return

    if str(result.get("status") or "ok") == "error":
        error = str(result.get("error") or result.get("message") or "failed")

        def _mark_failed(own: Any) -> None:
            for entry in jobs:
                fail_job(own, str(entry["job_id"]), error=error)
                update_job_progress(own, str(entry["job_id"]), {"status": "failed", "result": result})

        await _run_db(conn_factory, _mark_failed)
        return

    def _mark_done(own: Any) -> None:
        for entry in jobs:
            entry_id = str(entry["job_id"])
            complete_job(own, entry_id, detail=result)
            update_job_progress(
                own,
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
                    own,
                    write_id=str(entry["write_id"]),
                    job_id=entry_id,
                    source_id=entry.get("source_id"),
                    sync_batch_id=entry.get("sync_batch_id"),
                )

    await _run_db(conn_factory, _mark_done)


#: Idle poll interval. Every tick claims against SQLite, so this is also how
#: often the worker competes for the write gate.
_IDLE_POLL_SECONDS = 0.25
#: Backoff ceiling once the queue has been empty for a while. An idle node has
#: no reason to hit the database four times a second.
_MAX_POLL_SECONDS = 5.0

#: How often to check whether a provider that was blocking parked derivation
#: debts has come back. Deliberately slow: the check is a cached reachability
#: probe and touches the database ONLY on the not-ready → ready edge, so this
#: is the latency of noticing a newly installed model, not a polling cost.
_DEBT_SWEEP_SECONDS = 300.0


async def _maybe_revive_blocked_debts(conn_factory: Callable[[], Any]) -> None:
    """Give parked derivation debts a fresh attempt when their model arrives.

    ``run_derivation_retry_job`` refuses to re-run a debt whose provider is
    absent, so those rows sit 'failed' and nothing else in the queue moves them
    out. Without this the work resumed only when a human hit
    ``POST /signal/derivation-debt/retry``.
    """
    from ..enrichment.derivation_recovery import revive_capability_blocked_debts

    try:
        await _run_db(conn_factory, revive_capability_blocked_debts)
    except Exception as exc:  # noqa: BLE001 — a sweep must never kill the worker
        logger.debug("pipeline debt sweep skipped: %s", exc)


async def _worker_loop(conn_factory: Callable[[], Any]) -> None:
    idle_delay = _IDLE_POLL_SECONDS
    next_debt_sweep = 0.0
    while True:
        try:
            if not _enabled():
                await asyncio.sleep(1.0)
                continue

            now = time.monotonic()
            if now >= next_debt_sweep:
                next_debt_sweep = now + _DEBT_SWEEP_SECONDS
                await _maybe_revive_blocked_debts(conn_factory)

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
                return claim_next_job(own, lease_owner=_lease_owner, kinds=_executable_kinds())

            job = await asyncio.to_thread(_claim)
            if job is None:
                await asyncio.sleep(idle_delay)
                # Ease off while nothing is queued; snap back the moment work lands.
                idle_delay = min(idle_delay * 1.5, _MAX_POLL_SECONDS)
                continue
            idle_delay = _IDLE_POLL_SECONDS
            try:
                await process_job(conn_factory, job)
            except Exception as exc:  # noqa: BLE001
                logger.warning("pipeline worker loop error: %s", exc, exc_info=exc)
                try:
                    await _run_db(conn_factory, fail_job, str(job["job_id"]), error=str(exc))
                except Exception:  # noqa: BLE001
                    pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # The claim used to sit OUTSIDE any try: on 2026-08-07 a "database
            # is locked" that outlived claim_next_job's bounded busy-retries
            # killed this task with an unretrieved exception, and the queue
            # silently stopped draining. Nothing that happens in one iteration
            # is allowed to end the loop — back off and try again.
            if is_busy_error(exc):
                logger.info("pipeline claim found the database locked; backing off: %s", exc)
            else:
                logger.warning("pipeline worker loop error: %s", exc, exc_info=exc)
            await asyncio.sleep(idle_delay)
            idle_delay = min(idle_delay * 1.5, _MAX_POLL_SECONDS)


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


async def stop_pipeline_worker() -> None:
    """Cancel the worker loop started by :func:`start_pipeline_worker`.

    Without this the task outlived its app. ``start_pipeline_worker`` skips when
    ``_worker_task`` is not ``done()``, and a task left pending on a closed loop
    never becomes done — so the FIRST app instance in a process owned the worker
    forever and every later one silently ran without one. Clearing the global is
    the part that matters; the cancel just stops a live loop from writing during
    the next app's startup.
    """
    global _worker_task
    with _worker_lock:
        task = _worker_task
        _worker_task = None
    if task is None or task.done():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        return
    if task.get_loop() is not running:
        # Belongs to an earlier app instance's loop, which is already closed:
        # awaiting it here raises "attached to a different loop". Dropping the
        # reference above is both all we can do and all that is needed — a
        # closed loop is not running it.
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 — teardown never raises
        pass


def recover_pipeline_jobs(conn) -> int:
    if conn is None:
        return 0
    return recover_stale_jobs(conn)


async def process_pending_jobs_once(conn_factory: Callable[[], Any], *, limit: int = 10) -> int:
    """Process up to ``limit`` queued jobs synchronously (for tests and repair tools)."""
    processed = 0
    if conn_factory() is None:
        return 0

    def _claim() -> Optional[Dict[str, Any]]:
        own = conn_factory()
        if own is None:
            return None
        return claim_next_job(own, lease_owner=_lease_owner, kinds=_executable_kinds())

    for _ in range(max(1, int(limit))):
        job = await asyncio.to_thread(_claim)
        if job is None:
            break
        await process_job(conn_factory, job)
        processed += 1
    return processed
