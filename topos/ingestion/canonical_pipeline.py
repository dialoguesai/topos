"""Shared canonical mapping and post-canonical signal/enrichment stages.

Signal dimensions are derived only from canonical table payloads — never from raw
retention or source flat tables.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Union

from ..ingestion.parsers.base import NormalizedRecord

logger = logging.getLogger("topos.ingestion.canonical_pipeline")


@dataclass
class CanonicalizeResult:
    canonical_records: List[Dict[str, Any]] = field(default_factory=list)
    conversations_created: int = 0
    messages_created: int = 0
    events_created: int = 0
    errors: List[Dict[str, Any]] = field(default_factory=list)


def build_staging_record(
    normalized_payload: Dict[str, Any],
    *,
    dataset_id: str,
    source_id: str,
) -> Dict[str, Any]:
    staging: Dict[str, Any] = {
        "message_id": normalized_payload.get("message_id") or normalized_payload.get("id"),
        "dataset_id": dataset_id,
        "thread_id": normalized_payload.get("thread_id")
        or normalized_payload.get("conversation_id")
        or dataset_id,
        "ts": normalized_payload.get("ts")
        or normalized_payload.get("created_at")
        or normalized_payload.get("visited_at")
        or str(datetime.now(timezone.utc).timestamp()),
        "sender_type": normalized_payload.get("sender_type"),
        "content": normalized_payload.get("content"),
        "source_id": source_id,
    }
    if "_metadata" in normalized_payload:
        staging["_metadata"] = normalized_payload["_metadata"]
    return staging


def activity_payload_to_signal_record(
    canonical_payload: Dict[str, Any],
    *,
    source_id: str,
) -> Dict[str, Any]:
    event_id = canonical_payload.get("event_id") or canonical_payload.get("record_id")
    return {
        "event_id": event_id,
        "record_id": event_id,
        "url": canonical_payload.get("url"),
        "title": canonical_payload.get("title"),
        "activity_type": canonical_payload.get("activity_type"),
        "occurred_at": canonical_payload.get("occurred_at"),
        "source_id": source_id,
    }


def canonicalize_normalized_batch(
    db_conn,
    source_def,
    normalized_records: Sequence[Union[NormalizedRecord, Dict[str, Any]]],
    *,
    dataset_id: str,
    sync_batch_id: str,
) -> CanonicalizeResult:
    """Map normalized ingest records into canonical tables; return signal-ready dicts."""
    if not db_conn or not source_def or not normalized_records:
        return CanonicalizeResult()

    source_id = source_def.source_id
    group = getattr(source_def, "canonical_group_id", None)
    result = CanonicalizeResult()

    payloads: List[Dict[str, Any]] = []
    for item in normalized_records:
        if isinstance(item, NormalizedRecord):
            payloads.append(dict(item.payload))
        elif isinstance(item, dict):
            payloads.append(dict(item))
        else:
            continue

    staging_records = [
        build_staging_record(payload, dataset_id=dataset_id, source_id=source_id)
        for payload in payloads
    ]

    if group == "conversations":
        try:
            from ..storage.canonical import ConversationsTablesManager

            conv_result = ConversationsTablesManager(db_conn).upsert_message_batch(
                staging_records,
                dataset_id,
                source_id,
                sync_batch_id=sync_batch_id,
            )
            result.conversations_created = int(conv_result.get("conversations_created", 0))
            result.messages_created = int(conv_result.get("messages_created", 0))
            for staging in staging_records:
                metadata_json = None
                if "_metadata" in staging:
                    metadata_json = json.dumps(staging["_metadata"])
                result.canonical_records.append(
                    {
                        "message_id": staging.get("message_id"),
                        "conversation_id": staging.get("thread_id")
                        or staging.get("conversation_id")
                        or dataset_id,
                        "sender_type": staging.get("sender_type"),
                        "sender_id": None,
                        "ts": staging.get("ts"),
                        "content": staging.get("content"),
                        "content_rendered": None,
                        "metadata_json": metadata_json,
                        "seq": 0,
                        "source_id": source_id,
                    }
                )
        except Exception as exc:
            logger.error("[PIPELINE:CANONICAL] conversations upsert failed: %s", exc, exc_info=True)
            result.errors.append({"step": "conversations", "error": str(exc)})
        return result

    if group == "activity":
        try:
            from ..canonicalization.mappers import MAPPER_REGISTRY
            from ..storage.canonical.activity_tables import ActivityEventsManager

            mapper_cls = MAPPER_REGISTRY.get(source_def.canonical_mapper_id or "browser_activity")
            mapper = mapper_cls() if mapper_cls else None
            mapped_payloads: List[Dict[str, Any]] = []
            for payload in payloads:
                norm = NormalizedRecord(
                    record_id=str(payload.get("id") or payload.get("record_id") or payload.get("message_id")),
                    payload=payload,
                )
                if mapper:
                    mapped_payloads.append(mapper.map(norm).payload)
            if mapped_payloads:
                batch_result = ActivityEventsManager(db_conn).upsert_batch(
                    mapped_payloads,
                    source_id=source_id,
                    sync_batch_id=sync_batch_id,
                )
                result.events_created = int(batch_result.get("events_created", 0))
                for canonical_payload in mapped_payloads:
                    result.canonical_records.append(
                        activity_payload_to_signal_record(canonical_payload, source_id=source_id)
                    )
        except Exception as exc:
            logger.error("[PIPELINE:CANONICAL] activity upsert failed: %s", exc, exc_info=True)
            result.errors.append({"step": "activity", "error": str(exc)})
        return result

    if source_def.canonical_mapper_id and group == "ai_messages":
        try:
            from ..storage.canonical.ai_chat import CanonicalTablesManager, Canonicalizer

            canonicalizer = Canonicalizer(CanonicalTablesManager(db_conn))
            canonical_result = canonicalizer.canonicalize_staging_batch(
                staging_records,
                source=source_def.canonical_mapper_id,
                batch_size=1000,
                sync_batch_id=sync_batch_id,
                mapping_source_id=source_id,
            )
            result.messages_created = int(canonical_result.get("messages_created", 0))
            result.conversations_created = int(canonical_result.get("conversations_created", 0))
            mapped = canonical_result.get("canonical_messages")
            if isinstance(mapped, list):
                result.canonical_records.extend(msg for msg in mapped if isinstance(msg, dict))
            errors = canonical_result.get("errors")
            if isinstance(errors, list):
                result.errors.extend(errors)
        except Exception as exc:
            logger.error("[PIPELINE:CANONICAL] ai_chat upsert failed: %s", exc, exc_info=True)
            result.errors.append({"step": "ai_chat", "error": str(exc)})
        return result

    if source_def.canonical_mapper_id:
        logger.warning(
            "[PIPELINE:CANONICAL] unsupported canonical_group_id=%s for source_id=%s",
            group,
            source_id,
        )
        result.errors.append(
            {
                "error": f"unsupported canonical_group_id: {group}",
                "source_id": source_id,
            }
        )
    return result


def load_canonical_records_for_signal(
    db_conn,
    source_def,
    *,
    limit: int = 5000,
) -> List[Dict[str, Any]]:
    """Load canonical rows for signal derivation / reprocess backfill."""
    if not db_conn or not source_def:
        return []

    source_id = source_def.source_id
    group = getattr(source_def, "canonical_group_id", None)

    if group == "activity":
        rows = db_conn.execute(
            """
            SELECT event_id, activity_type, url, title, occurred_at, source_id
            FROM activity_events
            WHERE source_id=?
            ORDER BY occurred_at DESC
            LIMIT ?
            """,
            (source_id, limit),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            payload = {
                "event_id": row[0],
                "activity_type": row[1],
                "url": row[2],
                "title": row[3],
                "occurred_at": row[4],
                "source_id": row[5] or source_id,
            }
            out.append(activity_payload_to_signal_record(payload, source_id=source_id))
        return out

    if group == "conversations":
        rows = db_conn.execute(
            """
            SELECT message_id, conversation_id, sender_type, content, ts, source_id
            FROM conversation_messages
            WHERE source_id=?
            ORDER BY ts DESC
            LIMIT ?
            """,
            (source_id, limit),
        ).fetchall()
        return [
            {
                "message_id": row[0],
                "conversation_id": row[1],
                "sender_type": row[2],
                "content": row[3],
                "ts": row[4],
                "source_id": row[5] or source_id,
            }
            for row in rows
        ]

    rows = db_conn.execute(
        """
        SELECT message_id, conversation_id, sender_type, content, ts, source_id
        FROM ai_chat_messages
        WHERE source_id=?
        ORDER BY ts DESC
        LIMIT ?
        """,
        (source_id, limit),
    ).fetchall()
    return [
        {
            "message_id": row[0],
            "conversation_id": row[1],
            "sender_type": row[2],
            "content": row[3],
            "ts": row[4],
            "source_id": row[5] or source_id,
        }
        for row in rows
    ]


async def run_post_canonical_pipeline(
    *,
    source_def,
    canonical_records: List[Dict[str, Any]],
    sync_batch_id: str,
    tables_manager=None,
    job_names: Optional[List[str]] = None,
    run_signal: bool = True,
    run_enrichment: bool = True,
) -> Dict[str, Any]:
    """Run canonical enrichment then signal derivation on canonical payloads."""
    from ..enrichment.derived_tables import DerivedTablesManager
    from ..enrichment.jobs.canonical.url_classification_core import merge_url_classification_into_records
    from ..enrichment.orchestrator import EnrichmentOrchestrator, SignalDerivationOrchestrator

    outcome: Dict[str, Any] = {
        "signal_derivation": None,
        "canonical_enrichment": None,
    }
    if not canonical_records or not source_def:
        return outcome

    source_id = source_def.source_id
    signal_jobs = list(getattr(source_def, "signal_derivation_jobs", []) or [])
    canonical_jobs = list(getattr(source_def, "canonical_enrichment_jobs", []) or [])
    enrichment_trigger = getattr(source_def, "enrichment_trigger", "automatic")
    records_for_signal = list(canonical_records)

    derived = tables_manager or DerivedTablesManager()

    if (
        run_enrichment
        and canonical_jobs
        and enrichment_trigger == "automatic"
        and canonical_records
    ):
        try:
            enrichment_orchestrator = EnrichmentOrchestrator(tables_manager=derived)
            outcome["canonical_enrichment"] = await enrichment_orchestrator.run_canonical(
                canonical_records,
                job_names=canonical_jobs,
            )
            if "url_classification" in canonical_jobs:
                from ..core.state import get_db_connection

                conn = get_db_connection()
                if conn:
                    classified_rows = []
                    for rec in canonical_records:
                        event_id = rec.get("event_id") or rec.get("record_id")
                        if not event_id:
                            continue
                        row = conn.execute(
                            """
                            SELECT url_category, url_confidence, model_name
                            FROM browser_url_classification
                            WHERE enriched_from_table='activity_events' AND record_id=?
                            """,
                            (event_id,),
                        ).fetchone()
                        if row:
                            classified_rows.append(
                                {
                                    "record_id": event_id,
                                    "event_id": event_id,
                                    "category": row[0],
                                    "confidence": row[1],
                                    "model": row[2],
                                }
                            )
                    if classified_rows:
                        records_for_signal = merge_url_classification_into_records(
                            records_for_signal,
                            classified_rows,
                        )
        except Exception as exc:
            logger.error("[PIPELINE:ENRICHMENT] post-canonical failed: %s", exc, exc_info=True)
            outcome["canonical_enrichment"] = {"errors": [str(exc)]}
    elif canonical_jobs and enrichment_trigger == "manual":
        logger.info(
            "[PIPELINE:ENRICHMENT] Skipping canonical enrichment (manual trigger): source_id=%s records=%d",
            source_id,
            len(canonical_records),
        )

    if run_signal and signal_jobs:
        try:
            orchestrator = SignalDerivationOrchestrator(tables_manager=derived)
            outcome["signal_derivation"] = await orchestrator.run_signal_derivation(
                records_for_signal,
                source_id=source_id,
                sync_batch_id=sync_batch_id,
                job_names=job_names,
            )
        except Exception as exc:
            logger.error("[PIPELINE:SIGNAL_DERIVE] post-canonical failed: %s", exc, exc_info=True)
            outcome["signal_derivation"] = {"errors": [str(exc)]}

    return outcome
