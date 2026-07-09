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


def _bump_generation_after_canonicalize(db_conn, source_id: str, result: "CanonicalizeResult") -> None:
    if not db_conn or not source_id:
        return
    if not (
        result.canonical_records
        or result.messages_created
        or result.events_created
        or result.conversations_created
    ):
        return
    try:
        from ..query.source_generation import bump_source_generation

        bump_source_generation(db_conn, source_id)
    except Exception as exc:
        logger.debug("source generation bump skipped: %s", exc)


def _prepare_signal_record(record: Dict[str, Any]) -> Dict[str, Any]:
    from ..enrichment.jobs.canonical.brief_fallback import prepare_signal_record

    return prepare_signal_record(record)


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
    if normalized_payload.get("sender_id") is not None:
        staging["sender_id"] = normalized_payload.get("sender_id")
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

    def _finish() -> CanonicalizeResult:
        _bump_generation_after_canonicalize(db_conn, source_id, result)
        return result

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
                        # Table stamp: without it these records are
                        # key-for-key identical to ai_chat records (both say
                        # sender_type='human'), so downstream attribution
                        # (stats tabling, fact-extraction owner gate) cannot
                        # tell the families apart.
                        "_table": "conversation_messages",
                    }
                )
        except Exception as exc:
            logger.error("[PIPELINE:CANONICAL] conversations upsert failed: %s", exc, exc_info=True)
            result.errors.append({"step": "conversations", "error": str(exc)})
        return _finish()

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
                        _prepare_signal_record(
                            activity_payload_to_signal_record(canonical_payload, source_id=source_id)
                        )
                    )
        except Exception as exc:
            logger.error("[PIPELINE:CANONICAL] activity upsert failed: %s", exc, exc_info=True)
            result.errors.append({"step": "activity", "error": str(exc)})
        return _finish()

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
                for msg in mapped:
                    if not isinstance(msg, dict):
                        continue
                    # Table stamp (see conversations branch): canonical
                    # ai_chat rows say sender_type='human' for the owner and
                    # are shape-identical to conversation records otherwise.
                    msg.setdefault("_table", "ai_chat_messages")
                    result.canonical_records.append(msg)
            errors = canonical_result.get("errors")
            if isinstance(errors, list):
                result.errors.extend(errors)
        except Exception as exc:
            logger.error("[PIPELINE:CANONICAL] ai_chat upsert failed: %s", exc, exc_info=True)
            result.errors.append({"step": "ai_chat", "error": str(exc)})
        return _finish()

    demo_table_by_group = {
        "schedule": "calendar_events",
        "journal": "journal_entries",
        "profile": "profile_records",
        "financial": "financial_transactions",
        "places": "location_events",
    }
    if group in demo_table_by_group and source_def.canonical_mapper_id:
        table_name = demo_table_by_group[group]
        try:
            from ..canonicalization.mappers import MAPPER_REGISTRY
            from ..storage.canonical.canonical_store import SQLiteCanonicalStore

            mapper_cls = MAPPER_REGISTRY.get(source_def.canonical_mapper_id)
            mapper = mapper_cls() if mapper_cls else None
            store = SQLiteCanonicalStore(db_conn)
            created = 0
            for payload in payloads:
                norm = NormalizedRecord(record_id=str(payload.get("record_id") or payload.get("event_id") or ""), payload=payload)
                canonical_payload = dict(mapper.map(norm).payload if mapper else payload)
                canonical_payload["source_id"] = source_id
                ref = store.upsert(table_name, canonical_payload, sync_batch_id=sync_batch_id)
                if ref.created:
                    created += 1
                signal_record = _prepare_signal_record(dict(canonical_payload))
                signal_record["source_id"] = source_id
                if table_name == "calendar_events":
                    result.events_created += 1
                result.canonical_records.append(signal_record)
                if table_name == "journal_entries" and str(canonical_payload.get("place_name") or "").strip():
                    from .journal_location_fanout import (
                        journal_location_event_from_entry,
                        journal_location_signal_record,
                    )

                    loc_row = journal_location_event_from_entry(canonical_payload, source_id=source_id)
                    if loc_row:
                        store.upsert("location_events", loc_row, sync_batch_id=sync_batch_id)
                        result.events_created += 1
                        result.canonical_records.append(
                            _prepare_signal_record(journal_location_signal_record(loc_row))
                        )
            result.messages_created = created
        except Exception as exc:
            logger.error("[PIPELINE:CANONICAL] demo %s upsert failed: %s", group, exc, exc_info=True)
            result.errors.append({"step": group, "error": str(exc)})
        return _finish()

    if group == "contacts" and source_def.canonical_mapper_id:
        try:
            from ..canonicalization.mappers import MAPPER_REGISTRY
            from ..storage.canonical.conversations_tables import ensure_contacts_table, ensure_contact_identifiers_table

            ensure_contacts_table(db_conn)
            ensure_contact_identifiers_table(db_conn)
            mapper_cls = MAPPER_REGISTRY.get(source_def.canonical_mapper_id)
            mapper = mapper_cls() if mapper_cls else None
            for payload in payloads:
                norm = NormalizedRecord(
                    record_id=str(payload.get("contact_id") or payload.get("record_id") or ""),
                    payload=payload,
                )
                mapped = dict(mapper.map(norm).payload if mapper else payload)
                contact_id = str(mapped.get("contact_id") or "")
                display_name = str(mapped.get("display_name") or "")
                identifier = str(mapped.get("identifier") or "")
                identifier_type = str(mapped.get("identifier_type") or "email")
                source_record_id = str(mapped.get("source_record_id") or f"{contact_id}:{identifier}")
                if not contact_id or not identifier:
                    continue
                db_conn.execute(
                    """
                    INSERT INTO contacts (
                        contact_id, dataset_id, source_id, display_name, is_self,
                        source_record_id, ingested_at, sync_batch_id
                    ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)
                    ON CONFLICT(contact_id) DO UPDATE SET
                        display_name=excluded.display_name,
                        sync_batch_id=excluded.sync_batch_id,
                        ingested_at=excluded.ingested_at,
                        source_record_id=excluded.source_record_id
                    """,
                    (
                        contact_id,
                        dataset_id,
                        source_id,
                        display_name,
                        1 if contact_id == "contact-self" else 0,
                        source_record_id,
                        sync_batch_id,
                    ),
                )
                db_conn.execute(
                    """
                    INSERT INTO contact_identifiers (
                        dataset_id, source_id, identifier, identifier_type, contact_id,
                        source_record_id, ingested_at, sync_batch_id
                    ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'), ?)
                    ON CONFLICT(dataset_id, source_id, identifier) DO UPDATE SET
                        contact_id=excluded.contact_id,
                        sync_batch_id=excluded.sync_batch_id,
                        source_record_id=excluded.source_record_id
                    """,
                    (
                        dataset_id,
                        source_id,
                        identifier,
                        identifier_type,
                        contact_id,
                        source_record_id,
                        sync_batch_id,
                    ),
                )
                result.canonical_records.append(
                    _prepare_signal_record(
                        {
                            "contact_id": contact_id,
                            "display_name": display_name,
                            "identifier": identifier,
                            "identifier_type": identifier_type,
                            "source_id": source_id,
                        }
                    )
                )
            db_conn.commit()
            result.messages_created = len(result.canonical_records)
        except Exception as exc:
            logger.error("[PIPELINE:CANONICAL] contacts upsert failed: %s", exc, exc_info=True)
            result.errors.append({"step": "contacts", "error": str(exc)})
        return _finish()

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
    return _finish()


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

    demo_load_sql = {
        "schedule": (
            "SELECT event_id, title, starts_at, ends_at, source_id FROM calendar_events WHERE source_id=? ORDER BY starts_at DESC LIMIT ?",
            lambda row: {
                "event_id": row[0],
                "title": row[1],
                "starts_at": row[2],
                "ends_at": row[3],
                "source_id": row[4] or source_id,
            },
        ),
        "journal": (
            "SELECT entry_id, entry_at, starts_at, ends_at, mood_tag, category, content, people, place_name, duration, source_id FROM journal_entries WHERE source_id=? ORDER BY entry_at DESC LIMIT ?",
            lambda row: {
                "entry_id": row[0],
                "entry_at": row[1],
                "starts_at": row[2],
                "ends_at": row[3],
                "mood_tag": row[4],
                "category": row[5],
                "content": row[6],
                "people": row[7],
                "place_name": row[8],
                "duration": row[9],
                "source_id": row[10] or source_id,
            },
        ),
        "profile": (
            "SELECT record_id, record_type, title, organization, description, source_id FROM profile_records WHERE source_id=? ORDER BY record_id LIMIT ?",
            lambda row: {
                "record_id": row[0],
                "record_type": row[1],
                "title": row[2],
                "organization": row[3],
                "description": row[4],
                "source_id": row[5] or source_id,
            },
        ),
        "financial": (
            "SELECT transaction_id, account_type, amount, category, description, source_id FROM financial_transactions WHERE source_id=? ORDER BY posted_at DESC LIMIT ?",
            lambda row: {
                "transaction_id": row[0],
                "account_type": row[1],
                "amount": row[2],
                "category": row[3],
                "description": row[4],
                "source_id": row[5] or source_id,
            },
        ),
        "places": (
            "SELECT event_id, place_name, city, region, event_at, source_id FROM location_events WHERE source_id=? ORDER BY event_at DESC LIMIT ?",
            lambda row: {
                "event_id": row[0],
                "place_name": row[1],
                "city": row[2],
                "region": row[3],
                "event_at": row[4],
                "source_id": row[5] or source_id,
            },
        ),
        "contacts": (
            """
            SELECT c.contact_id, c.display_name, ci.identifier, ci.identifier_type, c.source_id
            FROM contacts c
            JOIN contact_identifiers ci ON ci.contact_id = c.contact_id AND ci.source_id = c.source_id
            WHERE c.source_id=?
            ORDER BY c.contact_id
            LIMIT ?
            """,
            lambda row: {
                "contact_id": row[0],
                "display_name": row[1],
                "identifier": row[2],
                "identifier_type": row[3],
                "source_id": row[4] or source_id,
            },
        ),
    }
    if group in demo_load_sql:
        sql, mapper = demo_load_sql[group]
        rows = db_conn.execute(sql, (source_id, limit)).fetchall()
        return [_prepare_signal_record(mapper(row)) for row in rows]

    if group == "ai_messages":
        rows = db_conn.execute(
            """
            SELECT message_id, conversation_id, sender_type, content, event_at, source_id
            FROM ai_chat_messages
            WHERE source_id=?
            ORDER BY event_at DESC
            LIMIT ?
            """,
            (source_id, limit),
        ).fetchall()
        return [
            _prepare_signal_record(
                {
                    "message_id": row[0],
                    "conversation_id": row[1],
                    "sender_type": row[2],
                    "content": row[3],
                    "event_at": row[4],
                    "source_id": row[5] or source_id,
                }
            )
            for row in rows
        ]

    rows = db_conn.execute(
        """
        SELECT message_id, conversation_id, sender_type, content, event_at, source_id
        FROM ai_chat_messages
        WHERE source_id=?
        ORDER BY event_at DESC
        LIMIT ?
        """,
        (source_id, limit),
    ).fetchall()
    return [
        _prepare_signal_record(
            {
                "message_id": row[0],
                "conversation_id": row[1],
                "sender_type": row[2],
                "content": row[3],
                "event_at": row[4],
                "source_id": row[5] or source_id,
            }
        )
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
    force_signal: bool = False,
    enrichment_records: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run privacy disclosure, canonical enrichment, then signal derivation.

    ``enrichment_trigger="manual"`` gates BOTH ML lanes (canonical enrichment
    and signal derivation) on the automatic ingest path. The privacy
    disclosure layer always runs. Deliberate invocations (manual process,
    backfill, reprocess) either pass explicit ``job_names`` or set
    ``force_signal=True`` to run the signal lane regardless of trigger.
    """
    from ..enrichment.derived_tables import DerivedTablesManager
    from ..enrichment.jobs.canonical.url_classification_core import merge_url_classification_into_records
    from ..enrichment.orchestrator import EnrichmentOrchestrator, SignalDerivationOrchestrator

    outcome: Dict[str, Any] = {
        "signal_derivation": None,
        "canonical_enrichment": None,
        "privacy_disclosure_layer": None,
    }
    if not canonical_records or not source_def:
        return outcome

    source_id = source_def.source_id
    from ..enrichment.source_overrides import effective_canonical_enrichment_jobs
    from ..sources.canonical_signal_defaults import resolved_signal_derivation_jobs

    signal_jobs = resolved_signal_derivation_jobs(source_def, explicit_jobs=job_names)
    canonical_jobs = effective_canonical_enrichment_jobs(source_def)
    enrichment_trigger = getattr(source_def, "enrichment_trigger", "automatic")
    records_for_signal = list(canonical_records)
    records_for_enrichment = (
        list(enrichment_records) if enrichment_records is not None else list(canonical_records)
    )

    derived = tables_manager or DerivedTablesManager()
    if tables_manager is None and getattr(derived, "conn", None) is None:
        try:
            from ..core.state import get_db_connection

            conn = get_db_connection()
            if conn is not None:
                derived = DerivedTablesManager(conn=conn)
        except Exception:
            pass

    # Platform Privacy Layer — mandatory, not gated by enrichment_trigger
    try:
        from ..core.state import get_db_connection
        from ..disclosure.field_registry import canonical_table_for_group
        from ..disclosure.privacy_layer import run_privacy_disclosure_layer

        conn = get_db_connection()
        canon_table = canonical_table_for_group(getattr(source_def, "canonical_group_id", None))
        if conn and canon_table and canonical_records:
            for rec in canonical_records:
                rec.setdefault("_table", canon_table)
            outcome["privacy_disclosure_layer"] = await run_privacy_disclosure_layer(
                conn,
                canonical_records,
                source_group=getattr(source_def, "canonical_group_id", None),
            )
    except Exception as exc:
        logger.error("[PIPELINE:PRIVACY] privacy_disclosure_layer failed: %s", exc, exc_info=True)
        outcome["privacy_disclosure_layer"] = {"errors": [str(exc)]}

    if (
        run_enrichment
        and canonical_jobs
        and enrichment_trigger == "automatic"
        and records_for_enrichment
    ):
        try:
            from ..disclosure.field_registry import canonical_table_for_group

            canon_table = canonical_table_for_group(getattr(source_def, "canonical_group_id", None))
            if canon_table:
                for rec in records_for_enrichment:
                    rec.setdefault("_table", canon_table)
            from ..engine import Engine

            enrichment_orchestrator = EnrichmentOrchestrator(tables_manager=derived, engine=Engine())
            outcome["canonical_enrichment"] = await enrichment_orchestrator.run_canonical(
                records_for_enrichment,
                job_names=canonical_jobs,
            )
            if "url_classification" in canonical_jobs:
                from ..core.state import get_db_connection

                conn = get_db_connection()
                if conn:
                    classified_rows = []
                    for rec in records_for_enrichment:
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
        logger.debug(
            "[PIPELINE:ENRICHMENT] Skipping canonical enrichment (manual trigger): source_id=%s records=%d",
            source_id,
            len(canonical_records),
        )

    # Signal derivation honors the same manual gate as canonical enrichment,
    # unless the caller deliberately requested it (explicit job_names or
    # force_signal from backfill/reprocess paths).
    signal_gate_open = (
        enrichment_trigger == "automatic" or force_signal or job_names is not None
    )
    if run_signal and signal_jobs and not signal_gate_open:
        logger.debug(
            "[PIPELINE:SIGNAL_DERIVE] Skipping signal derivation (manual trigger): source_id=%s records=%d",
            source_id,
            len(records_for_signal),
        )
        outcome["signal_derivation"] = {"skipped": "manual_trigger", "jobs_run": 0}
    elif run_signal and signal_jobs:
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

    from ..engine.pipeline_memory import flush_engine_model_cache_after_pipeline

    flush_engine_model_cache_after_pipeline()
    return outcome
