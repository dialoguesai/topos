"""Enqueue a local sync (iMessage, Signal, the AI coding tools) as a durable background job.

One helper, three doors. The websocket handler (``source_sync``) and the node's
own HTTP route serve the SAME url and the same user-visible button — the app
reaches the first in production and the second through the dev proxy — and they
had already drifted once: only the HTTP route refreshed messenger analytics
after a sync, so the same click produced different state depending on which door
it came through. Both now enqueue through here, and so does the node's own sync
scheduler (``local_sync_schedule``), so a change to sync semantics cannot land
on one path and miss the others.

The work itself runs in ``job_runner._execute_local_sync``, on the worker lane
reserved for long jobs.

``LOCAL_SYNC_RUNNERS`` (CONTRACTS.md §2.8) is the one table every door reads:
the job executor's guard and branch, this enqueue door, the relay guard in
``core/handlers/sources.py`` and the scheduler's eligibility in
``local_sync_schedule``. A source that is not in it cannot be synced by any
door. iMessage and Signal are registered with exactly their former behaviour;
the three AI-coding-tool sources (``ingestion/ai_tool_sync.py``) join them.
Every entry resolves its callables lazily so a test that patches
``topos.ingestion.local_sync.run_imessage_sync`` still sees its patch.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, Optional, Tuple

logger = logging.getLogger("topos.ingestion.local_sync_jobs")


def _lazy(module: str, name: str) -> Callable[..., Any]:
    """A callable that resolves ``module.name`` at call time (so a monkeypatched attribute is honoured)."""

    def call(*args: Any, **kwargs: Any) -> Any:
        return getattr(importlib.import_module(module), name)(*args, **kwargs)

    call.__name__ = name
    call.__qualname__ = f"{module}.{name}"
    return call


@dataclass(frozen=True)
class LocalSyncRunner:
    """One local-sync source as the doors see it.

    ``run(dataset_id, *, sync_options, progress_cb, batch_size, **kw) -> dict`` — the sync itself,
    the ``run_imessage_sync`` result shape.
    ``describe_checkpoint(conn, dataset_id) -> dict`` — the settings screen's "where the next
    since-last sync starts"; None when the source shows none.
    ``post_sync(conn, dataset_id, result, sync_options) -> dict`` — after the receipt, on the
    executor's thread: decorate the result (the ``sync`` summary) and run any follow-up (the
    messenger analytics refresh). A failure here never fails a landed sync.
    ``default_options(sync_options) -> sync_options`` — what a door fills in when the caller sent
    none (iMessage: the 3-month window).
    ``pre_enqueue_refusal(conn, dataset_id, sync_options) -> refusal | None`` — asked before a job
    exists and before a schedule is enabled, so the button and the settings screen say why at once.
    ``schedulable`` — whether the node's scheduler may run it.
    ``after_settle(dataset_id, outcome)`` — the scheduler's hand-off once a scheduled run settled
    (iMessage: the owner's standing attestation). Beyond the six contract fields, optional.
    ``default_schedule`` — the ``put_schedule`` changes the scheduler writes for an ENABLED source
    row that has no schedule row at all (``local_sync_schedule.ensure_default_schedules``). None
    means the source is scheduled only when the owner, or its own enable path, writes a row.
    """

    source_id: str
    run: Callable[..., Dict[str, Any]]
    schedulable: bool
    describe_checkpoint: Optional[Callable[[Any, str], Dict[str, Any]]] = None
    post_sync: Optional[Callable[..., Dict[str, Any]]] = None
    default_options: Optional[Callable[[Optional[Dict[str, Any]]], Optional[Dict[str, Any]]]] = None
    pre_enqueue_refusal: Optional[Callable[[Any, str, Optional[Dict[str, Any]]], Optional[Dict[str, Any]]]] = None
    after_settle: Optional[Callable[[str, str], None]] = None
    default_schedule: Optional[Dict[str, Any]] = None


# --- iMessage and Signal, exactly as before --------------------------------------


def _messenger_analytics_refresh(conn: Any, dataset_id: str, result: Dict[str, Any], source_id: str) -> None:
    """The refresh both messenger doors get after a sync that moved rows (the executor is the single door)."""
    if str(result.get("status") or "") != "ok" or int(result.get("records_processed") or 0) <= 0:
        return
    try:
        from ..analytics.messenger_communities import compute_and_persist_messenger_analytics

        compute_and_persist_messenger_analytics(
            dataset_id=dataset_id, conn=conn, source_ids=[source_id], period_granularity="month",
        )
    except Exception as exc:  # noqa: BLE001 — never fail a good sync on analytics
        logger.warning("messenger analytics refresh failed after sync: %s", exc)


def _imessage_post_sync(conn: Any, dataset_id: str, result: Dict[str, Any], sync_options: Any) -> Dict[str, Any]:
    from ..pipeline.job_runner import _sync_summary

    decorated = {**result, "sync": _sync_summary(result, sync_options)}
    _messenger_analytics_refresh(conn, dataset_id, decorated, "imessage")
    return decorated


def _signal_post_sync(conn: Any, dataset_id: str, result: Dict[str, Any], sync_options: Any) -> Dict[str, Any]:
    _messenger_analytics_refresh(conn, dataset_id, result, "signal")
    return result


def _imessage_default_options(sync_options: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    return sync_options if sync_options else {"mode": "3m"}


def _imessage_after_settle(dataset_id: str, outcome: str) -> None:
    from .local_sync_schedule import _prove_after_sync

    _prove_after_sync(dataset_id, outcome)


# --- the AI coding tools ---------------------------------------------------------


def _ai_tool_runner(source_id: str, run_name: str) -> LocalSyncRunner:
    module = "topos.ingestion.ai_tool_sync"
    describe = _lazy(module, "describe_ai_tool_checkpoint")
    refusal = _lazy(module, "sync_refusal")
    return LocalSyncRunner(
        source_id=source_id,
        run=_lazy(module, run_name),
        schedulable=True,
        describe_checkpoint=lambda conn, dataset_id: describe(conn, dataset_id, source_id),
        post_sync=_lazy(module, "ai_tool_post_sync"),
        default_options=lambda options: options if options else {"mode": "since_last"},
        pre_enqueue_refusal=lambda conn, dataset_id, options: refusal(conn, source_id, options),
    )


LOCAL_SYNC_RUNNERS: Dict[str, LocalSyncRunner] = {
    "imessage": LocalSyncRunner(
        source_id="imessage",
        run=_lazy("topos.ingestion.local_sync", "run_imessage_sync"),
        schedulable=True,
        describe_checkpoint=_lazy("topos.ingestion.local_sync", "describe_imessage_checkpoint"),
        post_sync=_imessage_post_sync,
        default_options=_imessage_default_options,
        pre_enqueue_refusal=_lazy("topos.ingestion.local_sync", "enrolled_dataset_refusal"),
        after_settle=_imessage_after_settle,
        # Hourly, like the coding tools, and without an owner action: the schedule shipped opt-in on
        # 29 Sep 2026 and nothing ever opted in, so the owner's node synced iMessage only by the
        # button (six runs, the last on 8 Oct) while the scheduler ran the coding tools every hour.
        # The coding tools keep writing their own row at enable time (ai_tool_sync.enable_ai_tool_source).
        default_schedule={"enabled": True, "interval_hours": 1},
    ),
    # Signal needs a key the node may not hold, so the node never schedules it; no checkpoint on the
    # settings screen, no enrollment guard, no default window.
    "signal": LocalSyncRunner(
        source_id="signal",
        run=_lazy("topos.ingestion.local_sync", "run_signal_sync"),
        schedulable=False,
        post_sync=_signal_post_sync,
    ),
    "claude_code_sessions": _ai_tool_runner("claude_code_sessions", "run_claude_code_sync"),
    "codex_sessions": _ai_tool_runner("codex_sessions", "run_codex_sync"),
    "cursor_sessions": _ai_tool_runner("cursor_sessions", "run_cursor_sync"),
}

#: Sources this helper knows how to run. Anything else is a caller error rather
#: than a job that would be claimed and then fail as unrunnable. Kept as a name for
#: readers of the old tuple; the table is the truth.
SUPPORTED_SYNC_SOURCE_IDS: Tuple[str, ...] = tuple(LOCAL_SYNC_RUNNERS)


def runner_for(source_id: str) -> Optional[LocalSyncRunner]:
    return LOCAL_SYNC_RUNNERS.get(str(source_id or "").strip())


def schedulable_source_ids() -> Tuple[str, ...]:
    return tuple(source_id for source_id, runner in LOCAL_SYNC_RUNNERS.items() if runner.schedulable)


def no_receipt_codes() -> FrozenSet[str]:
    """Result codes after which the executor writes no ``user_ingestion_sources`` receipt.

    A plan waiting for the owner or a preview must not move ``last_sync_at``; a run refused because
    another holds the dataset leaves the receipt to that run; a dataset refused as not enrolled, a
    source that is not enabled, paused, or on an unpaired node must not get a source row at all —
    its INSERT alone moves the enrollment's clock, and the row defaults to enabled=1.
    """
    from .ai_tool_sync import NO_RECEIPT_CODES
    from .local_sync import DATASET_NOT_ENROLLED, SYNC_IN_PROGRESS

    return frozenset({SYNC_IN_PROGRESS, DATASET_NOT_ENROLLED, *NO_RECEIPT_CODES})


def enqueue_local_sync_blocking(
    conn: Any,
    *,
    source_id: str,
    dataset_id: str,
    sync_options: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Queue one sync on ``conn`` from the CALLING thread; never on the event loop.

    Every job-store call takes the write gate, a blocking OS lock. The async
    wrapper below runs this in a worker thread; the scheduler calls it from its
    own. Returns ``{"status": "ok", "job_id": ..., "already_running": bool}`` or
    ``{"status": "error", "error": ..., "code"?: ...}``.
    """
    from ..pipeline.job_runner import LOCAL_SYNC_KIND
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
    runner = runner_for(source_id)
    if runner is None:
        return {"status": "error", "error": f"sync not implemented for source_id={source_id}"}
    if conn is None:
        return {"status": "error", "error": "Database connection not available"}

    if runner.default_options is not None:
        sync_options = runner.default_options(sync_options)
    if runner.pre_enqueue_refusal is not None:
        # Refused here, before a job exists, so the button says why at once. The
        # sync repeats the check when it runs, for the paths that skip this one.
        refusal = runner.pre_enqueue_refusal(conn, dataset_id, sync_options)
        if refusal is not None:
            return {"status": "error", "error": refusal["error"], "code": refusal.get("code")}

    # A node that stopped mid-sync leaves a row marked running with a dead
    # owner, and the only stale-job sweep runs at startup — so without this
    # the next press would read that corpse as a live sync and refuse to
    # start behind it, wedging the button with no error anywhere. Requeuing
    # the same row resumes that sync from its checkpoint instead.
    reclaimed = reclaim_stale_job(
        conn, kind=LOCAL_SYNC_KIND, source_id=source_id, dataset_id=dataset_id
    )
    if reclaimed:
        logger.info(
            "[PIPELINE:SYNC] requeued a sync whose worker died: source_id=%s job_id=%s",
            source_id,
            reclaimed,
        )
    active = find_active_job(
        conn, kind=LOCAL_SYNC_KIND, source_id=source_id, dataset_id=dataset_id
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
    job_id = str(uuid.uuid4())
    enqueue_job(
        conn,
        kind=LOCAL_SYNC_KIND,
        payload={
            "source_id": source_id,
            "dataset_id": dataset_id,
            "sync_options": sync_options,
        },
        job_id=job_id,
        source_id=source_id,
        # Unique per request, never stable: enqueue_job hands back a `done`
        # row's id untouched, so a stable key would let the first success
        # block every later sync forever. Mutual exclusion is find_active_job's
        # job, above, which only looks at queued/running rows.
        idempotency_key=f"{LOCAL_SYNC_KIND}:{job_id}",
    )
    update_job_progress(
        conn,
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
    from ..pipeline.job_runner import start_pipeline_worker

    source_id = (source_id or "").strip()
    dataset_id = (dataset_id or "").strip()
    if not source_id or not dataset_id:
        return {"status": "error", "error": "source_id and dataset_id required"}
    if runner_for(source_id) is None:
        return {"status": "error", "error": f"sync not implemented for source_id={source_id}"}

    def _enqueue() -> Dict[str, Any]:
        return enqueue_local_sync_blocking(
            conn_factory(),
            source_id=source_id,
            dataset_id=dataset_id,
            sync_options=sync_options,
        )

    outcome = await asyncio.to_thread(_enqueue)
    if outcome.get("status") == "ok":
        start_pipeline_worker(conn_factory)
        logger.info(
            "[PIPELINE:SYNC] source_sync %s: source_id=%s job_id=%s",
            "re-attached to running job" if outcome.get("already_running") else "enqueued",
            source_id,
            outcome.get("job_id"),
        )
    return outcome


__all__ = [
    "LOCAL_SYNC_RUNNERS",
    "LocalSyncRunner",
    "SUPPORTED_SYNC_SOURCE_IDS",
    "enqueue_local_sync",
    "enqueue_local_sync_blocking",
    "no_receipt_codes",
    "runner_for",
    "schedulable_source_ids",
]
