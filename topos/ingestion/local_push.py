"""One batch from a local pusher, written as the owner's capture app, deduped by ``write_id``.

The control plane's ``app_ingest`` relay (``core/handlers/ingest.py``) writes a
pushed batch in four steps: a prior delivery under the same ``write_id`` answers
at once; each record goes through ``ingest_ui_payload`` with enrichment deferred;
the canonical records are handed to one ``inbox_deferred_enrichment`` job; the
delivery is recorded so the next redelivery dedupes. The local door
(``api/local_pushers.py``) does the same four here, over the node's own dataset,
with the writer the enrolment named. The dedupe table is the relay's
(``usage_inbox_dedupe``), so a batch the control plane already delivered is a
no-op locally and the other way round.

Kept beside the relay handler rather than inside it: the handler reads the
owner, dataset and app out of a relay message the control plane authorised,
this helper reads them out of the node's own identity and a channel-verified
principal. Nothing here reads the writer from a payload.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("topos.ingestion.local_push")


def _install_dataset(owner_user_id: str, source_id: str, dataset_id: str) -> Optional[str]:
    """The install's dataset for an install-bound source (the relay door's ``_install_door_dataset``), else None."""
    try:
        from ..core.state import get_db_connection
        from ..permissions_v2.capture_receipts import door_dataset

        conn = get_db_connection()
        if conn is None:
            return None
        return door_dataset(conn, owner_id=owner_user_id, source_id=source_id, authorised=dataset_id)
    except Exception as exc:  # noqa: BLE001 — provenance bookkeeping never fails an ingest
        logger.warning("[LOCAL_PUSH] install dataset unresolved (%s); recording the node's dataset",
                       type(exc).__name__)
        return None


async def ingest_push_batch(
    *,
    source_def: Any,
    records: List[Any],
    owner_user_id: str,
    dataset_id: str,
    writer_class: str,
    writer_app_id: Optional[str],
    write_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Write ``records`` for ``source_def`` and return the relay's response shape.

    ``{"write_id", "records_processed", "records_total", "errors", "deduplicated"}``;
    ``deduplicated`` is True when a prior delivery under ``write_id`` answered and
    nothing was written. Every database stretch runs off the event-loop thread
    (the write gate is a blocking OS lock), on that thread's own connection.
    """
    from ..ingestion.ingest_helpers import ingest_ui_payload
    from ..ingestion.usage_inbox_dedupe import get_prior_delivery, record_delivery
    from ..permissions_v2.capture_receipts import bound_to_install
    from ..pipeline.job_runner import start_pipeline_worker
    from ..pipeline.job_store import enqueue_job
    import topos.core.handlers as hub

    source_id = str(source_def.source_id)
    wid = str(write_id or "").strip() or None
    if wid:
        prior = await asyncio.to_thread(get_prior_delivery, wid)
        if prior is not None:
            return {
                "write_id": wid,
                "records_processed": int(prior["records_processed"]),
                "records_total": int(prior["records_total"]),
                "errors": [],
                "deduplicated": True,
            }

    writer_dataset_id = None
    if bound_to_install(source_def):
        writer_dataset_id = await asyncio.to_thread(_install_dataset, owner_user_id, source_id, dataset_id)

    schema_id = str(getattr(source_def, "schema_id", "") or "")
    processed = 0
    errors: List[Dict[str, Any]] = []
    contexts: List[dict] = []
    for index, rec in enumerate(records):
        if not isinstance(rec, dict):
            errors.append({"index": index, "error": "record must be a dict"})
            continue
        try:
            result = await ingest_ui_payload(
                dataset_id=dataset_id,
                schema_id=schema_id,
                payload=rec,
                source_id=source_id,
                defer_enrichment=True,
                writer_class=writer_class,
                writer_app_id=writer_app_id,
                writer_dataset_id=writer_dataset_id,
            )
        except Exception as exc:  # noqa: BLE001 — one bad record never fails the batch
            errors.append({"index": index, "error": str(exc)})
            logger.warning("[LOCAL_PUSH] record failed: source_id=%s index=%s", source_id, index, exc_info=True)
            continue
        if result.get("status") == "ok":
            processed += 1
            ctx = result.get("_enrichment_ctx")
            if isinstance(ctx, dict):
                contexts.append(ctx)
        else:
            errors.append({"index": index, "error": str(result.get("error") or "ingest failed")})

    if processed > 0 and contexts:
        merged: list = []
        sync_batch_id = None
        for ctx in contexts:
            merged.extend(ctx.get("canonical_records") or [])
            sync_batch_id = sync_batch_id or ctx.get("sync_batch_id")
        job_payload = {
            "source_id": source_id,
            "sync_batch_id": sync_batch_id,
            "canonical_records": merged,
            "write_id": wid,
        }
        idempotency = f"inbox_derivation:{wid}" if wid else f"local_push:{source_id}:{sync_batch_id}"

        def _enqueue() -> None:
            own = hub.get_db_connection()
            if own is None:
                return
            enqueue_job(
                own,
                kind="inbox_deferred_enrichment",
                payload=job_payload,
                source_id=source_id,
                write_id=wid,
                sync_batch_id=str(sync_batch_id) if sync_batch_id else None,
                idempotency_key=idempotency,
            )

        await asyncio.to_thread(_enqueue)
        start_pipeline_worker(hub.get_db_connection)

    if wid and processed > 0:
        await asyncio.to_thread(record_delivery, wid, records_processed=processed, records_total=len(records))

    return {
        "write_id": wid,
        "records_processed": processed,
        "records_total": len(records),
        "errors": errors,
        "deduplicated": False,
    }
