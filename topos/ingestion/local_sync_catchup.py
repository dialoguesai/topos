"""Embed the local-sync messages that were stored before the sync lane embedded them.

A local-sync batch (iMessage, Signal) used to run only its source's canonical
jobs, and ``embeddings`` is a signal-lane job, so no synced message reached the
vector index (see ``local_sync.SYNC_TIME_SIGNAL_JOBS``). Sync now embeds each
batch; this drains what was stored before, and anything a failed batch left.

It starts on the owner's word: the ``local_sync_embeddings`` upgrade target
(a ``consent: prompt`` step) queues the first bounded batch of record ids per
source, and each batch's completion queues the next. From then on the source is
enrolled, so worker start and every finished sync also pick up what is owed,
including rows a failed sync-time batch left. What is owed is read from
``enrichment_record_progress``, which the canonical orchestrator writes only
after a batch's vectors are committed, so a restart, a stopped node or a failed
batch resumes at the first row still owed. Payloads hold record ids only.
Execution rechecks the source's current jobs, so an owner who detaches
``embeddings`` from a source stops the queue at the next batch.
``TOPOS_LOCAL_SYNC_CATCHUP=off`` pauses it: the next batch ends without work and
nothing new is queued until the switch is back on and the node restarts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from typing import Any

KIND = "local_sync_embeddings_catchup"
JOB = "embeddings"
#: Sources whose local-sync lane writes conversation_messages.
SOURCE_IDS = ("imessage", "signal")
#: One batch is one model load and one marker write. Embeddings alone assign no
#: topics, so the coding catch-up's 128-record topic limit does not apply.
BATCH_SIZE = 500
MAX_BATCH_SIZE = 2000
_OFF_VALUES = ("0", "false", "off", "no")


def catchup_enabled() -> bool:
    return os.environ.get("TOPOS_LOCAL_SYNC_CATCHUP", "on").strip().lower() not in _OFF_VALUES


def _embeddings_attached(source_id: str) -> bool:
    from .local_sync import sync_time_jobs
    from ..sources.registry import REGISTRY

    definition = REGISTRY.get(source_id)
    return definition is not None and JOB in sync_time_jobs(definition)


def _owed_sql(select: str, tail: str = "") -> str:
    return (
        f"SELECT {select} FROM conversation_messages m WHERE m.source_id=? "
        "AND NOT EXISTS (SELECT 1 FROM enrichment_record_progress p "
        "WHERE p.source_id=m.source_id AND p.job_id=? AND p.record_id=m.message_id "
        f"AND p.spec_version>=?){tail}"
    )


def processing_status(conn: Any, source_id: str) -> dict:
    """Counts of messages embedded and still owed. Counts only, never text."""
    from ..enrichment.models.mvp_defaults import job_spec_version

    total = conn.execute(
        "SELECT COUNT(*) FROM conversation_messages WHERE source_id=?", (source_id,)
    ).fetchone()[0]
    pending = conn.execute(
        _owed_sql("COUNT(*)"), (source_id, JOB, job_spec_version(JOB))
    ).fetchone()[0]
    return {"source_id": source_id, "job_id": JOB, "eligible_records": int(total or 0),
            "processed_records": int(total or 0) - int(pending or 0),
            "pending_records": int(pending or 0)}


def _enrolled(conn: Any, source_id: str) -> bool:
    """True once the owner has started this source's catch-up (pipeline_jobs rows are never pruned)."""
    return conn.execute(
        "SELECT 1 FROM pipeline_jobs WHERE kind=? AND source_id=? LIMIT 1", (KIND, source_id)
    ).fetchone() is not None


def enqueue_catchup(conn: Any, source_id: str | None = None, *, start: bool = False) -> list[str]:
    """Queue the next owed batch for each enrolled source with nothing of its own in flight.

    ``start`` enrolls a source; only the upgrade target passes it.
    """
    from ..enrichment.models.mvp_defaults import job_spec_version
    from ..pipeline.job_store import enqueue_job

    if not catchup_enabled():
        return []
    queued = []
    for sid in ([source_id] if source_id else SOURCE_IDS):
        if sid not in SOURCE_IDS or not _embeddings_attached(sid):
            continue
        if not start and not _enrolled(conn, sid):
            continue
        # A running sync embeds its own batches; one catch-up batch at a time.
        active = conn.execute(
            "SELECT 1 FROM pipeline_jobs WHERE source_id=? AND status IN ('queued','running') "
            "AND kind IN (?, 'local_sync') LIMIT 1", (sid, KIND)
        ).fetchone()
        if active:
            continue
        version = job_spec_version(JOB)
        # Newest first: recent messages are the likeliest to be asked about.
        ids = [str(row[0]) for row in conn.execute(
            _owed_sql("m.message_id", " ORDER BY m.event_at DESC, m.message_id LIMIT ?"),
            (sid, JOB, version, BATCH_SIZE),
        ).fetchall()]
        if not ids:
            continue
        digest = hashlib.sha256(json.dumps([sid, version, ids]).encode()).hexdigest()[:24]
        queued.append(enqueue_job(
            conn, kind=KIND, source_id=sid, idempotency_key=f"{KIND}:{sid}:{digest}",
            payload={"source_id": sid, "record_ids": ids, "spec_version": version},
        ))
    return queued


