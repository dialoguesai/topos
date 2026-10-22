"""Embed stored rows that their source's ingest lane never embedded.

One queue, one lane per reason the rows went unembedded, each started only on
the owner's word by its own upgrade target (a ``consent: prompt`` step):

- ``local_sync_embeddings`` (iMessage, Signal). A local-sync batch used to run
  only its source's canonical jobs, and ``embeddings`` is a signal-lane job, so
  no synced message reached the vector index (see
  ``local_sync.SYNC_TIME_SIGNAL_JOBS``). Sync now embeds each batch; this
  drains what was stored before, and anything a failed batch left.
- ``imported_ai_chat_embeddings`` (the ChatGPT export and the ChatGPT capture).
  An import stopped mid-run (the owner restarting the node for a newer build)
  keeps the canonical rows it committed, and nothing derives them afterwards:
  a month on, one node's stopped September imports had left 2,619 rows with
  no vector and no marker. Only rows the owner shares are read
  (``share_excluded = 0``). A row whose vector is already stored is not owed,
  whatever its marker says: the ``embeddings`` spec bump to 2 retired the old
  completion witness and kept the vectors, so re-embedding a row the index
  already holds costs model time and changes nothing.

The target queues the first bounded batch of record ids per source, and each
batch's completion queues the next. From then on the source is enrolled, so
worker start and every finished sync or import of it also pick up what is
owed, including rows a failed batch left. What is owed is read from
``enrichment_record_progress``, which the canonical orchestrator writes only
after a batch's vectors are committed, so a restart, a stopped node or a failed
batch resumes at the first row still owed. Payloads hold record ids only.
Execution rereads the rows and rechecks the source's current jobs, so a row
flagged since, or an owner who detaches ``embeddings`` from a source, stops at
the next batch. Each lane has a switch (``TOPOS_LOCAL_SYNC_CATCHUP``,
``TOPOS_IMPORTED_AI_CHAT_CATCHUP``): off, the next batch ends without work and
nothing new is queued until the switch is back on and the node restarts.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Optional

JOB = "embeddings"
#: One batch is one model load and one marker write. Embeddings alone assign no
#: topics, so the coding catch-up's 128-record topic limit does not apply.
#: Measured on synthetic rows shaped like one node's imported-chat backlog
#: (2.7k characters, 1.5 chunks a row): 18-24 s a batch, the same rows/s as 200.
BATCH_SIZE = 500
MAX_BATCH_SIZE = 2000
_OFF_VALUES = ("0", "false", "off", "no")


def _attached_at_sync(source_id: str) -> bool:
    from .local_sync import sync_time_jobs
    from ..sources.registry import REGISTRY

    definition = REGISTRY.get(source_id)
    return definition is not None and JOB in sync_time_jobs(definition)


def _attached_at_import(source_id: str) -> bool:
    """Run in the canonical lane, as at import; an override that drops it from either lane stops it."""
    from ..enrichment.source_overrides import effective_canonical_enrichment_jobs
    from ..sources.canonical_signal_defaults import resolved_signal_derivation_jobs
    from ..sources.registry import REGISTRY

    definition = REGISTRY.get(source_id)
    return (definition is not None and JOB in effective_canonical_enrichment_jobs(definition)
            and JOB in resolved_signal_derivation_jobs(definition))


@dataclass(frozen=True)
class Lane:
    target: str
    #: pipeline_jobs kind. A queued row outlives the build that wrote it, so a kind is never renamed.
    kind: str
    source_ids: tuple[str, ...]
    table: str
    switch: str
    #: The source's own ingest, which goes first; a finished one continues an enrolled catch-up.
    ingest_kinds: tuple[str, ...]
    attached: Callable[[str], bool]
    #: Columns handed to the job, as its ingest hands them; None hands the whole row.
    columns: Optional[tuple[str, ...]]
    #: A row with a stored vector is done whatever its marker says (see above). The local-sync
    #: lane keeps the marker-only rule its 1.5.15 upgrade step declares.
    stored_vector_is_done: bool

    def enabled(self) -> bool:
        return os.environ.get(self.switch, "on").strip().lower() not in _OFF_VALUES


LOCAL_SYNC = Lane(
    target="local_sync_embeddings", kind="local_sync_embeddings_catchup",
    source_ids=("imessage", "signal"), table="conversation_messages",
    switch="TOPOS_LOCAL_SYNC_CATCHUP", ingest_kinds=("local_sync",), attached=_attached_at_sync,
    columns=("message_id", "conversation_id", "sender_type", "sender_id", "reply_to_message_id",
             "message_type", "event_type", "event_at", "content"),
    stored_vector_is_done=False,
)
IMPORTED_AI_CHAT = Lane(
    target="imported_ai_chat_embeddings", kind="imported_ai_chat_embeddings_catchup",
    source_ids=("chatgpt_file_ingestion", "chatgpt_ui_conversation"), table="ai_chat_messages",
    switch="TOPOS_IMPORTED_AI_CHAT_CATCHUP",
    ingest_kinds=("file_ingestion", "enrichment_process_source", "inbox_deferred_enrichment"),
    attached=_attached_at_import, columns=None, stored_vector_is_done=True,
)
LANES = (LOCAL_SYNC, IMPORTED_AI_CHAT)
CATCHUP_KINDS = tuple(lane.kind for lane in LANES)
#: Finished jobs of these kinds queue an enrolled source's next batch.
CONTINUING_KINDS = frozenset(CATCHUP_KINDS + tuple(k for lane in LANES for k in lane.ingest_kinds))


def _lane_for(source_id: str) -> Optional[Lane]:
    return next((lane for lane in LANES if source_id in lane.source_ids), None)


def _table_present(conn: Any, lane: Lane) -> bool:
    # Neither table is created by migrations: a node that never synced or imported such a source owes nothing.
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (lane.table,)
    ).fetchone() is not None


def _owed_sql(conn: Any, lane: Lane, select: str, tail: str = "") -> str:
    from ..storage.share_excluded import share_excluded_clause

    stored = (" AND NOT EXISTS (SELECT 1 FROM signal_embeddings e WHERE e.record_id=m.message_id "
              "AND e.vector_blob IS NOT NULL)") if lane.stored_vector_is_done else ""
    return (
        f"SELECT {select} FROM {lane.table} m WHERE m.source_id=?"
        f"{share_excluded_clause(conn, alias='m', table=lane.table)} "
        "AND NOT EXISTS (SELECT 1 FROM enrichment_record_progress p "
        "WHERE p.source_id=m.source_id AND p.job_id=? AND p.record_id=m.message_id "
        f"AND p.spec_version>=?){stored}{tail}"
    )


def processing_status(conn: Any, source_id: str) -> dict:
    """Counts of rows embedded and still owed, of the rows this lane may read. Counts only, never text."""
    from ..enrichment.models.mvp_defaults import job_spec_version
    from ..storage.share_excluded import share_excluded_clause

    lane = _lane_for(source_id)
    if lane is None:
        raise ValueError(f"{source_id!r} has no embeddings catch-up")
    if not _table_present(conn, lane):
        return {"source_id": source_id, "job_id": JOB, "eligible_records": 0,
                "processed_records": 0, "pending_records": 0}
    total = conn.execute(
        f"SELECT COUNT(*) FROM {lane.table} m WHERE m.source_id=?"
        + share_excluded_clause(conn, alias="m", table=lane.table), (source_id,)
    ).fetchone()[0]
    pending = conn.execute(
        _owed_sql(conn, lane, "COUNT(*)"), (source_id, JOB, job_spec_version(JOB))
    ).fetchone()[0]
    return {"source_id": source_id, "job_id": JOB, "eligible_records": int(total or 0),
            "processed_records": int(total or 0) - int(pending or 0),
            "pending_records": int(pending or 0)}


def _enrolled(conn: Any, lane: Lane, source_id: str) -> bool:
    """True once the owner has started this source's catch-up (pipeline_jobs rows are never pruned)."""
    return conn.execute(
        "SELECT 1 FROM pipeline_jobs WHERE kind=? AND source_id=? LIMIT 1", (lane.kind, source_id)
    ).fetchone() is not None


