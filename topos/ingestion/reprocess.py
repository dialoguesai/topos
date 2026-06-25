"""Reprocess ingestion from raw or canonical stage without re-upload."""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Literal, Optional

from ..core.state import get_db_connection
from ..pipeline.audit import SQLiteIngestAuditStore, StageAuditRow, stage_context
from ..pipeline.stages import PipelineStage
from ..sources.registry import REGISTRY
from .canonical_pipeline import canonicalize_normalized_batch, load_canonical_records_for_signal, run_post_canonical_pipeline
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
        return {
            "records_created": 0,
            "records_updated": 0,
            "records_unchanged": 0,
            "canonical_records": [],
        }

    parser_cls = PARSER_REGISTRY.get(source_def.parser_id or source_def.schema_id)
    if parser_cls is None:
        return {
            "records_created": 0,
            "records_updated": 0,
            "records_unchanged": len(records),
            "canonical_records": [],
        }

    parser = parser_cls(dataset_id=dataset_id, _schema_id=source_def.schema_id)
    normalized_records = []
    for payload in records:
        record_id = str(payload.get("id") or payload.get("record_id") or uuid.uuid4())
        raw = RawRecord(record_id=record_id, payload=payload)
        validation = parser.validate(raw)
        if not validation.is_valid:
            continue
        normalized_records.append(parser.parse(raw))

    conn = get_db_connection()
    if not conn:
        raise RuntimeError("Database connection not available for reprocess")

    before = _count_canonical_rows(conn, source_def)
    canon_result = canonicalize_normalized_batch(
        conn,
        source_def,
        normalized_records,
        dataset_id=dataset_id,
        sync_batch_id=sync_batch_id,
    )
    created = max(
        canon_result.events_created,
        canon_result.messages_created,
        len(canon_result.canonical_records),
    )

    after = _count_canonical_rows(conn, source_def)
    net_new = max(after - before, created)
    unchanged = max(len(records) - net_new, 0)
    return {
        "records_created": net_new if net_new > 0 else created,
        "records_updated": 0,
        "records_unchanged": unchanged,
        "canonical_records": canon_result.canonical_records,
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
    canonical_records: List[Dict[str, Any]] = []

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
            canonical_records = list(counts.get("canonical_records") or [])
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
            canonical_records = list(counts.get("canonical_records") or [])

    if not canonical_records:
        canonical_records = load_canonical_records_for_signal(conn, source_def)

    from ..sources.canonical_signal_defaults import resolved_signal_derivation_jobs

    signal_status = "skipped"
    signal_records_out = 0
    if canonical_records and resolved_signal_derivation_jobs(source_def):
        with stage_context(
            audit,
            stage=PipelineStage.SIGNAL_DERIVE,
            sync_batch_id=batch_id,
            source_id=source_id,
            records_in=len(canonical_records),
        ) as signal_outcome:
            stages.append(PipelineStage.SIGNAL_DERIVE.value)
            pipeline_outcome = await run_post_canonical_pipeline(
                source_def=source_def,
                canonical_records=canonical_records,
                sync_batch_id=batch_id,
                run_enrichment=True,
            )
            derive_result = pipeline_outcome.get("signal_derivation") or {}
            signal_status = "completed" if derive_result.get("jobs_run") else "deferred"
            if derive_result.get("deferred_jobs"):
                signal_status = "deferred"
            signal_records_out = sum((derive_result.get("records_created") or {}).values())
            signal_outcome["records_out"] = signal_records_out
    else:
        audit.append_stage(
            StageAuditRow(
                sync_batch_id=batch_id,
                source_id=source_id,
                stage=PipelineStage.SIGNAL_DERIVE,
                status="skipped",
                records_out=0,
            )
        )
        stages.append(PipelineStage.SIGNAL_DERIVE.value)

    return {
        "status": "accepted",
        "sync_batch_id": batch_id,
        "from_stage": from_stage,
        "stages": stages,
        "signal_derive_status": signal_status,
        "signal_records_out": signal_records_out,
        **{k: counts[k] for k in ("records_created", "records_updated", "records_unchanged")},
    }
