"""Reprocess ingestion from raw or canonical stage without re-upload."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Literal, Optional

from ..core.state import get_db_connection
from ..pipeline.audit import SQLiteIngestAuditStore, StageAuditRow, stage_context
from ..pipeline.stages import PipelineStage
from ..pipeline.stub_enqueue import enqueue_signal_derive_stub
from ..sources.registry import REGISTRY
from .parsers import PARSER_REGISTRY
from .sources.base import RawRecord

FromStage = Literal["raw", "canonical"]

logger = logging.getLogger("topos.ingestion.reprocess")


def _count_canonical_rows(conn, source_def) -> int:
    group = getattr(source_def, "canonical_group_id", None)
    if group == "conversations":
        row = conn.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE source_id=?",
            (source_def.source_id,),
        ).fetchone()
        return int(row[0]) if row else 0
    if group == "activity":
        row = conn.execute(
            "SELECT COUNT(*) FROM activity_events WHERE source_id=?",
            (source_def.source_id,),
        ).fetchone()
        return int(row[0]) if row else 0
    row = conn.execute(
        "SELECT COUNT(*) FROM ai_chat_messages WHERE source_id=?",
        (source_def.source_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def _load_raw_records(conn, source_id: str) -> List[Dict[str, Any]]:
    from ..storage.raw.raw_tables_manager import RawTablesManager

    manager = RawTablesManager(conn)
    table_name = manager.get_raw_table_name(source_id)
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone():
        return []
    rows = conn.execute(
        f"SELECT source_record_id, payload_json FROM {table_name} WHERE source_system=?",
        (source_id,),
    ).fetchall()
    out: List[Dict[str, Any]] = []
    for source_record_id, payload_json in rows:
        try:
            payload = json.loads(payload_json) if payload_json else {}
        except json.JSONDecodeError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        payload.setdefault("id", source_record_id)
        payload.setdefault("record_id", source_record_id)
        out.append(payload)
    return out


async def _remap_records(
    *,
    source_def,
    dataset_id: str,
    records: List[Dict[str, Any]],
    sync_batch_id: str,
) -> Dict[str, int]:
    if not records:
        return {"records_created": 0, "records_updated": 0, "records_unchanged": 0}

    parser_cls = PARSER_REGISTRY.get(source_def.parser_id or source_def.schema_id)
    if parser_cls is None:
        return {"records_created": 0, "records_updated": 0, "records_unchanged": len(records)}

    parser = parser_cls(dataset_id=dataset_id, _schema_id=source_def.schema_id)
    staging_records: List[Dict[str, Any]] = []
    for payload in records:
        record_id = str(payload.get("id") or payload.get("record_id") or uuid.uuid4())
        raw = RawRecord(record_id=record_id, payload=payload)
        validation = parser.validate(raw)
        if not validation.is_valid:
            continue
        normalized = parser.parse(raw)
        staging_records.append(
            {
                "message_id": normalized.payload.get("message_id") or record_id,
                "dataset_id": dataset_id,
                "thread_id": normalized.payload.get("thread_id")
                or normalized.payload.get("conversation_id")
                or dataset_id,
                "ts": normalized.payload.get("ts")
                or normalized.payload.get("created_at")
                or normalized.payload.get("visited_at"),
                "sender_type": normalized.payload.get("sender_type"),
                "content": normalized.payload.get("content"),
                "source_id": source_def.source_id,
                **({"_metadata": normalized.payload["_metadata"]} if "_metadata" in normalized.payload else {}),
                **normalized.payload,
            }
        )

    conn = get_db_connection()
    if not conn:
        raise RuntimeError("Database connection not available for reprocess")

    before = _count_canonical_rows(conn, source_def)
    group = getattr(source_def, "canonical_group_id", None)
    created = 0

    if group == "conversations":
        from ..storage.canonical import ConversationsTablesManager

        result = ConversationsTablesManager(conn).upsert_message_batch(
            staging_records,
            dataset_id,
            source_def.source_id,
            sync_batch_id=sync_batch_id,
        )
        created = int(result.get("messages_created", 0))
    elif group == "activity":
        from ..canonicalization.mappers import MAPPER_REGISTRY
        from ..ingestion.parsers.base import NormalizedRecord
        from ..storage.canonical.activity_tables import ActivityEventsManager

        mapper = MAPPER_REGISTRY.get(source_def.canonical_mapper_id or "browser_activity")
        activity_manager = ActivityEventsManager(conn)
        mapped_payloads: List[Dict[str, Any]] = []
        for staging in staging_records:
            norm = NormalizedRecord(
                record_id=str(staging.get("message_id") or staging.get("id")),
                payload=staging,
            )
            if mapper:
                mapped_payloads.append(mapper.map(norm).payload)
        batch_result = activity_manager.upsert_batch(
            mapped_payloads,
            source_id=source_def.source_id,
            sync_batch_id=sync_batch_id,
        )
        created = int(batch_result.get("events_created", 0))
    elif source_def.canonical_mapper_id:
        from ..storage.canonical.ai_chat import CanonicalTablesManager, Canonicalizer

        result = Canonicalizer(CanonicalTablesManager(conn)).canonicalize_staging_batch(
            staging_records,
            source=source_def.canonical_mapper_id,
            sync_batch_id=sync_batch_id,
            mapping_source_id=source_def.source_id,
        )
        created = int(result.get("messages_created", 0))

    after = _count_canonical_rows(conn, source_def)
    net_new = max(after - before, created)
    unchanged = max(len(records) - net_new, 0)
    return {
        "records_created": net_new if net_new > 0 else created,
        "records_updated": 0,
        "records_unchanged": unchanged,
    }


async def reprocess_source(
    *,
    source_id: str,
    dataset_id: str,
    from_stage: FromStage = "raw",
    sync_batch_id: Optional[str] = None,
    force: bool = False,
) -> Dict[str, Any]:
    source_def = REGISTRY.get(source_id)
    if source_def is None:
        raise ValueError(f"Unknown source_id: {source_id}")

    conn = get_db_connection()
    if conn is None:
        raise RuntimeError("Database connection not available for reprocess")

    batch_id = sync_batch_id or str(uuid.uuid4())
    audit = SQLiteIngestAuditStore(conn)
    stages: List[str] = []
    counts = {"records_created": 0, "records_updated": 0, "records_unchanged": 0}

    _ = force  # reserved for future forced remap

    raw_records = _load_raw_records(conn, source_id)

    if from_stage == "raw":
        with stage_context(
            audit,
            stage=PipelineStage.RAW_WRITE,
            sync_batch_id=batch_id,
            source_id=source_id,
            records_in=len(raw_records),
        ) as raw_outcome:
            stages.append(PipelineStage.RAW_WRITE.value)
            raw_outcome["records_out"] = len(raw_records)

        with stage_context(
            audit,
            stage=PipelineStage.CANONICAL_MAP,
            sync_batch_id=batch_id,
            source_id=source_id,
            records_in=len(raw_records),
        ) as map_outcome:
            stages.append(PipelineStage.CANONICAL_MAP.value)
            counts = await _remap_records(
                source_def=source_def,
                dataset_id=dataset_id,
                records=raw_records,
                sync_batch_id=batch_id,
            )
            map_outcome["records_out"] = counts["records_created"]
    else:
        with stage_context(
            audit,
            stage=PipelineStage.CANONICAL_MAP,
            sync_batch_id=batch_id,
            source_id=source_id,
            records_in=len(raw_records),
        ) as map_outcome:
            stages.append(PipelineStage.CANONICAL_MAP.value)
            counts = await _remap_records(
                source_def=source_def,
                dataset_id=dataset_id,
                records=raw_records,
                sync_batch_id=batch_id,
            )
            map_outcome["records_out"] = counts["records_created"]

    enqueue_signal_derive_stub(
        logger,
        source_id=source_id,
        batch_id=batch_id,
        record_ids=[],
        signal_derivation_jobs=list(getattr(source_def, "signal_derivation_jobs", []) or []),
    )
    audit.append_stage(
        StageAuditRow(
            sync_batch_id=batch_id,
            source_id=source_id,
            stage=PipelineStage.SIGNAL_DERIVE,
            status="accepted_stub",
            records_out=0,
        )
    )
    stages.append(PipelineStage.SIGNAL_DERIVE.value)

    return {
        "status": "accepted",
        "sync_batch_id": batch_id,
        "from_stage": from_stage,
        "stages": stages,
        **counts,
    }
