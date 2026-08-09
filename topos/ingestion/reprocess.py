"""Reprocess ingestion from raw or canonical stage without re-upload."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Any, Dict, List, Literal, Optional

from ..core.state import get_db_connection
from ..pipeline.audit import SQLiteIngestAuditStore, StageAuditRow, stage_context
from ..pipeline.stages import PipelineStage
from ..sources.registry import REGISTRY
from .canonical_pipeline import (
    canonicalize_normalized_batch,
    load_canonical_records_for_signal,
    run_post_canonical_pipeline,
)
from .parsers import PARSER_REGISTRY
from .sources.base import RawRecord

FromStage = Literal["raw", "canonical"]

logger = logging.getLogger("topos.ingestion.reprocess")

# Order matters: prefer the source's declared type, then common legacy names.
_DEFAULT_RAW_SOURCE_TYPES = ("ui_stream", "chat_messages", "events")


def _resolve_source_def(source_id: str):
    """Return a registry source, rehydrating runtime installs when needed."""
    sid = str(source_id or "").strip()
    if not sid:
        raise ValueError("source_id required")
    source_def = REGISTRY.get(sid)
    if source_def is not None:
        return source_def
    try:
        from ..sources.install_service import rehydrate_active_installs_runtime

        rehydrate_active_installs_runtime(source_id=sid)
    except Exception as exc:  # noqa: BLE001
        logger.debug("rehydrate for reprocess skipped: %s", exc)
    source_def = REGISTRY.get(sid)
    if source_def is None:
        raise ValueError(f"Unknown source_id: {sid}")
    return source_def


def _count_canonical_rows(conn, source_def) -> int:
    group = getattr(source_def, "canonical_group_id", None)
    source_id = source_def.source_id
    if group == "conversations":
        row = conn.execute(
            "SELECT COUNT(*) FROM conversation_messages WHERE source_id=?",
            (source_id,),
        ).fetchone()
        return int(row[0]) if row else 0
    if group == "activity":
        row = conn.execute(
            "SELECT COUNT(*) FROM activity_events WHERE source_id=?",
            (source_id,),
        ).fetchone()
        return int(row[0]) if row else 0
    if group == "journal":
        row = conn.execute(
            "SELECT COUNT(*) FROM journal_entries WHERE source_id=?",
            (source_id,),
        ).fetchone()
        return int(row[0]) if row else 0
    row = conn.execute(
        "SELECT COUNT(*) FROM ai_chat_messages WHERE source_id=?",
        (source_id,),
    ).fetchone()
    return int(row[0]) if row else 0


def _raw_source_types_for(source_def: Any) -> List[str]:
    declared = str(getattr(source_def, "source_type", None) or "").strip()
    ordered: List[str] = []
    if declared and declared not in {"file", "local_sync", "stub", "derived"}:
        ordered.append(declared)
    for candidate in _DEFAULT_RAW_SOURCE_TYPES:
        if candidate not in ordered:
            ordered.append(candidate)
    # file uploads historically land in chat_messages raw tables
    if "chat_messages" not in ordered:
        ordered.append("chat_messages")
    return ordered


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return bool(row)


def _count_source_rows(conn: sqlite3.Connection, table_name: str, source_id: str) -> int:
    try:
        row = conn.execute(
            f'SELECT COUNT(*) FROM "{table_name}" WHERE source_system=?',
            (source_id,),
        ).fetchone()
        return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def resolve_raw_table_name(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    source_def: Any = None,
) -> Optional[str]:
    """Pick the raw retention table that actually holds this source's rows."""
    from ..storage.raw.raw_tables_manager import RawTablesManager

    manager = RawTablesManager(conn)
    candidates: List[str] = []
    for source_type in _raw_source_types_for(source_def):
        name = manager.get_raw_table_name(source_id, source_type)
        if name not in candidates:
            candidates.append(name)

    # Prefer an existing candidate that already has rows for this source.
    for table_name in candidates:
        if _table_exists(conn, table_name) and _count_source_rows(conn, table_name, source_id) > 0:
            return table_name

    # Fall back to any existing candidate (empty table is still a valid target).
    for table_name in candidates:
        if _table_exists(conn, table_name):
            return table_name

    # Last resort: scan raw_* tables for matching source_system rows.
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'raw_%' ORDER BY name"
        ).fetchall()
    except sqlite3.Error:
        return None
    for (name,) in rows:
        table_name = str(name)
        if _count_source_rows(conn, table_name, source_id) > 0:
            return table_name
    return None