def start_catchup(conn: Any) -> dict:
    """The upgrade target: enroll every local-sync source and queue its first batch. Counts only."""
    queued = enqueue_catchup(conn, start=True)
    return {"queued_batches": len(queued),
            "pending_records": {sid: processing_status(conn, sid)["pending_records"] for sid in SOURCE_IDS}}


def _load_owed(conn: Any, sid: str, ids: list[str]) -> list[dict]:
    """The batch's rows still owed, with the fields a sync batch hands the same job."""
    from ..enrichment.models.mvp_defaults import job_spec_version
    from ..enrichment.record_progress import processed_record_ids

    done = processed_record_ids(conn, sid, JOB, job_spec_version(JOB))
    owed = [rid for rid in ids if rid not in done]
    if not owed:
        return []
    keys = ("message_id", "conversation_id", "sender_type", "sender_id", "reply_to_message_id",
            "message_type", "event_type", "event_at", "content")
    rows = conn.execute(
        f"SELECT {', '.join(keys)} FROM conversation_messages WHERE source_id=? AND message_id IN ("
        + ",".join("?" for _ in owed) + ")", [sid, *owed],
    ).fetchall()
    out = []
    for row in rows:
        record = dict(zip(keys, row))
        out.append({**record, "ts": record["event_at"], "source_id": sid,
                    "_table": "conversation_messages"})
    return out


async def execute_catchup(payload: dict) -> dict:
    from ..core.state import get_db_connection
    from ..enrichment.models.mvp_defaults import job_spec_version
    from ..enrichment.orchestrator import EnrichmentOrchestrator
    from ..enrichment.record_progress import processed_record_ids

    sid = str(payload.get("source_id") or "")
    if sid not in SOURCE_IDS:
        return {"status": "error", "error": "Not a local-sync source"}
    if not catchup_enabled():
        # Done without work: discovery is off too, so nothing follows this batch.
        return {"status": "ok", "skipped": "TOPOS_LOCAL_SYNC_CATCHUP is off"}
    ids = [str(rid) for rid in (payload.get("record_ids") or []) if rid]
    if not ids or len(ids) > MAX_BATCH_SIZE:
        return {"status": "error", "error": "Invalid catch-up batch size"}

    def load() -> list[dict] | None:
        if not _embeddings_attached(sid):
            return None
        return _load_owed(get_db_connection(), sid, ids)

    records = await asyncio.to_thread(load)
    if records is None:
        return {"status": "ok", "skipped": "embeddings detached from source"}
    if not records:
        return {"status": "ok", "messages_processed": 0}

    def progress(processed_count, total_count, job_name, job_percent, current_job_progress):
        update = payload.get("_progress_updater")
        if update:
            update({"status": "processing", "messages_processed": processed_count,
                    "messages_total": total_count, "current_job_name": job_name,
                    "current_job_progress_percent": current_job_progress})

    result = await EnrichmentOrchestrator().run_canonical(records, job_names=[JOB], progress_callback=progress)
    errors = list(result.get("errors") or [])
    if not errors:
        # Done must mean marked: an unmarked row would be rediscovered under
        # this same batch key, which enqueue_job answers with the finished job,
        # and the queue would stop there without a word.
        def unmarked() -> int:
            done = processed_record_ids(get_db_connection(), sid, JOB, job_spec_version(JOB))
            return sum(1 for r in records if str(r["message_id"]) not in done)

        missing = await asyncio.to_thread(unmarked)
        if missing:
            errors.append(f"{missing} records ran without a progress marker")
    out = {"status": "error" if errors else "ok", "messages_processed": len(records),
           "records_created": result.get("records_created") or {}, "errors": errors}
    if errors:
        out["error"] = str(errors[:3])
    return out
