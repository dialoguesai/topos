"""Resume coding-session derivation independently of transcript byte cursors.

1.5.10 could commit canonical rows and advance its cursor before derivation
finished. A normal incremental sync never sees those rows again. Queue one
bounded, idempotent batch per enabled source; completion queues the next.
Payloads hold record ids only, and execution rechecks the current source and
share-exclusion policy. Model failures leave the batch visibly failed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

KIND = "ai_tool_enrichment_catchup"
BATCH_SIZE = 256


def enqueue_catchup(conn: Any, source_id: str | None = None, retry_failed: bool = False) -> list[str]:
    from .ai_tool_sync import AI_TOOL_SOURCE_IDS, source_state
    from ..enrichment.models.mvp_defaults import job_spec_version
    from ..enrichment.source_overrides import effective_canonical_enrichment_jobs
    from ..pipeline.job_store import enqueue_job
    from ..sources.registry import REGISTRY

    queued = []
    for sid in ([source_id] if source_id else AI_TOOL_SOURCE_IDS):
        if sid not in AI_TOOL_SOURCE_IDS:
            continue
        state = source_state(conn, sid)
        if not state.get("enabled") or state.get("paused"):
            continue
        # Let ordinary ingest finish first. Its record markers then tell us
        # exactly what remains, without duplicate model work.
        active = conn.execute(
            "SELECT 1 FROM pipeline_jobs WHERE source_id=? AND status IN ('queued','running') "
            "AND kind IN (?, 'inbox_deferred_enrichment') LIMIT 1", (sid, KIND)
        ).fetchone()
        if active:
            continue
        if retry_failed:
            failed = conn.execute(
                "SELECT job_id FROM pipeline_jobs WHERE source_id=? AND kind=? AND status='failed' "
                "ORDER BY created_at LIMIT 1", (sid, KIND)
            ).fetchone()
            if failed:
                from ..pipeline.job_store import requeue_job
                requeue_job(conn, str(failed[0]))
                queued.append(str(failed[0]))
                continue
        definition = REGISTRY.get(sid)
        jobs = [*effective_canonical_enrichment_jobs(definition), "ai_tool_pipeline"]
        missing: dict[str, list[str]] = {}
        ids: set[str] = set()
        for job in jobs:
            authored = job in (definition.authored_only_jobs or [])
            rows = conn.execute(
                "SELECT m.message_id FROM ai_chat_messages m WHERE m.source_id=? "
                "AND COALESCE(m.share_excluded,0)=0 "
                + ("AND m.sender_type='human' " if authored else "")
                + "AND NOT EXISTS (SELECT 1 FROM enrichment_record_progress p "
                "WHERE p.source_id=m.source_id AND p.record_id=m.message_id "
                "AND p.job_id=? AND p.spec_version>=?) ORDER BY m.message_id LIMIT ?",
                (sid, job, job_spec_version(job), BATCH_SIZE),
            ).fetchall()
            missing[job] = [str(row[0]) for row in rows]
            ids.update(missing[job])
        if not ids:
            continue
        # A shared cap keeps a many-job debt bounded too.
        selected = set(sorted(ids)[:BATCH_SIZE])
        missing = {job: [rid for rid in rows if rid in selected]
                   for job, rows in missing.items() if any(rid in selected for rid in rows)}
        versions = {job: job_spec_version(job) for job in missing}
        digest = hashlib.sha256(json.dumps([missing, versions], sort_keys=True).encode()).hexdigest()[:24]
        queued.append(enqueue_job(
            conn, kind=KIND, source_id=sid,
            idempotency_key=f"{KIND}:{sid}:{digest}",
            payload={"source_id": sid, "missing": missing},
        ))
    return queued


async def execute_catchup(payload: dict) -> dict:
    from .ai_tool_sync import AI_TOOL_SOURCE_IDS, source_state
    from .canonical_pipeline import run_post_canonical_pipeline
    from ..core.state import get_db_connection
    from ..enrichment.orchestrator import EnrichmentOrchestrator
    from ..sources.registry import REGISTRY

    sid = str(payload.get("source_id") or "")
    if sid not in AI_TOOL_SOURCE_IDS:
        return {"status": "error", "error": "Not a coding-session source"}
    definition = REGISTRY.get(sid)
    from ..enrichment.source_overrides import effective_canonical_enrichment_jobs
    current_jobs = set(effective_canonical_enrichment_jobs(definition))

    def load():
        conn = get_db_connection()
        state = source_state(conn, sid)
        if not state.get("enabled") or state.get("paused"):
            return None
        ids = sorted({str(rid) for batch in (payload.get("missing") or {}).values() for rid in batch})
        if not ids or len(ids) > BATCH_SIZE:
            raise ValueError("Invalid catch-up batch size")
        cursor = conn.execute(
            "SELECT * FROM ai_chat_messages WHERE source_id=? AND COALESCE(share_excluded,0)=0 "
            "AND message_id IN (" + ",".join("?" for _ in ids) + ")", [sid, *ids]
        )
        keys = [c[0] for c in cursor.description]
        return [{**dict(zip(keys, row)), "_table": "ai_chat_messages"} for row in cursor.fetchall()]

    records = await asyncio.to_thread(load)
    if records is None:
        return {"status": "ok", "skipped": "source disabled or paused"}
    by_id = {str(r.get("message_id") or r.get("record_id") or ""): r for r in records}
    orchestrator = EnrichmentOrchestrator()
    errors = []
    processed: set[str] = set()

    def progress(processed_count, total_count, job_name, job_percent, current_job_progress):
        update = payload.get("_progress_updater")
        if update:
            update({"status": "processing", "messages_processed": processed_count,
                    "messages_total": total_count, "current_job_name": job_name,
                    "current_job_progress_percent": current_job_progress})

    for job, ids in (payload.get("missing") or {}).items():
        batch = [by_id[rid] for rid in ids if rid in by_id]
        if not batch:
            continue
        if job != "ai_tool_pipeline" and job not in current_jobs:
            continue
        if job != "ai_tool_pipeline":
            result = await orchestrator.run_canonical(batch, job_names=[job], progress_callback=progress)
            errors.extend(result.get("errors") or [])
        processed.update(str(r.get("message_id") or r.get("record_id")) for r in batch)
    if errors:
        return {"status": "error", "errors": errors, "error": str(errors[:3])}
    batch = [by_id[rid] for rid in sorted(processed)]
    if batch:
        from ..sources.canonical_signal_defaults import resolved_signal_derivation_jobs
        from ..enrichment.source_overrides import effective_canonical_enrichment_jobs
        canonical_jobs = set(effective_canonical_enrichment_jobs(definition))
        signal_jobs = [j for j in resolved_signal_derivation_jobs(definition) if j not in canonical_jobs]
        result = await run_post_canonical_pipeline(
            source_def=definition, canonical_records=batch, job_names=signal_jobs,
            sync_batch_id=f"catchup:{sid}:{hashlib.sha256(''.join(sorted(processed)).encode()).hexdigest()[:16]}",
            run_enrichment=False, enrichment_progress=progress,
        )
        for lane in ("signal_derivation", "privacy_disclosure_layer"):
            lane_result = result.get(lane) or {}
            errors.extend(lane_result.get("errors") or [])
            errors.extend(f"Deferred signal job: {j}" for j in lane_result.get("deferred_jobs") or [])
        if not errors:
            from ..enrichment.record_progress import mark_records_processed_in_a_worker
            await mark_records_processed_in_a_worker(
                get_db_connection, sid, "ai_tool_pipeline", batch, 1
            )
    return {"status": "error" if errors else "ok", "messages_processed": len(processed),
            "errors": errors, **({"error": str(errors[:3])} if errors else {})}


def processing_status(conn: Any, source_id: str) -> dict:
    """Counts of completed runs, rather than counts of nonempty model output."""
    from ..sources.registry import REGISTRY
    from ..enrichment.models.mvp_defaults import job_spec_version
    from ..enrichment.source_overrides import effective_canonical_enrichment_jobs

    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ai_chat_messages'").fetchone():
        return {"jobs": []}
    definition = REGISTRY.get(source_id)
    if definition is None:
        return {"jobs": []}
    out = []
    for job in [*effective_canonical_enrichment_jobs(definition), "ai_tool_pipeline"]:
        authored = job in (definition.authored_only_jobs or [])
        row = conn.execute(
            "SELECT COUNT(*),SUM(EXISTS(SELECT 1 FROM enrichment_record_progress p "
            "WHERE p.source_id=m.source_id AND p.record_id=m.message_id AND p.job_id=? "
            "AND p.spec_version>=?)) FROM ai_chat_messages m WHERE source_id=? "
            "AND COALESCE(share_excluded,0)=0 " + ("AND sender_type='human'" if authored else ""),
            (job, job_spec_version(job), source_id),
        ).fetchone()
        eligible, done = int(row[0] or 0), int(row[1] or 0)
        out.append({"job_id": job, "eligible_records": eligible, "processed_records": done,
                    "pending_records": eligible - done})
    return {"jobs": out}