def _load_raw_records(
    conn: sqlite3.Connection,
    source_id: str,
    *,
    source_def: Any = None,
    limit: Optional[int] = None,
) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """Load raw payloads for a source. Returns (records, table_name_used)."""
    table_name = resolve_raw_table_name(conn, source_id=source_id, source_def=source_def)
    if not table_name:
        return [], None

    params: List[Any] = [source_id]
    sql = (
        f'SELECT source_record_id, payload_json FROM "{table_name}" '
        "WHERE source_system=? ORDER BY created_at DESC, source_record_id DESC"
    )
    if limit is not None:
        lim = max(0, int(limit))
        if lim == 0:
            return [], table_name
        sql += " LIMIT ?"
        params.append(lim)

    rows = conn.execute(sql, tuple(params)).fetchall()
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
    # Process oldest→newest within the selected window so dependent ordering stays natural.
    out.reverse()
    return out, table_name


async def _remap_records(
    *,
    source_def,
    dataset_id: str,
    records: List[Dict[str, Any]],
    sync_batch_id: str,
) -> Dict[str, Any]:
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
            logger.debug(
                "reprocess skip invalid raw record source_id=%s record_id=%s errors=%s",
                source_def.source_id,
                record_id,
                getattr(validation, "errors", None),
            )
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
    limit: Optional[int] = None,
    run_enrichment: bool = True,
) -> Dict[str, Any]:
    """Re-run raw→canonical (and optionally enrichment/signal) for a source.

    ``limit`` selects the newest N raw rows (by ``created_at``). Useful for
    recovering missed canonicalization without replaying an entire source.
    """
    source_def = _resolve_source_def(source_id)

    conn = get_db_connection()
    if conn is None:
        raise RuntimeError("Database connection not available for reprocess")

    batch_id = sync_batch_id or str(uuid.uuid4())
    audit = SQLiteIngestAuditStore(conn)
    stages: List[str] = []
    counts = {"records_created": 0, "records_updated": 0, "records_unchanged": 0}
    canonical_records: List[Dict[str, Any]] = []

    _ = force  # reserved for future forced remap

    raw_records, raw_table = _load_raw_records(
        conn,
        source_def.source_id,
        source_def=source_def,
        limit=limit,
    )
    logger.info(
        "reprocess load source_id=%s table=%s raw_rows=%s limit=%s from_stage=%s",
        source_def.source_id,
        raw_table,
        len(raw_records),
        limit,
        from_stage,
    )

    if from_stage == "raw":
        with stage_context(
            audit,
            stage=PipelineStage.RAW_WRITE,
            sync_batch_id=batch_id,
            source_id=source_def.source_id,
            records_in=len(raw_records),
        ) as raw_outcome:
            stages.append(PipelineStage.RAW_WRITE.value)
            raw_outcome["records_out"] = len(raw_records)

        with stage_context(
            audit,
            stage=PipelineStage.CANONICAL_MAP,
            sync_batch_id=batch_id,
            source_id=source_def.source_id,
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
            source_id=source_def.source_id,
            records_in=len(raw_records),
        ) as map_outcome:
            stages.append(PipelineStage.CANONICAL_MAP.value)
            # Canonical-stage reprocess still remaps from raw when present so
            # mappers stay the source of truth; otherwise load existing rows.
            if raw_records:
                counts = await _remap_records(
                    source_def=source_def,
                    dataset_id=dataset_id,
                    records=raw_records,
                    sync_batch_id=batch_id,
                )
                map_outcome["records_out"] = counts["records_created"]
                canonical_records = list(counts.get("canonical_records") or [])
            else:
                canonical_records = load_canonical_records_for_signal(conn, source_def)
                if limit is not None:
                    canonical_records = canonical_records[: max(0, int(limit))]
                map_outcome["records_out"] = len(canonical_records)
                counts = {
                    "records_created": 0,
                    "records_updated": 0,
                    "records_unchanged": len(canonical_records),
                    "canonical_records": canonical_records,
                }

    # Do not fall back to "enrich everything" when raw→canonical found nothing.
    # That path previously hung UI-stream recoveries after a wrong-table miss.

    from ..sources.canonical_signal_defaults import resolved_signal_derivation_jobs

    signal_status = "skipped"
    signal_records_out = 0
    if (
        run_enrichment
        and canonical_records
        and resolved_signal_derivation_jobs(source_def)
    ):
        with stage_context(
            audit,
            stage=PipelineStage.SIGNAL_DERIVE,
            sync_batch_id=batch_id,
            source_id=source_def.source_id,
            records_in=len(canonical_records),
        ) as signal_outcome:
            stages.append(PipelineStage.SIGNAL_DERIVE.value)
            pipeline_outcome = await run_post_canonical_pipeline(
                source_def=source_def,
                canonical_records=canonical_records,
                sync_batch_id=batch_id,
                run_enrichment=True,
                # Reprocess is a deliberate owner action: run the signal lane
                # even for manual-trigger sources.
                force_signal=True,
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
                source_id=source_def.source_id,
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
        "source_id": source_def.source_id,
        "raw_table": raw_table,
        "raw_rows_loaded": len(raw_records),
        "limit": limit,
        "stages": stages,
        "signal_derive_status": signal_status,
        "signal_records_out": signal_records_out,
        **{k: counts[k] for k in ("records_created", "records_updated", "records_unchanged")},
    }