def enqueue_catchup(conn: Any, source_id: str | None = None, *, start: str | None = None) -> list[str]:
    """Queue the next owed batch for each enrolled source with nothing of its own in flight.

    ``start`` names an upgrade target and enrolls that lane's sources; only the target passes it.
    """
    from ..enrichment.models.mvp_defaults import job_spec_version
    from ..pipeline.job_store import enqueue_job

    queued = []
    for lane in LANES:
        if (start and start != lane.target) or not lane.enabled() or not _table_present(conn, lane):
            continue
        for sid in lane.source_ids:
            if (source_id and sid != source_id) or not lane.attached(sid):
                continue
            if not start and not _enrolled(conn, lane, sid):
                continue
            # The source's own ingest embeds its batches; one catch-up batch at a time.
            kinds = (lane.kind, *lane.ingest_kinds)
            active = conn.execute(
                "SELECT 1 FROM pipeline_jobs WHERE source_id=? AND status IN ('queued','running') "
                f"AND kind IN ({','.join('?' for _ in kinds)}) LIMIT 1", (sid, *kinds)
            ).fetchone()
            if active:
                continue
            version = job_spec_version(JOB)
            # Newest first: recent messages are the likeliest to be asked about.
            ids = [str(row[0]) for row in conn.execute(
                _owed_sql(conn, lane, "m.message_id", " ORDER BY m.event_at DESC, m.message_id LIMIT ?"),
                (sid, JOB, version, BATCH_SIZE),
            ).fetchall()]
            if not ids:
                continue
            digest = hashlib.sha256(json.dumps([sid, version, ids]).encode()).hexdigest()[:24]
            queued.append(enqueue_job(
                conn, kind=lane.kind, source_id=sid, idempotency_key=f"{lane.kind}:{sid}:{digest}",
                payload={"source_id": sid, "record_ids": ids, "spec_version": version},
            ))
    return queued


