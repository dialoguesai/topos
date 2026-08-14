"""Durable retry records for signal-derivation jobs that failed mid-batch.

A derivation job used to fail like this:

    ERROR job facts failed: cannot commit - no transaction is active
    DEBUG complete source_id=github_activity jobs_run=8 deferred=[]

The exception was caught, appended to an in-memory ``results["errors"]`` list,
and dropped on the floor. The completion line printed ``jobs_run`` and
``deferred`` but never ``errors``, so a batch that lost data read as clean. The
facts for 88 GitHub commits were simply gone, with nothing anywhere recording
that they should exist.

The raw payloads survive — ingest writes those before derivation runs — so the
loss is recoverable in principle. It just needs something durable to point at.
That is this module: every failed derivation is written to ``pipeline_jobs``
(the existing leased, idempotent, recovery-indexed queue) so it can be found and
re-run later, by a person or by the worker.

Everything here is fail-soft. Recording a failure must never turn a partial
batch into a crashed one.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: pipeline_jobs.kind for a derivation that must be re-run.
SIGNAL_DERIVE_RETRY_KIND = "signal_derive_retry"

#: Record ids kept per retry record. Enough to re-run a batch or to tell a human
#: exactly what is missing, without turning the queue row into a data store.
_MAX_RECORD_IDS = 500

#: engine_config key stamped the first time this node is able to record a
#: derivation failure. Everything BEFORE it is invisible to this mechanism —
#: the 2026-07-30 loss that motivated the module has no record of its own,
#: because the code did not exist yet.
RECORDING_SINCE_KEY = "derivation_debt.recording_since"

#: What a clean report actually covers. Surfaced verbatim so no caller has to
#: infer the scope, and so "no known gaps" is never read as "nothing is missing".
DEBT_COVERAGE = "signal derivation jobs (not ingestion, sync, or connector fetches)"


def ensure_recording_since(conn: Optional[sqlite3.Connection]) -> Optional[str]:
    """Stamp (once) when this node became able to record derivation failures.

    Returns the ISO timestamp, or None if it could not be established. Written
    on first read rather than by a migration so an existing node stamps the
    moment it upgrades — which is the honest answer to "since when have you
    been watching?", and is NOT the date this code was written.
    """
    if conn is None:
        return None
    try:
        from ..core.state import get_engine_config_value, set_engine_config_value

        existing = get_engine_config_value(conn, RECORDING_SINCE_KEY)
        if existing:
            return str(existing)
        from datetime import datetime, timezone

        stamp = datetime.now(timezone.utc).isoformat()
        set_engine_config_value(conn, RECORDING_SINCE_KEY, stamp)
        logger.info("[DERIVE:RETRY] derivation-debt recording starts %s", stamp)
        return stamp
    except Exception as exc:  # noqa: BLE001 — a missing stamp must not break the report
        logger.debug("[DERIVE:RETRY] could not establish recording_since: %s", exc)
        return None


def retry_idempotency_key(sync_batch_id: str, job_name: str) -> str:
    """One retry record per (batch, job) — a job that fails twice is one debt."""
    return f"{sync_batch_id or 'unknown'}:{job_name}:signal_derive_retry"


def record_failed_derivation(
    conn: Optional[sqlite3.Connection],
    *,
    source_id: str,
    sync_batch_id: str,
    job_name: str,
    error: str,
    record_ids: Optional[Sequence[str]] = None,
    record_count: int = 0,
) -> Optional[str]:
    """Persist a failed derivation so it can be re-run. Returns job_id or None.

    Idempotent per (batch, job): ``enqueue_job`` re-queues an existing failed
    row rather than accumulating duplicates.
    """
    if conn is None:
        logger.warning(
            "[DERIVE:RETRY] no connection; derivation loss NOT recorded "
            "source=%s batch=%s job=%s",
            source_id,
            sync_batch_id,
            job_name,
        )
        return None
    ids = [str(r) for r in (record_ids or []) if r][:_MAX_RECORD_IDS]
    try:
        from ..pipeline.job_store import enqueue_job

        job_id = enqueue_job(
            conn,
            kind=SIGNAL_DERIVE_RETRY_KIND,
            payload={
                "source_id": source_id,
                "sync_batch_id": sync_batch_id,
                "job_name": job_name,
                "error": str(error)[:2000],
                "record_ids": ids,
                "record_count": int(record_count or len(ids)),
            },
            source_id=source_id,
            sync_batch_id=sync_batch_id,
            idempotency_key=retry_idempotency_key(sync_batch_id, job_name),
        )
        logger.warning(
            "[DERIVE:RETRY] recorded failed derivation job=%s source=%s batch=%s "
            "records=%d job_id=%s — data is MISSING until this is re-run",
            job_name,
            source_id,
            sync_batch_id,
            int(record_count or len(ids)),
            job_id,
        )
        return job_id
    except Exception as exc:  # noqa: BLE001 — recording must never break the batch
        # Last resort: if even the durable record cannot be written, say so as
        # loudly as possible. Silence here is what caused the original loss.
        logger.error(
            "[DERIVE:RETRY] FAILED TO RECORD derivation loss source=%s batch=%s "
            "job=%s records=%d original_error=%s recording_error=%s",
            source_id,
            sync_batch_id,
            job_name,
            int(record_count or len(ids)),
            str(error)[:200],
            exc,
        )
        return None


def clear_derivation_retry(
    conn: Optional[sqlite3.Connection],
    *,
    sync_batch_id: str,
    job_name: str,
) -> bool:
    """Mark a (batch, job) debt settled after a later run succeeded."""
    if conn is None:
        return False
    key = retry_idempotency_key(sync_batch_id, job_name)
    try:
        from ..storage.db.write_gate import commit_connection, with_db_write

        with with_db_write():
            cur = conn.execute(
                """
                UPDATE pipeline_jobs
                SET status='done', finished_at=datetime('now'), updated_at=datetime('now')
                WHERE idempotency_key=? AND status != 'done'
                """,
                (key,),
            )
            changed = int(cur.rowcount or 0)
            # Commit even when 0 rows matched: the UPDATE opened an implicit
            # transaction regardless, and leaving it open makes the next
            # BEGIN IMMEDIATE on this connection fail — which broke every
            # topic_clusters batch (this runs after each successful job).
            commit_connection(conn)
        if changed:
            logger.info(
                "[DERIVE:RETRY] cleared derivation debt job=%s batch=%s",
                job_name,
                sync_batch_id,
            )
        return bool(changed)
    except Exception as exc:  # noqa: BLE001 — never break a successful batch
        logger.debug("[DERIVE:RETRY] clear failed batch=%s job=%s: %s", sync_batch_id, job_name, exc)
        return False


def list_pending_derivation_retries(
    conn: Optional[sqlite3.Connection],
    *,
    source_id: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Outstanding derivation debts, newest first — what data is still missing."""
    if conn is None:
        return []
    try:
        from ..pipeline.job_store import ensure_pipeline_jobs_schema

        ensure_pipeline_jobs_schema(conn)
        sql = (
            "SELECT job_id, source_id, sync_batch_id, payload_json, status, created_at, updated_at "
            "FROM pipeline_jobs WHERE kind=? AND status IN ('queued','running','failed')"
        )
        params: List[Any] = [SIGNAL_DERIVE_RETRY_KIND]
        if source_id:
            sql += " AND source_id=?"
            params.append(source_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[DERIVE:RETRY] listing failed: %s", exc)
        return []

    out: List[Dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(str(row[3] or "{}"))
        except (ValueError, TypeError):
            payload = {}
        out.append(
            {
                "job_id": str(row[0]),
                "source_id": str(row[1] or payload.get("source_id") or ""),
                "sync_batch_id": str(row[2] or payload.get("sync_batch_id") or ""),
                "job_name": str(payload.get("job_name") or ""),
                "record_count": int(payload.get("record_count") or 0),
                "record_ids": list(payload.get("record_ids") or []),
                "error": str(payload.get("error") or ""),
                "status": str(row[4] or ""),
                "created_at": str(row[5] or ""),
                "updated_at": str(row[6] or ""),
            }
        )
    return out


def _deferral_reason(result: Dict[str, Any], job_name: str) -> str:
    """The job's own deferral error ('ollama_unreachable'), if it envelope'd one."""
    for env in result.get("envelopes") or []:
        prov = env.get("provenance") or {}
        if prov.get("job_name") == job_name and prov.get("status") == "deferred":
            return f"{job_name} deferred: {env.get('error') or 'unknown'}"
    return f"{job_name} deferred again"


async def retry_single_derivation(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    sync_batch_id: str,
    job_name: str,
    record_ids: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Re-run exactly one recorded derivation debt.

    Reloads the debt's canonical records and re-runs ONLY the job that failed,
    so a retry cannot disturb jobs that already succeeded. Jobs are idempotent
    per (batch, job) at the ledger level; a retry that fails again simply leaves
    the debt in place for the next attempt.

    Returns ``{"outcome": "recovered" | "still_failing" | "skipped", ...}``.
    "skipped" means the debt cannot be re-run mechanically (unknown source, no
    surviving records) — the caller decides whether that discharges it.
    """
    if not job_name or not source_id:
        return {"outcome": "skipped", "reason": "incomplete record"}
    try:
        from ..ingestion.canonical_pipeline import load_canonical_records_for_signal
        # Reuses reprocess's resolver so a runtime-installed source rehydrates
        # the same way it does for a manual reprocess.
        from ..ingestion.reprocess import _resolve_source_def

        try:
            source_def = _resolve_source_def(source_id)
        except ValueError:
            return {"outcome": "skipped", "reason": "unknown source"}
        records = load_canonical_records_for_signal(conn, source_def)
        wanted = {str(r) for r in (record_ids or []) if r}
        if wanted:
            scoped = [
                r
                for r in records
                if str(r.get("message_id") or r.get("record_id") or "") in wanted
            ]
            records = scoped or records
        if not records:
            return {"outcome": "skipped", "reason": "no records"}

        from .orchestrator import SignalDerivationOrchestrator

        result = await SignalDerivationOrchestrator().run_signal_derivation(
            records, source_id, job_names=[job_name], sync_batch_id=sync_batch_id
        )
        if result.get("errors"):
            return {
                "outcome": "still_failing",
                "error": result["errors"][0].get("error"),
            }
        if job_name in (result.get("deferred_jobs") or []):
            # A deferral is a return value, not a raise, so it never reaches
            # results["errors"]. Falling through to "recovered" discharged the
            # debt and marked the queue row done with zero rows created — the
            # retry claimed to have repaired data it never produced. Retrying
            # into a provider that is still down is still failing.
            return {
                "outcome": "still_failing",
                "error": _deferral_reason(result, job_name),
            }
        # run_signal_derivation clears the debt itself on success.
        return {
            "outcome": "recovered",
            "records": len(records),
            "created": result.get("records_created", {}).get(job_name, 0),
        }
    except Exception as exc:  # noqa: BLE001 — one bad debt must not stop the rest
        logger.warning("[DERIVE:RETRY] retry failed job=%s source=%s: %s", job_name, source_id, exc)
        return {"outcome": "still_failing", "error": str(exc)}


async def retry_pending_derivations(
    conn: Optional[sqlite3.Connection] = None,
    *,
    source_id: Optional[str] = None,
    limit: int = 20,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Re-run derivation jobs that previously failed, clearing settled debts.

    ``dry_run`` reports what would run without touching anything.
    """
    from ..core.state import get_db_connection

    conn = conn if conn is not None else get_db_connection()
    pending = list_pending_derivation_retries(conn, source_id=source_id, limit=limit)
    outcome: Dict[str, Any] = {
        "attempted": 0,
        "recovered": [],
        "still_failing": [],
        "skipped": [],
        "dry_run": bool(dry_run),
        "pending_before": len(pending),
    }
    if not pending or conn is None:
        return outcome

    if dry_run:
        outcome["skipped"] = [
            {"job_name": p["job_name"], "source_id": p["source_id"], "batch": p["sync_batch_id"]}
            for p in pending
        ]
        return outcome

    for debt in pending:
        job_name = debt["job_name"]
        src = debt["source_id"]
        batch = debt["sync_batch_id"]
        result = await retry_single_derivation(
            conn,
            source_id=src,
            sync_batch_id=batch,
            job_name=job_name,
            record_ids=debt.get("record_ids") or [],
        )
        kind = result.pop("outcome")
        entry = {"job_name": job_name, "source_id": src, **result}
        if kind == "recovered":
            outcome["attempted"] += 1
            outcome["recovered"].append({**entry, "batch": batch})
        elif kind == "still_failing":
            outcome["attempted"] += 1
            outcome["still_failing"].append(entry)
        else:
            outcome["skipped"].append(entry)

    outcome["pending_after"] = len(list_pending_derivation_retries(conn, source_id=source_id, limit=1000))
    logger.info(
        "[DERIVE:RETRY] retry pass: attempted=%d recovered=%d still_failing=%d",
        outcome["attempted"],
        len(outcome["recovered"]),
        len(outcome["still_failing"]),
    )
    return outcome


#: engine_config key holding the last observed readiness of each blocking
#: provider. PERSISTED rather than process state: installing a model and
#: restarting the app is how a person actually does it, so the edge has to
#: survive the restart to fire. Keeping it in memory instead would either miss
#: that sequence entirely, or — if a cold start counted as an edge — re-run
#: every genuinely broken debt on every launch.
PROVIDER_READY_KEY = "derivation_debt.provider_ready"


def _read_last_ready(conn: sqlite3.Connection) -> Dict[str, bool]:
    try:
        from ..core.state import get_engine_config_value

        raw = get_engine_config_value(conn, PROVIDER_READY_KEY)
        return {str(k): bool(v) for k, v in json.loads(str(raw or "{}")).items()}
    except Exception as exc:  # noqa: BLE001 — unknown reads as "never observed"
        logger.debug("[DERIVE:RETRY] could not read provider readiness: %s", exc)
        return {}


def _write_last_ready(conn: sqlite3.Connection, ready: Dict[str, bool]) -> None:
    try:
        from ..core.state import set_engine_config_value

        set_engine_config_value(conn, PROVIDER_READY_KEY, json.dumps(ready, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 — failing to remember must not break the sweep
        logger.debug("[DERIVE:RETRY] could not persist provider readiness: %s", exc)


def revive_capability_blocked_debts(
    conn: Optional[sqlite3.Connection],
    *,
    limit: int = 200,
) -> Dict[str, Any]:
    """Re-queue failed debts whose provider has just become reachable.

    EDGE-triggered, not level-triggered. Re-queueing every failed debt whose
    job looks runnable would put debts that fail for real reasons — a bug, a
    corrupt record — back on the queue on every sweep, forever. Firing only on
    the not-ready → ready transition means a debt gets exactly one fresh
    attempt per time the missing provider actually shows up, which is the event
    that could plausibly change the outcome.

    The edge is read from and written to ``engine_config``, so it survives a
    restart. That matters in both directions: "install the model, relaunch the
    app" still fires, and a node that has always had the provider does not
    re-run its broken debts on every launch.

    This is the half that makes recorded debt self-healing: ``run_derivation_
    retry_job`` declines to burn an attempt while the provider is absent, so
    the debt sits parked until this notices the provider arrive.
    """
    from .job_readiness import blocking_providers_ready, provider_for_job

    out: Dict[str, Any] = {"revived": 0, "newly_ready": [], "job_ids": []}
    if conn is None:
        return out

    current = blocking_providers_ready(force=True)
    last = _read_last_ready(conn)
    newly_ready = sorted(p for p, ready in current.items() if ready and not last.get(p, False))
    _write_last_ready(conn, {**last, **current})
    out["newly_ready"] = newly_ready
    if not newly_ready:
        return out

    ready_set = set(newly_ready)
    candidates = [
        debt
        for debt in list_pending_derivation_retries(conn, limit=limit)
        if debt.get("status") == "failed"
        and provider_for_job(str(debt.get("job_name") or "")) in ready_set
    ]
    if not candidates:
        return out

    try:
        from ..pipeline.job_store import requeue_failed_jobs

        job_ids = [str(d["job_id"]) for d in candidates]
        moved = requeue_failed_jobs(conn, job_ids)
    except Exception as exc:  # noqa: BLE001 — a sweep must never break the worker
        logger.warning("[DERIVE:RETRY] reviving blocked debts failed: %s", exc)
        return out

    out["revived"] = int(moved)
    out["job_ids"] = job_ids
    if moved:
        logger.info(
            "[DERIVE:RETRY] %s reachable again — re-queued %d parked derivation debt(s): %s",
            ", ".join(newly_ready),
            moved,
            ", ".join(sorted({str(d.get("job_name") or "?") for d in candidates})),
        )
    return out


#: How long a worker-claimed retry waits for an active derivation batch to end
#: before handing the job back to the queue. Debts are recorded MID-batch, and
#: the worker claims within its poll interval — re-running immediately would
#: re-contend for the same write gate that produced the original "database is
#: locked". The wait doubles as the requeue backoff: while a long batch runs,
#: the worker cycles claim → wait → requeue at this period instead of spinning.
_IN_FLIGHT_WAIT_SECONDS = 30.0
_IN_FLIGHT_POLL_SECONDS = 0.5


async def run_derivation_retry_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Worker executor for ``SIGNAL_DERIVE_RETRY_KIND`` pipeline jobs.

    This is the "by the worker" half of the module contract: the debt record IS
    the queue row, so the worker claims it and re-runs the one derivation it
    describes. Success marks the row done (the orchestrator clears the debt,
    then the worker completes the job). A retry that fails again returns an
    error status, parking the row 'failed' — still visible in every debt
    listing, and re-queued by the next organic failure of the same (batch, job),
    by ``revive_capability_blocked_debts`` when a missing provider returns, or
    by ``POST /signal/derivation-debt/retry``. One claim is one attempt; nothing
    here loops.
    """
    import asyncio

    from .job_readiness import job_is_ready
    from .pipeline_activity import is_derivation_in_flight

    job_name = str(payload.get("job_name") or "")

    # Asked FIRST, before the in-flight wait: a debt that cannot run at all is
    # not worth blocking a worker for 30s to discover. Re-running would reload
    # every canonical record for the batch and re-enter the orchestrator only
    # to defer again — expensive work whose sole output is the same parked row
    # and a spent attempt. Park it with what it is waiting FOR instead of a
    # generic failure; revive_capability_blocked_debts() picks it back up when
    # the provider returns.
    ready, reason = job_is_ready(job_name)
    if not ready:
        logger.info(
            "[DERIVE:RETRY] holding debt job=%s batch=%s — %s",
            job_name,
            payload.get("sync_batch_id"),
            reason,
        )
        return {"status": "error", "error": f"waiting for provider: {reason}"}

    waited = 0.0
    while is_derivation_in_flight():
        if waited >= _IN_FLIGHT_WAIT_SECONDS:
            return {"status": "requeue", "reason": "derivation in flight"}
        await asyncio.sleep(_IN_FLIGHT_POLL_SECONDS)
        waited += _IN_FLIGHT_POLL_SECONDS

    from ..core.state import get_db_connection

    conn = get_db_connection()
    if conn is None:
        return {"status": "error", "error": "no database connection"}

    result = await retry_single_derivation(
        conn,
        source_id=str(payload.get("source_id") or ""),
        sync_batch_id=str(payload.get("sync_batch_id") or "unknown"),
        job_name=job_name,
        record_ids=payload.get("record_ids") or [],
    )
    outcome = result.pop("outcome")
    if outcome == "recovered":
        return {
            "status": "ok",
            "records_created": {job_name: result.get("created", 0)},
            **result,
        }
    if outcome == "skipped":
        # A debt that cannot be re-run is still recorded data loss. Failing the
        # job keeps it visible for a human instead of silently discharging it.
        return {"status": "error", "error": f"cannot retry: {result.get('reason')}"}
    return {"status": "error", "error": str(result.get("error") or "retry failed")}


def pending_derivation_summary(conn: Optional[sqlite3.Connection]) -> Dict[str, Any]:
    """What derived data this node KNOWS it is missing, and since when it knew.

    Deliberately not a health score. An earlier version of this returned
    ``healthy: not pending``, which claims far more than the data supports: it
    reads as "your data is complete" when it only means "nothing failed while
    this mechanism was watching". The loss that motivated this whole module has
    no record here at all, because the recording did not exist when it happened.

    So the contract is narrow and every caller gets the caveats inline:

      known_gaps       failures actually recorded — never "you have everything"
      recording_since  before this, failures were invisible (None = unknown)
      covers           which stage this speaks for, and which it does not

    A UI that renders `known_gaps: false` as a green tick is misreading it; the
    fields are named so that misreading takes effort.
    """
    pending = list_pending_derivation_retries(conn, limit=1000)
    by_job: Dict[str, int] = {}
    by_source: Dict[str, int] = {}
    records = 0
    for item in pending:
        by_job[item["job_name"]] = by_job.get(item["job_name"], 0) + 1
        by_source[item["source_id"]] = by_source.get(item["source_id"], 0) + 1
        records += int(item.get("record_count") or 0)
    return {
        "pending_derivations": len(pending),
        "affected_records": records,
        "by_job": by_job,
        "by_source": by_source,
        "known_gaps": bool(pending),
        "recording_since": ensure_recording_since(conn),
        "covers": DEBT_COVERAGE,
    }
