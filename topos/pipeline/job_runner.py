"""Background worker for durable pipeline jobs."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from ..storage.db.write_gate import is_busy_error
# Safe at import time: derivation_recovery has no module-level heavy imports.
from ..enrichment.derivation_recovery import SIGNAL_DERIVE_RETRY_KIND
from .job_store import (
    DEFAULT_LEASE_SECONDS,
    claim_matching_queued_jobs,
    claim_next_job,
    complete_job,
    fail_job,
    recover_stale_jobs,
    record_derivation_completion,
    renew_job_lease,
    requeue_job,
    update_job_progress,
)

logger = logging.getLogger("topos.pipeline.job_runner")

_worker_task: Optional[asyncio.Task] = None
#: Worker for _LONG_RUNNING_KINDS. Separate task, same lock and lifecycle.
_long_worker_task: Optional[asyncio.Task] = None
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


LOCAL_SYNC_KIND = "local_sync"


async def _execute_local_sync(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Run one iMessage/Signal sync to completion, off the request path.

    This is the whole reason the kind exists. ``run_imessage_sync`` drains the
    entire backlog — hours on a first run — and it used to be awaited inside the
    websocket handler, so the control plane hit its 20s engine deadline and
    answered 504 while the sync kept going invisibly. The work always survived;
    the *receipt* never landed, because the coroutine that would have written it
    was gone. Here the executor owns the receipt, so it is written whether or not
    anyone is still listening.

    The body is a plain blocking ``def``, and this coroutine is awaited on the
    event-loop thread (``process_job``), so every part of it runs in
    ``asyncio.to_thread`` with the connection fetched INSIDE the thread — a
    connection handed across threads is the 2026-07-30 transaction corruption.
    """
    source_id = str(payload.get("source_id") or "").strip()
    dataset_id = str(payload.get("dataset_id") or "").strip()
    sync_options = payload.get("sync_options")
    progress_updater = payload.get("_progress_updater")
    job_id = str(payload.get("_job_id") or "")

    def _on_batch(progress: Dict[str, Any]) -> None:
        """Report a batch and prove the job is still alive, in that order.

        The lease is renewed here rather than on a timer because a batch is the
        only moment this job is provably making progress; a heartbeat that ran
        independently of the work would keep claiming liveness for a sync that
        had silently stopped.
        """
        if progress_updater is not None:
            progress_updater(progress)
        if not job_id:
            return
        try:
            from ..core.state import get_db_connection

            own = get_db_connection()
            if own is not None:
                renew_job_lease(own, job_id, lease_seconds=LONG_JOB_LEASE_SECONDS)
        except Exception as exc:  # noqa: BLE001 — liveness is best-effort
            logger.debug("lease renewal failed job_id=%s: %s", job_id, exc)

    if not source_id or not dataset_id:
        return {"status": "error", "error": "source_id and dataset_id required"}
    if source_id not in ("imessage", "signal"):
        return {"status": "error", "error": f"sync not implemented for source_id={source_id}"}

    def _run() -> Dict[str, Any]:
        from ..core.state import get_db_connection
        from ..ingestion.local_sync import run_imessage_sync, run_signal_sync
        from ..storage.source_settings import update_sync_result

        own = get_db_connection()
        if own is None:
            return {"status": "error", "error": "no database connection"}

        if source_id == "imessage":
            result = run_imessage_sync(dataset_id, sync_options=sync_options, progress_cb=_on_batch)
        else:
            result = run_signal_sync(dataset_id, sync_options=sync_options, progress_cb=_on_batch)

        status = str(result.get("status") or "error")
        # The receipt, on this thread, where taking the write gate is legal.
        # Both branches are best-effort: a sync that moved rows must not be
        # reported as failed because its bookkeeping write lost a lock race.
        try:
            if status == "ok":
                update_sync_result(
                    own, dataset_id, source_id,
                    success=True,
                    last_sync_at=datetime.now(timezone.utc).isoformat(),
                )
            else:
                update_sync_result(
                    own, dataset_id, source_id,
                    success=False,
                    last_error=str(result.get("error") or "Sync failed"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("sync receipt write failed source=%s: %s", source_id, exc)

        if status == "ok":
            # The node's own HTTP route has always refreshed messenger analytics
            # after a sync and the websocket handler never did, so the same sync
            # produced different state depending on which door it came through.
            # The executor is now the single door: both get the refresh.
            try:
                from ..analytics.messenger_communities import compute_and_persist_messenger_analytics

                compute_and_persist_messenger_analytics(
                    dataset_id=dataset_id,
                    conn=own,
                    source_ids=[source_id],
                    period_granularity="month",
                )
            except Exception as exc:  # noqa: BLE001 — never fail a good sync on analytics
                logger.warning("messenger analytics refresh failed after sync: %s", exc)

        # _mark_done reads `messages_processed`; the sync functions speak
        # `records_processed`. Alias rather than rename, so the direct callers
        # and the tests that pin the sync's own return shape stay valid.
        if "records_processed" in result and "messages_processed" not in result:
            result = {**result, "messages_processed": result.get("records_processed", 0)}
        return result

    return await asyncio.to_thread(_run)


EXECUTORS: Dict[str, ExecutorFn] = {
    "inbox_deferred_enrichment": _execute_inbox_deferred_enrichment,
    "file_ingestion": _execute_file_ingestion,
    "enrichment_process_source": _execute_enrichment_process_source,
    "topic_consolidation": _execute_topic_consolidation,
    SIGNAL_DERIVE_RETRY_KIND: _execute_signal_derive_retry,
    LOCAL_SYNC_KIND: _execute_local_sync,
}


#: Kinds that run for minutes-to-hours and therefore get their own worker.
#: The general loop is strictly serial — one claim, then ``await process_job``
#: inline — so a multi-hour iMessage sync sitting in it would stall
#: inbox_deferred_enrichment, file_ingestion, enrichment_process_source,
#: topic_consolidation and signal_derive_retry for its whole run. Partitioning
#: by kind keeps that queue moving at exactly its present speed; the long lane
#: is separately serial, which is also the guard that stops two syncs of the
#: same source overlapping on one SQLite file.
_LONG_RUNNING_KINDS: frozenset[str] = frozenset({LOCAL_SYNC_KIND})

#: Lease for the long lane. The default 300s is shorter than a SINGLE iMessage
#: batch (~9 min here), so a healthy sync would spend most of its life looking
#: dead to anything that reads the table — including the queued/running check
#: that stops two syncs running at once. Long enough to cover a batch, short
#: enough that a real death is noticed in minutes; the executor also renews it
#: on every batch, so this is the worst case after a crash, not the norm.
LONG_JOB_LEASE_SECONDS = 1800

#: Idle cadence for the long lane. Every tick claims against SQLite under the
#: write gate, and this lane's kinds fire a few times a day — polling them four
#: times a second would double the queue's idle gate traffic to watch for work
#: that is almost never there, on a node whose write-gate contention is what put
#: the sync in a job in the first place. A newly enqueued sync waits at most
#: _LONG_MAX_POLL_SECONDS to start, which is nothing against a run measured in
#: hours, and the caller stamps the row 'processing' before that.
_LONG_IDLE_POLL_SECONDS = 1.0
_LONG_MAX_POLL_SECONDS = 10.0


def _executable_kinds() -> list[str]:
    """Kinds the general worker may claim. Computed per claim so tests that patch
    EXECUTORS are honored. A queued row of any other kind (e.g. written by a
    newer node version) stays queued and visible instead of being claimed and
    immediately failed as unknown."""
    return sorted(set(EXECUTORS) - _LONG_RUNNING_KINDS)


def _long_running_kinds() -> list[str]:
    """Kinds the dedicated long-job worker may claim.

    Intersected with EXECUTORS for the same reason ``_executable_kinds`` is
    computed per claim: a test that patches EXECUTORS must not leave this loop
    claiming rows nothing can run.
    """
    return sorted(set(EXECUTORS) & _LONG_RUNNING_KINDS)


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
    if kind in ("enrichment_process_source", LOCAL_SYNC_KIND):
        payload["_progress_updater"] = _progress_updater
    if kind == LOCAL_SYNC_KIND:
        payload["_job_id"] = job_id

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


async def _worker_loop(
    conn_factory: Callable[[], Any],
    *,
    kinds_fn: Callable[[], list[str]] = _executable_kinds,
    sweep_debts: bool = True,
    label: str = "pipeline",
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
    idle_seconds: float = _IDLE_POLL_SECONDS,
    max_idle_seconds: float = _MAX_POLL_SECONDS,
) -> None:
    """One serial claim-and-run loop over the kinds ``kinds_fn`` returns.

    Two instances run: the general queue, and a lane for the long-running kinds
    (see ``_LONG_RUNNING_KINDS``). ``sweep_debts`` belongs to the general loop
    only — running the derivation-debt sweep from both would double its
    database traffic for no benefit.
    """
    idle_delay = idle_seconds
    next_debt_sweep = 0.0
    while True:
        try:
            if not _enabled():
                await asyncio.sleep(1.0)
                continue

            now = time.monotonic()
            if sweep_debts and now >= next_debt_sweep:
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
                return claim_next_job(
                    own, lease_owner=_lease_owner, kinds=kinds_fn(), lease_seconds=lease_seconds
                )

            job = await asyncio.to_thread(_claim)
            if job is None:
                await asyncio.sleep(idle_delay)
                # Ease off while nothing is queued; snap back the moment work lands.
                idle_delay = min(idle_delay * 1.5, max_idle_seconds)
                continue
            idle_delay = idle_seconds
            try:
                await process_job(conn_factory, job)
            except Exception as exc:  # noqa: BLE001
                logger.warning("%s worker loop error: %s", label, exc, exc_info=exc)
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
                logger.info("%s claim found the database locked; backing off: %s", label, exc)
            else:
                logger.warning("%s worker loop error: %s", label, exc, exc_info=exc)
            await asyncio.sleep(idle_delay)
            idle_delay = min(idle_delay * 1.5, max_idle_seconds)


def start_pipeline_worker(conn_factory: Callable[[], Any]) -> None:
    """Start the async pipeline worker loops once per process.

    Two loops: the general queue, and a lane dedicated to the long-running
    kinds. They are started and stopped together and guarded by the same lock,
    so every existing caller of this function keeps working unchanged — it just
    now also gets the long lane.
    """
    global _worker_task, _long_worker_task
    if not _enabled():
        return
    with _worker_lock:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if _worker_task is None or _worker_task.done():
            _worker_task = loop.create_task(_worker_loop(conn_factory))
        # Checked independently of the general loop: an older process may have
        # started only the general one, and a half-started pair must be able to
        # finish starting rather than being skipped by a single combined guard.
        if _long_worker_task is None or _long_worker_task.done():
            _long_worker_task = loop.create_task(
                _worker_loop(
                    conn_factory,
                    kinds_fn=_long_running_kinds,
                    sweep_debts=False,
                    label="pipeline-long",
                    lease_seconds=LONG_JOB_LEASE_SECONDS,
                    idle_seconds=_LONG_IDLE_POLL_SECONDS,
                    max_idle_seconds=_LONG_MAX_POLL_SECONDS,
                )
            )


async def _cancel_worker_task(task: Optional[asyncio.Task]) -> None:
    """Cancel one worker task, tolerating a task from an already-closed loop."""
    if task is None or task.done():
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        return
    if task.get_loop() is not running:
        # Belongs to an earlier app instance's loop, which is already closed:
        # awaiting it here raises "attached to a different loop". Dropping the
        # reference by the caller is both all we can do and all that is needed —
        # a closed loop is not running it.
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 — teardown never raises
        pass


async def stop_pipeline_worker() -> None:
    """Cancel the worker loops started by :func:`start_pipeline_worker`.

    Without this the task outlived its app. ``start_pipeline_worker`` skips when
    the task is not ``done()``, and a task left pending on a closed loop never
    becomes done — so the FIRST app instance in a process owned the worker
    forever and every later one silently ran without one. Clearing the globals is
    the part that matters; the cancel just stops a live loop from writing during
    the next app's startup.

    Both loops are cleared under one lock acquisition, so a teardown can never
    leave the long lane running against a closed app.
    """
    global _worker_task, _long_worker_task
    with _worker_lock:
        task = _worker_task
        long_task = _long_worker_task
        _worker_task = None
        _long_worker_task = None
    await _cancel_worker_task(task)
    await _cancel_worker_task(long_task)


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
        # Both lanes: this helper means "drain what is runnable", and the
        # long-running kinds are runnable — they are merely given their own
        # worker in production so they cannot block the general queue.
        return claim_next_job(own, lease_owner=_lease_owner, kinds=sorted(EXECUTORS))

    for _ in range(max(1, int(limit))):
        job = await asyncio.to_thread(_claim)
        if job is None:
            break
        await process_job(conn_factory, job)
        processed += 1
    return processed