def start_catchup(conn: Any, target: str) -> dict:
    """An upgrade target: enroll its lane's sources and queue each one's first batch. Counts only."""
    lane = next(lane for lane in LANES if lane.target == target)
    queued = enqueue_catchup(conn, start=target)
    return {"queued_batches": len(queued),
            "pending_records": {sid: processing_status(conn, sid)["pending_records"] for sid in lane.source_ids}}


def _load_owed(conn: Any, lane: Lane, sid: str, ids: list[str]) -> list[dict]:
    """The batch's rows still owed, with the fields the source's ingest hands the same job."""
    from ..enrichment.models.mvp_defaults import job_spec_version

    select = ", ".join(f"m.{c}" for c in lane.columns) if lane.columns else "m.*"
    cursor = conn.execute(
        _owed_sql(conn, lane, select, " AND m.message_id IN (" + ",".join("?" for _ in ids) + ")"),
        [sid, JOB, job_spec_version(JOB), *ids],
    )
    keys = [c[0] for c in cursor.description]
    out = []
    for row in cursor.fetchall():
        record = dict(zip(keys, row))
        out.append({**record, "ts": record["event_at"], "source_id": sid, "_table": lane.table})
    return out


async def execute_catchup(payload: dict) -> dict:
    from ..core.state import get_db_connection
    from ..enrichment.models.mvp_defaults import job_spec_version
    from ..enrichment.orchestrator import EnrichmentOrchestrator
    from ..enrichment.record_progress import processed_record_ids

    sid = str(payload.get("source_id") or "")
    lane = _lane_for(sid)
    if lane is None:
        return {"status": "error", "error": "Not a source with an embeddings catch-up"}
    if not lane.enabled():
        # Done without work: discovery is off too, so nothing follows this batch.
        return {"status": "ok", "skipped": f"{lane.switch} is off"}
    ids = [str(rid) for rid in (payload.get("record_ids") or []) if rid]
    if not ids or len(ids) > MAX_BATCH_SIZE:
        return {"status": "error", "error": "Invalid catch-up batch size"}

    def load() -> list[dict] | None:
        if not lane.attached(sid):
            return None
        return _load_owed(get_db_connection(), lane, sid, ids)

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
