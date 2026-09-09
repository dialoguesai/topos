"""Enqueue a local sync (iMessage, Signal) as a durable background job.

One helper, two doors. The websocket handler (``source_sync``) and the node's
own HTTP route serve the SAME url and the same user-visible button — the app
reaches the first in production and the second through the dev proxy — and they
had already drifted once: only the HTTP route refreshed messenger analytics
after a sync, so the same click produced different state depending on which door
it came through. Both now enqueue through here, so a change to sync semantics
cannot land on one path and miss the other.

The work itself runs in ``job_runner._execute_local_sync``, on the worker lane
reserved for long jobs.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("topos.ingestion.local_sync_jobs")

#: Sources this helper knows how to run. Anything else is a caller error rather
#: than a job that would be claimed and then fail as unrunnable.
SUPPORTED_SYNC_SOURCE_IDS = ("imessage", "signal")


async def enqueue_local_sync(
    *,
    source_id: str,
    dataset_id: str,
    sync_options: Optional[Dict[str, Any]],
    conn_factory: Callable[[], Any],
) -> Dict[str, Any]:
    """Queue one sync and return its handle, without waiting for it to run.

    Returns ``{"status": "ok", "job_id": ..., "already_running": bool}`` or
    ``{"status": "error", "error": ...}``. Never raises for ordinary failures:
    both callers turn the returned dict straight into a response body.

    Every job-store call takes the write gate — a blocking OS lock — so the
    whole enqueue runs in one ``asyncio.to_thread`` with the connection fetched
    INSIDE the thread. Taking that gate on the event loop is the 2026-08-07
    freeze; handing a connection across threads is the 2026-07-30 transaction
    corruption.
    """
    from ..pipeline.job_runner import LOCAL_SYNC_KIND, start_pipeline_worker
    from ..pipeline.job_store import (
        enqueue_job,
        find_active_job,
        reclaim_stale_job,
        update_job_progress,
    )

    source_id = (source_id or "").strip()
    dataset_id = (dataset_id or "").strip()
    if not source_id or not dataset_id:
        return {"status": "error", "error": "source_id and dataset_id required"}
    if source_id not in SUPPORTED_SYNC_SOURCE_IDS:
        return {"status": "error", "error": f"sync not implemented for source_id={source_id}"}
    if conn_factory() is None:
        return {"status": "error", "error": "Database connection not available"}

    job_id = str(uuid.uuid4())
    job_payload = {
        "source_id": source_id,
        "dataset_id": dataset_id,
        "sync_options": sync_options,
    }

    def _enqueue_and_stamp() -> Dict[str, Any]:
        own = conn_factory()
        if own is None:
            return {"status": "error", "error": "Database connection not available"}
        # A node that stopped mid-sync leaves a row marked running with a dead
        # owner, and the only stale-job sweep runs at startup — so without this
        # the next press would read that corpse as a live sync and refuse to
        # start behind it, wedging the button with no error anywhere. Requeuing
        # the same row resumes that sync from its checkpoint instead.
        reclaimed = reclaim_stale_job(
            own, kind=LOCAL_SYNC_KIND, source_id=source_id, dataset_id=dataset_id
        )
        if reclaimed:
            logger.info(
                "[PIPELINE:SYNC] requeued a sync whose worker died: source_id=%s job_id=%s",
                source_id,
                reclaimed,
            )
        active = find_active_job(
            own, kind=LOCAL_SYNC_KIND, source_id=source_id, dataset_id=dataset_id
        )
        if active:
            # A second press re-attaches to the run already in flight rather
            # than starting a rival one against the same SQLite file — the
            # shape behind the live 'database is locked' receipt. Returning the
            # existing handle (instead of an error) means a double-click, a
            # second tab and a page reload all land on the same job.
            return {
                "status": "ok",
                "job_id": str(active.get("job_id")),
                "already_running": True,
            }
        enqueue_job(
            own,
            kind=LOCAL_SYNC_KIND,
            payload=job_payload,
            job_id=job_id,
            source_id=source_id,
            # Unique per request, never stable: enqueue_job hands back a `done`
            # row's id untouched, so a stable key would let the first success
            # block every later sync forever. Mutual exclusion is find_active_job's
            # job, above, which only looks at queued/running rows.
            idempotency_key=f"{LOCAL_SYNC_KIND}:{job_id}",
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
                "batch_num": 0,
            },
        )
        return {"status": "ok", "job_id": job_id, "already_running": False}

    outcome = await asyncio.to_thread(_enqueue_and_stamp)
    if outcome.get("status") == "ok":
        start_pipeline_worker(conn_factory)
        logger.info(
            "[PIPELINE:SYNC] source_sync %s: source_id=%s job_id=%s",
            "re-attached to running job" if outcome.get("already_running") else "enqueued",
            source_id,
            outcome.get("job_id"),
        )
    return outcome
