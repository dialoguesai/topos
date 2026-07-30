from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException

from ..auth import require_api_key
from ..enrichment.derived_tables import DerivedTablesManager
from ..enrichment.jobs import CANONICAL_JOBS
from ..enrichment.orchestrator import EnrichmentOrchestrator
from ..sources.registry import REGISTRY
from ..core.state import get_db_connection
# Removed imports: canonicalization.mappers, ingestion.parsers, storage.raw.file_store, analytics.raw_queries
# Enrichment now reads directly from canonical table (ai_chat_messages) per architecture design

logger = logging.getLogger("topos.api.enrichment")

router = APIRouter()


def _url_classification_test_schema() -> Dict[str, Any]:
    return {
        "type": "object",
        "required": ["url"],
        "properties": {
            "url": {
                "type": "string",
                "title": "URL",
                "description": "Website URL to classify",
                "example": "https://www.nytimes.com",
            },
            "title": {
                "type": "string",
                "title": "Page Title",
                "description": "Optional page title for better classification context",
                "example": "The New York Times - Breaking News",
            },
        },
    }


async def _test_browser_visits_url_classification(*, data_packet: Dict[str, Any]) -> Dict[str, Any]:
    from ..engine import Engine, build_url_classification_task

    url = data_packet.get("url")
    title = data_packet.get("title")
    if not isinstance(url, str) or not url.strip():
        raise HTTPException(status_code=400, detail="data_packet.url must be a non-empty string")
    if title is not None and not isinstance(title, str):
        raise HTTPException(status_code=400, detail="data_packet.title must be a string when provided")

    task = build_url_classification_task(
        task_id="test_url_cls",
        url=url.strip(),
        title=title,
    )
    engine = Engine()
    result = await asyncio.to_thread(engine.run, task)
    if result.status != "completed":
        raise HTTPException(
            status_code=502,
            detail=result.error or f"Engine returned status {result.status}",
        )
    return {
        "status": "ok",
        "input": {"url": url, "title": title},
        "output": result.output,
    }


_RAW_SOURCE_TEST_HANDLERS = {
    ("browser_visits", "url_classification"): _test_browser_visits_url_classification,
}

_RAW_SOURCE_TEST_SCHEMAS = {
    ("browser_visits", "url_classification"): _url_classification_test_schema(),
}


async def _backfill_browser_visits_url_classification(
    *,
    db_conn,
    only_missing: bool = True,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Backfill Interests signal facts from canonical activity_events (not raw tables)."""
    import uuid

    from ..ingestion.canonical_pipeline import (
        activity_payload_to_signal_record,
        run_post_canonical_pipeline,
    )
    from ..sources.registry import BROWSER_VISITS

    logger.debug(
        "[PIPELINE:SIGNAL_DERIVE] Browser visits backfill from activity_events only_missing=%s limit=%s",
        only_missing,
        limit,
    )

    table_exists = db_conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='activity_events'"
    ).fetchone()
    if not table_exists:
        return {
            "rows_scanned": 0,
            "rows_processed": 0,
            "rows_skipped": 0,
            "rows_failed": 0,
            "errors": [],
        }

    params: List[Any] = ["browser_visits"]
    query = """
        SELECT event_id, activity_type, url, title, occurred_at, source_id
        FROM activity_events
        WHERE source_id=?
    """
    if only_missing:
        query += """
          AND event_id NOT IN (
            SELECT record_id FROM signal_facts
            WHERE source_id='browser_visits' AND dimension='interests'
          )
        """
    query += " ORDER BY occurred_at ASC"
    if isinstance(limit, int) and limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    rows = db_conn.execute(query, tuple(params)).fetchall()
    canonical_records = [
        activity_payload_to_signal_record(
            {
                "event_id": row[0],
                "activity_type": row[1],
                "url": row[2],
                "title": row[3],
                "occurred_at": row[4],
                "source_id": row[5],
            },
            source_id="browser_visits",
        )
        for row in rows
        if isinstance(row[2], str) and row[2].strip()
    ]

    skipped = max(len(rows) - len(canonical_records), 0)
    if not canonical_records:
        return {
            "rows_scanned": len(rows),
            "rows_processed": 0,
            "rows_skipped": skipped + len(rows),
            "rows_failed": 0,
            "errors": [],
        }

    pipeline_outcome = await run_post_canonical_pipeline(
        source_def=BROWSER_VISITS,
        canonical_records=canonical_records,
        sync_batch_id=f"backfill_browser_visits_{uuid.uuid4().hex[:12]}",
        run_enrichment=False,
        job_names=["url_classification"],
    )
    derive_result = pipeline_outcome.get("signal_derivation") or {}
    processed = sum((derive_result.get("records_created") or {}).values())
    failed = len(derive_result.get("errors") or [])

    summary = {
        "rows_scanned": len(rows),
        "rows_processed": int(processed),
        "rows_skipped": skipped,
        "rows_failed": failed,
        "errors": (derive_result.get("errors") or [])[:100],
    }
    logger.debug(
        "[PIPELINE:SIGNAL_DERIVE] Browser visits backfill complete: scanned=%d processed=%d skipped=%d failed=%d",
        summary["rows_scanned"],
        summary["rows_processed"],
        summary["rows_skipped"],
        summary["rows_failed"],
    )
    return summary


_RAW_SOURCE_BACKFILL_HANDLERS = {
    ("browser_visits", "url_classification"): _backfill_browser_visits_url_classification,
}


# ---------------------------------------------------------------------------
# Unified catalog, coverage, generic backfill, and lifecycle (WS-A)
# ---------------------------------------------------------------------------

# Canonical table + record-id column per canonical_group_id, used for coverage
# totals and generic backfill record loading.
_CANONICAL_TABLE_BY_GROUP: Dict[str, tuple[str, str]] = {
    "ai_messages": ("ai_chat_messages", "message_id"),
    "conversations": ("conversation_messages", "message_id"),
    "activity": ("activity_events", "event_id"),
    "schedule": ("calendar_events", "event_id"),
    "journal": ("journal_entries", "entry_id"),
    "profile": ("profile_records", "record_id"),
    "financial": ("financial_transactions", "transaction_id"),
    "places": ("location_events", "event_id"),
    "contacts": ("contacts", "contact_id"),
}

_RECORD_ID_KEYS = (
    "message_id",
    "record_id",
    "event_id",
    "entry_id",
    "transaction_id",
    "contact_id",
)


def _record_identifier(record: Dict[str, Any]) -> Optional[str]:
    for key in _RECORD_ID_KEYS:
        value = record.get(key)
        if value:
            return str(value)
    return None


def _table_exists(conn, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _table_columns(conn, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row[1]) for row in rows}
    except Exception:
        return set()


def _canonical_table_for_source(source_def) -> Optional[tuple[str, str]]:
    group = str(getattr(source_def, "canonical_group_id", None) or "").strip()
    return _CANONICAL_TABLE_BY_GROUP.get(group)


def _count_canonical_records(conn, source_def) -> Optional[int]:
    table_info = _canonical_table_for_source(source_def)
    if not table_info:
        return None
    table, _id_col = table_info
    if not _table_exists(conn, table):
        return 0
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE source_id=?",
            (source_def.source_id,),
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:
        logger.warning("Coverage count failed for %s: %s", table, exc)
        return None


def _enrichment_catalog_core(source_id: Optional[str] = None) -> Dict[str, Any]:
    """Unified catalog, optionally annotated with a source's configuration."""
    from ..enrichment.catalog import jobs_configured_for_source, list_catalog_entries

    entries = list_catalog_entries()
    payload: Dict[str, Any] = {"status": "ok", "enrichments": entries}
    if source_id:
        source_def = REGISTRY.get(source_id)
        if not source_def:
            raise ValueError(f"Source {source_id} not found")
        lanes = jobs_configured_for_source(source_def)
        enabled: Dict[str, List[str]] = {}
        for lane, jobs in lanes.items():
            for job in jobs:
                enabled.setdefault(job, []).append(lane)
        for entry in entries:
            entry["enabled_lanes"] = enabled.get(entry["job_id"], [])
            entry["enabled"] = bool(entry["enabled_lanes"])
        payload["source_id"] = source_id
        payload["enrichment_trigger"] = getattr(source_def, "enrichment_trigger", "automatic")
    return payload


def _enrichment_coverage_core(source_id: str) -> Dict[str, Any]:
    """Per-job coverage stats for one source: rows enriched vs canonical total."""
    from ..enrichment.catalog import (
        all_jobs_for_source,
        coverage_table_for_job,
        get_catalog_entry,
    )

    source_def = REGISTRY.get(source_id)
    if not source_def:
        raise ValueError(f"Source {source_id} not found")
    conn = get_db_connection()
    if not conn:
        return {"status": "error", "source_id": source_id, "message": "Database connection not available", "jobs": []}

    total = _count_canonical_records(conn, source_def)
    jobs_payload: List[Dict[str, Any]] = []
    for job_id in all_jobs_for_source(source_def):
        entry = get_catalog_entry(job_id)
        job_info: Dict[str, Any] = {
            "job_id": job_id,
            "title": entry.title if entry else job_id,
            "baseline": bool(entry and entry.baseline),
            "output_tables": list(entry.output_tables) if entry else [],
            "enriched_records": None,
            "coverage_percent": None,
            "output_rows": {},
        }
        coverage_table = coverage_table_for_job(job_id)
        if job_id == "timeline":
            from ..features.timeline_projection import timeline_coverage_for_source

            timeline_stats = timeline_coverage_for_source(conn, source_id)
            job_info["enriched_records"] = timeline_stats["enriched_records"]
            job_info["coverage_percent"] = timeline_stats["coverage_percent"]
            job_info["output_rows"]["timeline"] = timeline_stats["enriched_records"]
        elif coverage_table and _table_exists(conn, coverage_table):
            cols = _table_columns(conn, coverage_table)
            id_col = "message_id" if "message_id" in cols else "record_id"
            if "source_id" in cols and id_col in cols:
                try:
                    row = conn.execute(
                        f"SELECT COUNT(DISTINCT {id_col}) FROM {coverage_table} WHERE source_id=?",
                        (source_id,),
                    ).fetchone()
                    enriched = int(row[0]) if row else 0
                    job_info["enriched_records"] = enriched
                    if total:
                        job_info["coverage_percent"] = round(min(100.0, enriched / total * 100.0), 1)
                    elif total == 0:
                        job_info["coverage_percent"] = 0.0
                except Exception as exc:
                    logger.warning("Coverage query failed for %s: %s", coverage_table, exc)
        for table in job_info["output_tables"]:
            if not _table_exists(conn, table):
                continue
            cols = _table_columns(conn, table)
            if "source_id" not in cols:
                continue
            try:
                row = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE source_id=?", (source_id,)
                ).fetchone()
                job_info["output_rows"][table] = int(row[0]) if row else 0
            except Exception:
                continue
        jobs_payload.append(job_info)

    return {
        "status": "ok",
        "source_id": source_id,
        "total_records": total,
        "enrichment_trigger": getattr(source_def, "enrichment_trigger", "automatic"),
        "jobs": jobs_payload,
    }


# Extra WHERE constraints when deleting shared tables so one job's delete does
# not wipe another job's rows in the same table.
_DELETE_CONSTRAINTS: Dict[tuple[str, str], tuple[str, tuple]] = {
    ("url_classification", "signal_facts"): ("dimension = ?", ("interests",)),
    ("statistics", "signal_facts"): ("fact_id LIKE ?", ("stat:%",)),
}


def _delete_enrichment_data_core(source_id: str, job_name: str) -> Dict[str, Any]:
    """Delete one enrichment's output rows for one source (owner lifecycle)."""
    from ..enrichment.catalog import get_catalog_entry

    source_def = REGISTRY.get(source_id)
    if not source_def:
        raise ValueError(f"Source {source_id} not found")
    entry = get_catalog_entry(job_name)
    if not entry:
        raise ValueError(f"Unknown enrichment job: {job_name}")
    if not entry.supports_delete or not entry.output_tables:
        raise ValueError(f"Enrichment '{job_name}' has no deletable output tables")

    conn = get_db_connection()
    if not conn:
        raise RuntimeError("Database connection not available")

    deleted: Dict[str, int] = {}
    for table in entry.output_tables:
        if not _table_exists(conn, table):
            continue
        cols = _table_columns(conn, table)
        if "source_id" not in cols:
            continue
        where = "source_id = ?"
        params: tuple = (source_id,)
        extra = _DELETE_CONSTRAINTS.get((job_name, table))
        if extra:
            where += f" AND {extra[0]}"
            params = params + extra[1]
        try:
            if table == "signal_embeddings":
                ids = [
                    str(row[0])
                    for row in conn.execute(
                        f"SELECT embedding_id FROM signal_embeddings WHERE {where}", params
                    ).fetchall()
                ]
                cur = conn.execute(f"DELETE FROM signal_embeddings WHERE {where}", params)
                try:
                    from ..storage.adapters.sqlite.vector_search import delete_vec_rows

                    delete_vec_rows(conn, ids)
                except Exception as exc:
                    logger.warning("Vector companion cleanup failed: %s", exc)
            else:
                cur = conn.execute(f"DELETE FROM {table} WHERE {where}", params)
            conn.commit()
            deleted[table] = int(cur.rowcount if cur.rowcount is not None else 0)
        except Exception as exc:
            conn.rollback()
            logger.error("Enrichment delete failed for %s.%s: %s", source_id, table, exc)
            raise

    return {
        "status": "ok",
        "source_id": source_id,
        "enrichment_name": job_name,
        "deleted_rows": deleted,
        "deleted_total": sum(deleted.values()),
    }


# Per-record enrichment lookup tables for the preview endpoint: job -> (table,
# value column). All key rows by record_id/message_id and carry source_id.
_PREVIEW_TABLES: List[tuple[str, str, str]] = [
    ("emotions", "message_emotions", "emotion_label"),
    ("topics", "message_topics", "topic"),
    ("sentiment", "message_sentiment", "label"),
    ("entities", "message_entities", "entity_text"),
    ("tags", "signal_tags", "tag"),
    ("goals", "user_goals", "goal_text"),
]


def _enrichment_preview_core(source_id: str, limit: int = 20) -> Dict[str, Any]:
    """Recent canonical records joined with their stored enrichment outputs.

    Powers the 'see the value' surface: each record carries chips (emotions,
    topics, sentiment, entities, tags, goals) from the derived tables.
    """
    from ..ingestion.canonical_pipeline import load_canonical_records_for_signal

    source_def = REGISTRY.get(source_id)
    if not source_def:
        raise ValueError(f"Source {source_id} not found")
    conn = get_db_connection()
    if not conn:
        raise RuntimeError("Database connection not available")

    limit = max(1, min(int(limit or 20), 100))
    records = load_canonical_records_for_signal(conn, source_def, limit=limit)[:limit]
    record_ids = [rid for rid in (_record_identifier(r) for r in records) if rid]
    if not record_ids:
        return {"status": "ok", "source_id": source_id, "records": []}

    placeholders = ",".join("?" * len(record_ids))
    by_record: Dict[str, Dict[str, List[str]]] = {rid: {} for rid in record_ids}
    for kind, table, value_col in _PREVIEW_TABLES:
        if not _table_exists(conn, table):
            continue
        cols = _table_columns(conn, table)
        if value_col not in cols:
            continue
        id_col = "message_id" if "message_id" in cols else "record_id"
        if id_col not in cols:
            continue
        try:
            rows = conn.execute(
                f"SELECT {id_col}, {value_col} FROM {table} "
                f"WHERE {id_col} IN ({placeholders})",
                record_ids,
            ).fetchall()
        except Exception as exc:
            logger.debug("Enrichment preview query failed for %s: %s", table, exc)
            continue
        for rid, value in rows:
            if rid is None or value is None:
                continue
            bucket = by_record.setdefault(str(rid), {})
            values = bucket.setdefault(kind, [])
            text = str(value).strip()
            if text and text not in values and len(values) < 8:
                values.append(text)

    out_records: List[Dict[str, Any]] = []
    for record in records:
        rid = _record_identifier(record)
        if not rid:
            continue
        preview = str(
            record.get("content")
            or record.get("title")
            or record.get("description")
            or record.get("url")
            or ""
        )[:300]
        out_records.append(
            {
                "record_id": rid,
                "preview": preview,
                "event_at": record.get("event_at")
                or record.get("ts")
                or record.get("occurred_at")
                or record.get("entry_at")
                or record.get("starts_at"),
                "enrichments": by_record.get(rid, {}),
            }
        )
    return {"status": "ok", "source_id": source_id, "records": out_records}


async def _generic_backfill_core(
    *,
    source_def,
    job_name: str,
    only_missing: bool = True,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Backfill any catalog enrichment over a source's canonical records.

    Loads canonical rows for the source (all canonical groups supported),
    optionally filters to records missing output for this job, then routes to
    the signal lane (preferred: writes adapter + legacy tables) or the
    canonical lane.
    """
    import uuid as _uuid

    from ..enrichment.catalog import coverage_table_for_job, jobs_configured_for_source
    from ..ingestion.canonical_pipeline import (
        load_canonical_records_for_signal,
        run_post_canonical_pipeline,
    )

    db_conn = get_db_connection()
    if not db_conn:
        raise RuntimeError("Database connection not available")

    source_id = source_def.source_id
    load_limit = limit if isinstance(limit, int) and limit > 0 else 5000
    records = load_canonical_records_for_signal(db_conn, source_def, limit=max(load_limit, 1))
    scanned = len(records)

    if only_missing:
        if job_name == "timeline":
            from ..features.timeline_projection import timeline_coverage_for_source

            stats = timeline_coverage_for_source(db_conn, source_id)
            if stats["missing_records"] == 0:
                return {
                    "rows_scanned": scanned,
                    "rows_processed": 0,
                    "rows_skipped": scanned,
                    "rows_failed": 0,
                    "errors": [],
                }
        else:
            from ..enrichment.catalog import catalog_spec_version

            coverage_table = coverage_table_for_job(job_name)
            if coverage_table and _table_exists(db_conn, coverage_table):
                cols = _table_columns(db_conn, coverage_table)
                id_col = "message_id" if "message_id" in cols else "record_id"
                if "source_id" in cols and id_col in cols:
                    min_spec = catalog_spec_version(job_name)
                    if "spec_version" in cols:
                        done = {
                            str(row[0])
                            for row in db_conn.execute(
                                f"SELECT DISTINCT {id_col} FROM {coverage_table} "
                                f"WHERE source_id=? AND COALESCE(spec_version, 0) >= ?",
                                (source_id, min_spec),
                            ).fetchall()
                            if row[0]
                        }
                    else:
                        done = {
                            str(row[0])
                            for row in db_conn.execute(
                                f"SELECT DISTINCT {id_col} FROM {coverage_table} WHERE source_id=?",
                                (source_id,),
                            ).fetchall()
                            if row[0]
                        }
                    records = [r for r in records if _record_identifier(r) not in done]

    if isinstance(limit, int) and limit > 0:
        records = records[:limit]

    if job_name == "timeline":
        from ..features.timeline_projection import repair_timeline_for_source

        report = repair_timeline_for_source(db_conn, source_id, missing_only=True, dry_run=False)
        processed = int(report.get("totals", {}).get("written", 0))
        return {
            "rows_scanned": scanned,
            "rows_processed": processed,
            "rows_skipped": max(scanned - processed, 0),
            "rows_failed": 0,
            "errors": [],
        }

    if not records:
        return {
            "rows_scanned": scanned,
            "rows_processed": 0,
            "rows_skipped": scanned,
            "rows_failed": 0,
            "errors": [],
        }

    lanes = jobs_configured_for_source(source_def)
    use_signal_lane = job_name in (lanes.get("signal") or [])

    errors: List[Any] = []
    processed = 0
    if use_signal_lane:
        outcome = await run_post_canonical_pipeline(
            source_def=source_def,
            canonical_records=records,
            sync_batch_id=f"backfill_{source_id}_{_uuid.uuid4().hex[:12]}",
            run_enrichment=False,
            force_signal=True,
            job_names=[job_name],
        )
        derive_result = outcome.get("signal_derivation") or {}
        processed = sum((derive_result.get("records_created") or {}).values())
        errors = list(derive_result.get("errors") or [])
    else:
        from ..enrichment.derived_tables import DerivedTablesManager
        from ..enrichment.orchestrator import EnrichmentOrchestrator
        from ..engine import Engine

        orchestrator = EnrichmentOrchestrator(
            tables_manager=DerivedTablesManager(conn=db_conn), engine=Engine()
        )
        result = await orchestrator.run_canonical(records, job_names=[job_name])
        processed = sum((result.get("records_created") or {}).values())
        errors = list(result.get("errors") or [])

    return {
        "rows_scanned": scanned,
        "rows_processed": int(processed),
        "rows_skipped": max(scanned - len(records), 0),
        "rows_failed": len(errors),
        "errors": errors[:100],
    }


def _get_enriched_message_ids(
    table_name: str,
    conn,
    *,
    min_spec_version: Optional[int] = None,
) -> set[str]:
    """Ids that already satisfy coverage for ``table_name``.

    When ``min_spec_version`` is set and the table has ``spec_version``, only
    rows with ``COALESCE(spec_version, 0) >= min_spec_version`` count as done
    (PLAN M3 stale predicate). Otherwise presence-only.
    """
    if not conn:
        return set()
    try:
        cols = _table_columns(conn, table_name)
        if not cols:
            return set()
        if "message_id" in cols and "record_id" in cols:
            id_expr = "COALESCE(message_id, record_id)"
        elif "message_id" in cols:
            id_expr = "message_id"
        elif "record_id" in cols:
            id_expr = "record_id"
        else:
            return set()
        if (
            min_spec_version is not None
            and "spec_version" in cols
            and isinstance(min_spec_version, int)
        ):
            cursor = conn.execute(
                f"SELECT DISTINCT {id_expr} FROM {table_name} "
                f"WHERE COALESCE(spec_version, 0) >= ?",
                (int(min_spec_version),),
            )
        else:
            cursor = conn.execute(f"SELECT DISTINCT {id_expr} FROM {table_name}")
        return {str(row[0]) for row in cursor.fetchall() if row[0]}
    except Exception as e:
        logger.warning("Failed to query enriched message IDs from %s: %s", table_name, e)
        return set()


def _load_canonical_messages_for_enrichment(
    db_conn,
    source_def,
    *,
    dataset_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Load ALL canonical messages for a source, whatever its canonical group.

    The historical reprocess paths read only ai_chat_messages, silently
    no-oping for browser/journal/transcript sources (2026-07-09). This routes
    through load_canonical_records_for_signal (group-aware; carries the
    is_from_self/actor_role identity fields the provenance gates need) and
    stamps ``_table`` from the group mapping so role classification and
    mention provenance see the right canonical table.
    """
    from ..ingestion.canonical_pipeline import load_canonical_records_for_signal

    try:
        records = load_canonical_records_for_signal(db_conn, source_def)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "Canonical load failed for source=%s: %s",
            getattr(source_def, "source_id", "?"),
            exc,
        )
        return []
    table_info = _canonical_table_for_source(source_def)
    table = table_info[0] if table_info else None
    for rec in records:
        if table:
            rec.setdefault("_table", table)
        # Normalize to the shape the canonical jobs consume: EntitiesJob (and
        # friends) key on message_id + content + event_at, but journal/activity
        # loader dicts carry entry_id/event_id and title instead.
        if not rec.get("message_id"):
            rid = _record_identifier(rec)
            if rid:
                rec["message_id"] = rid
        if not rec.get("content"):
            title = rec.get("title")
            if title:
                rec["content"] = str(title)
        if not rec.get("event_at"):
            for key in ("occurred_at", "starts_at", "entry_at", "posted_at", "visited_at"):
                if rec.get(key):
                    rec["event_at"] = rec[key]
                    break
    return records


async def _find_unprocessed_messages(
    source_id: str,
    dataset_id: Optional[str] = None,
    job_names: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Find canonical messages that haven't been enriched yet.

    Reads the source's OWN canonical table (group-aware) as the source of
    truth; the legacy ai_chat_messages read remains as a fallback for chat
    sources whose loader group yields nothing.

    Args:
        source_id: Source identifier
        dataset_id: Optional dataset ID to filter by (extracts user_id for filtering)
        job_names: List of enrichment job names to check

    Returns:
        List of canonical messages that need enrichment
    """
    # Get source definition
    source_def = REGISTRY.get(source_id)
    if not source_def:
        raise ValueError(f"Source {source_id} not found")

    from ..enrichment.source_overrides import effective_canonical_enrichment_jobs

    configured_jobs = effective_canonical_enrichment_jobs(source_def)
    if not configured_jobs:
        return []

    # Determine which jobs to check (default to all canonical enrichment jobs)
    jobs_to_check = job_names or configured_jobs
    
    # Get database connection
    db_conn = get_db_connection()
    if not db_conn:
        logger.warning("No database connection available for enrichment")
        return []

    # Group-aware canonical load — works for EVERY source (browser, journal,
    # transcripts, chat), not just ai_chat ones. The legacy ai_chat_messages
    # read below remains only as a fallback when this yields nothing.
    generic_messages = _load_canonical_messages_for_enrichment(
        db_conn, source_def, dataset_id=dataset_id
    )
    if generic_messages:
        from ..enrichment.catalog import catalog_spec_version

        enriched: Optional[set[str]] = None
        job_table_map = {job.get_job_name(): job.get_derived_table() for job in CANONICAL_JOBS}
        for job_name in jobs_to_check:
            table_name = job_table_map.get(job_name)
            if table_name:
                job_ids = _get_enriched_message_ids(
                    table_name,
                    db_conn,
                    min_spec_version=catalog_spec_version(job_name),
                )
                enriched = job_ids if enriched is None else (enriched & job_ids)
            else:
                logger.warning("Unknown enrichment job: %s (skipping check)", job_name)
        if enriched is None:
            enriched = set()
        unprocessed_generic = [
            msg
            for msg in generic_messages
            if (_record_identifier(msg) or msg.get("message_id")) not in enriched
        ]
        logger.debug(
            "Group-aware loader: %d/%d unprocessed for source_id=%s",
            len(unprocessed_generic),
            len(generic_messages),
            source_id,
        )
        return unprocessed_generic

    # Read canonical messages directly from ai_chat_messages table
    # This is the source of truth per architecture design
    try:
        # Check if ai_chat_messages table exists
        cursor = db_conn.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='ai_chat_messages'
        """)
        if not cursor.fetchone():
            logger.info(
                "ai_chat_messages table does not exist yet. "
                "Wait for ingestion to complete (job status 'completed') before triggering enrichment."
            )
            return []
        
        # Check if ai_chat_conversations table exists for dataset_id filtering
        cursor = db_conn.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='ai_chat_conversations'
        """)
        has_conversations_table = cursor.fetchone() is not None
        
        # Build query to read from canonical table
        # First, check if we have messages for this source_id at all
        msg_count_cursor = db_conn.execute("""
            SELECT COUNT(*) FROM ai_chat_messages WHERE source_id = ?
        """, (source_id,))
        total_msgs = msg_count_cursor.fetchone()[0]
        logger.debug("Debug: Total messages in ai_chat_messages for source_id=%s: %d", source_id, total_msgs)
        
        # Debug: Check what source_ids actually exist in the messages table
        all_sources_cursor = db_conn.execute("""
            SELECT DISTINCT source_id, COUNT(*) as count FROM ai_chat_messages GROUP BY source_id
        """)
        all_sources = [(row[0], row[1]) for row in all_sources_cursor.fetchall()]
        logger.debug("Debug: All source_ids in ai_chat_messages table: %s", all_sources if all_sources else "none")
        
        # Debug: Check total message count regardless of source_id
        total_all_cursor = db_conn.execute("SELECT COUNT(*) FROM ai_chat_messages")
        total_all = total_all_cursor.fetchone()[0]
        logger.debug("Debug: Total messages in ai_chat_messages (all sources): %d", total_all)
        
        if has_conversations_table and dataset_id and total_msgs > 0:
            # Join with conversations table to filter by owner_user_id
            user_id = dataset_id.split(":")[0] if ":" in dataset_id else dataset_id
            logger.debug(
                "Querying canonical messages: source_id=%s, dataset_id=%s, extracted_user_id=%s",
                source_id,
                dataset_id,
                user_id,
            )
            
            # Check what owner_user_ids actually exist for this source
            debug_cursor = db_conn.execute("""
                SELECT DISTINCT c.owner_user_id, COUNT(*) as msg_count
                FROM ai_chat_messages m
                INNER JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                WHERE m.source_id = ?
                GROUP BY c.owner_user_id
            """, (source_id,))
            debug_rows = debug_cursor.fetchall()
            logger.debug(
                "Debug: Found conversations with owner_user_ids: %s",
                [(row[0], row[1]) for row in debug_rows] if debug_rows else "none",
            )
            
            # Check what conversation_ids exist in messages
            conv_cursor = db_conn.execute("""
                SELECT DISTINCT conversation_id FROM ai_chat_messages WHERE source_id = ?
            """, (source_id,))
            conv_ids = [row[0] for row in conv_cursor.fetchall()]
            logger.debug("Debug: Conversation IDs in messages: %s", conv_ids[:5] if conv_ids else "none")
            
            # Check what conversations exist in conversations table
            all_conv_cursor = db_conn.execute("""
                SELECT conversation_id, owner_user_id FROM ai_chat_conversations
            """)
            all_convs = [(row[0], row[1]) for row in all_conv_cursor.fetchall()]
            logger.debug("Debug: All conversations in table: %s", all_convs[:5] if all_convs else "none")
            
            # Try query with user_id filter first
            query = """
                SELECT m.message_id, m.conversation_id, m.sender_type, m.sender_id,
                       m.event_at, m.content, m.content_rendered, m.metadata_json, m.sequence, m.source_id
                FROM ai_chat_messages m
                INNER JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                WHERE m.source_id = ? AND c.owner_user_id = ?
                ORDER BY m.event_at ASC
            """
            cursor = db_conn.execute(query, (source_id, user_id))
            result_count = len(cursor.fetchall())
            logger.debug("Debug: Query with user_id filter returned %d messages", result_count)
            
            # If no results with user_id filter, fall back to source_id only (for local mode)
            if result_count == 0:
                logger.debug("Debug: No messages found with user_id filter, falling back to source_id only")
                query = """
                    SELECT message_id, conversation_id, sender_type, sender_id,
                           event_at, content, content_rendered, metadata_json, sequence, source_id
                    FROM ai_chat_messages
                    WHERE source_id = ?
                    ORDER BY event_at ASC
                """
                cursor = db_conn.execute(query, (source_id,))
            else:
                # Re-execute the query since we consumed the cursor
                cursor = db_conn.execute(query, (source_id, user_id))
        else:
            # Direct query without user filtering (fallback if conversations table doesn't exist or no dataset_id)
            logger.debug(
                "Querying canonical messages without user filter: source_id=%s, has_conversations_table=%s, dataset_id=%s",
                source_id,
                has_conversations_table,
                dataset_id,
            )
            query = """
                SELECT message_id, conversation_id, sender_type, sender_id,
                       event_at, content, content_rendered, metadata_json, sequence, source_id
                FROM ai_chat_messages
                WHERE source_id = ?
                ORDER BY event_at ASC
            """
            cursor = db_conn.execute(query, (source_id,))
        
        # Convert rows to dictionaries
        canonical_messages: List[Dict[str, Any]] = []
        for row in cursor.fetchall():
            canonical_messages.append({
                "message_id": row[0],
                "conversation_id": row[1],
                "sender_type": row[2],
                "sender_id": row[3],
                "event_at": row[4],
                "content": row[5],
                "content_rendered": row[6],
                "metadata_json": row[7],
                "sequence": row[8],
                "source_id": row[9],
                # Table stamp: canonical sender_type here is 'human' (owner),
                # which shape-based classifiers cannot distinguish from
                # conversation_messages rows (messenger 'human' = other
                # people). Loaded from ai_chat_messages, so say so.
                "_table": "ai_chat_messages",
            })
        
        logger.debug(
            "Found %d canonical messages for source_id=%s, dataset_id=%s",
            len(canonical_messages),
            source_id,
            dataset_id,
        )
        
    except Exception as e:
        logger.error("Failed to read canonical messages from ai_chat_messages table: %s", e)
        return []
    
    if not canonical_messages:
        logger.debug("No canonical messages found for source_id=%s, dataset_id=%s", source_id, dataset_id)
        return []
    
    # Check which messages have already been enriched.
    # A message only counts as processed when EVERY checked job has produced a
    # row for it (intersection). The previous union behavior skipped messages
    # that had been enriched by any single job, leaving partial enrichment
    # permanently un-backfillable.
    from ..enrichment.catalog import catalog_spec_version

    enriched_ids: Optional[set[str]] = None
    # Create a mapping from job name to table name using the job registry
    job_to_table = {job.get_job_name(): job.get_derived_table() for job in CANONICAL_JOBS}

    for job_name in jobs_to_check:
        table_name = job_to_table.get(job_name)
        if table_name:
            job_ids = _get_enriched_message_ids(
                table_name,
                db_conn,
                min_spec_version=catalog_spec_version(job_name),
            )
            enriched_ids = job_ids if enriched_ids is None else (enriched_ids & job_ids)
        else:
            logger.warning("Unknown enrichment job: %s (skipping check)", job_name)
    if enriched_ids is None:
        enriched_ids = set()

    # Filter to unprocessed messages
    unprocessed = [
        msg for msg in canonical_messages
        if msg.get("message_id") not in enriched_ids
    ]
    
    logger.debug(
        "Found %d unprocessed messages out of %d total canonical messages for source_id=%s",
        len(unprocessed),
        len(canonical_messages),
        source_id,
    )
    
    return unprocessed


async def _process_enrichment_core(
    source_id: str,
    dataset_id: Optional[str] = None,
    job_names: Optional[List[str]] = None,
    force_reprocess: bool = False,
    include_signal: bool = True,
    progress_updater: Optional[Any] = None,
) -> Dict[str, Any]:
    """Core logic for processing enrichment (reusable from HTTP and WebSocket).
    
    Args:
        source_id: Source identifier
        dataset_id: Optional dataset ID to filter by
        job_names: Optional list of specific enrichment jobs to run
        force_reprocess: If True, reprocess even if already enriched
        
    Returns:
        Processing results
    """
    # Get source definition
    source_def = REGISTRY.get(source_id)
    if not source_def:
        raise ValueError(f"Source {source_id} not found")

    from ..enrichment.source_overrides import effective_canonical_enrichment_jobs

    # Determine which jobs to run
    jobs_to_run = job_names or effective_canonical_enrichment_jobs(source_def)
    if not jobs_to_run:
        return {
            "status": "ok",
            "message": "No enrichment jobs configured for this source",
            "messages_processed": 0,
            "records_created": {},
        }
    
    # Get database connection
    db_conn = get_db_connection()
    if not db_conn:
        return {
            "status": "error",
            "message": "Database connection not available",
            "messages_processed": 0,
            "records_created": {},
        }
    
    # Find unprocessed messages
    if force_reprocess:
        # Load ALL canonical messages regardless of enrichment status, from the
        # source's OWN canonical table. (2026-07-09 fix: this branch previously
        # read only ai_chat_messages, silently returning "No unprocessed
        # messages found" for browser/journal/transcript sources. The
        # group-aware loader also covers ai_messages, so no fallback needed.)
        unprocessed_messages = _load_canonical_messages_for_enrichment(
            db_conn, source_def, dataset_id=dataset_id
        )
    else:
        unprocessed_messages = await _find_unprocessed_messages(source_id, dataset_id, jobs_to_run)
    
    if not unprocessed_messages:
        return {
            "status": "ok",
            "message": "No unprocessed messages found",
            "messages_processed": 0,
            "records_created": {},
        }
    
    # Run enrichment
    tables_manager = DerivedTablesManager(conn=db_conn)
    orchestrator = EnrichmentOrchestrator(tables_manager=tables_manager)
    
    logger.debug(
        "[PIPELINE:ENRICHMENT] %s: Manual enrichment triggered: source_id=%s, messages=%d, jobs=%s",
        orchestrator,
        source_id,
        len(unprocessed_messages),
        jobs_to_run,
    )
    
    # Define progress callback to update progress during execution
    progress_callback = None
    if progress_updater is not None:
        def progress_callback(
            processed_count: int,
            total_count: int,
            job_name: str,
            job_percent: float,
            current_job_progress: float,
        ):
            estimated_messages_processed = int((job_percent / 100) * total_count) if total_count else 0
            jobs_complete = int((job_percent / 100) * len(jobs_to_run)) if jobs_to_run else 0
            progress_updater(
                {
                    "status": "processing",
                    "progress_percent": min(100.0, job_percent),
                    "messages_processed": estimated_messages_processed,
                    "messages_skipped": 0,
                    "messages_total": total_count,
                    "current_job_name": job_name,
                    "current_job_progress_percent": current_job_progress,
                    "jobs_complete": jobs_complete,
                    "jobs_total": len(jobs_to_run),
                    "jobs_progress_percent": (jobs_complete / len(jobs_to_run) * 100) if jobs_to_run else 0.0,
                }
            )
    else:
        try:
            from ..enrichment.progress import get_progress
            progress_obj = get_progress(source_id)
            if progress_obj:
                def progress_callback(
                    processed_count: int,
                    total_count: int,
                    job_name: str,
                    job_percent: float,
                    current_job_progress: float,
                ):
                    estimated_messages_processed = int((job_percent / 100) * total_count)
                    jobs_complete = int((job_percent / 100) * len(jobs_to_run))
                    progress_obj.update(
                        messages_processed=estimated_messages_processed,
                        messages_skipped=0,
                        current_job_name=job_name,
                        current_job_progress_percent=current_job_progress,
                        jobs_complete=jobs_complete,
                        jobs_total=len(jobs_to_run),
                    )
        except Exception:
            pass
    
    if progress_updater is not None:
        progress_updater(
            {
                "status": "processing",
                "messages_total": len(unprocessed_messages),
                "jobs_total": len(jobs_to_run),
            }
        )

    enrichment_result = await orchestrator.run_canonical(
        unprocessed_messages,
        job_names=jobs_to_run,
        progress_callback=progress_callback,
    )

    # Canonical jobs changed the derived layers even when the signal lane is
    # skipped below — mark the graph for a debounced rebuild either way.
    try:
        from ..features.entities.graph_refresh import mark_graph_dirty

        mark_graph_dirty()
    except Exception:  # noqa: BLE001 — refresh must never break enrichment
        pass

    # Manual processing is the deliberate counterpart of the automatic ingest
    # path, so it must also run the signal-derivation lane (embeddings,
    # clusters, facts, ...) that the ingest path skips for manual sources.
    signal_result: Dict[str, Any] = {}
    if include_signal:
        try:
            import uuid as _uuid

            from ..ingestion.canonical_pipeline import run_post_canonical_pipeline
            from ..sources.canonical_signal_defaults import resolved_signal_derivation_jobs

            if resolved_signal_derivation_jobs(source_def):
                pipeline_outcome = await run_post_canonical_pipeline(
                    source_def=source_def,
                    canonical_records=unprocessed_messages,
                    sync_batch_id=f"manual_enrichment_{_uuid.uuid4().hex[:12]}",
                    tables_manager=tables_manager,
                    run_enrichment=False,
                    force_signal=True,
                )
                signal_result = pipeline_outcome.get("signal_derivation") or {}
        except Exception as exc:  # noqa: BLE001
            logger.error("Manual signal derivation failed for %s: %s", source_id, exc, exc_info=True)
            signal_result = {"errors": [str(exc)]}

    return {
        "status": "ok",
        "source_id": source_id,
        "messages_processed": len(unprocessed_messages),
        "jobs_run": enrichment_result.get("jobs_run", 0),
        "records_created": enrichment_result.get("records_created", {}),
        "errors": enrichment_result.get("errors", []),
        "signal_derivation": signal_result,
    }


async def _get_enrichment_status_core(
    source_id: str,
    dataset_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Core logic for getting enrichment status (reusable from HTTP and WebSocket).
    
    This function reads directly from the ai_chat_messages table (canonical table)
    as the source of truth, per the architecture design.
    
    Returns:
        Status information including counts of processed/unprocessed messages
    """
    source_def = REGISTRY.get(source_id)
    if not source_def:
        raise ValueError(f"Source {source_id} not found")
    
    # Get database connection
    db_conn = get_db_connection()
    if not db_conn:
        return {
            "status": "error",
            "source_id": source_id,
            "total_messages": 0,
            "processed_messages": 0,
            "unprocessed_messages": 0,
            "enrichment_jobs": source_def.canonical_enrichment_jobs,
            "enrichment_trigger": getattr(source_def, "enrichment_trigger", "automatic"),
            "message": "Database connection not available",
        }
    
    # Read canonical messages directly from ai_chat_messages table
    try:
        # Check if ai_chat_messages table exists
        cursor = db_conn.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='ai_chat_messages'
        """)
        if not cursor.fetchone():
            return {
                "status": "ok",
                "source_id": source_id,
                "total_messages": 0,
                "processed_messages": 0,
                "unprocessed_messages": 0,
                "enrichment_jobs": source_def.canonical_enrichment_jobs,
                "enrichment_trigger": getattr(source_def, "enrichment_trigger", "automatic"),
                "message": "Canonical table does not exist yet",
            }
        
        # Check if ai_chat_conversations table exists for dataset_id filtering
        cursor = db_conn.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='ai_chat_conversations'
        """)
        has_conversations_table = cursor.fetchone() is not None
        
        # Build query to count messages from canonical table
        if has_conversations_table and dataset_id:
            # Join with conversations table to filter by owner_user_id
            user_id = dataset_id.split(":")[0] if ":" in dataset_id else dataset_id
            query = """
                SELECT COUNT(*)
                FROM ai_chat_messages m
                LEFT JOIN ai_chat_conversations c ON m.conversation_id = c.conversation_id
                WHERE m.source_id = ? AND c.owner_user_id = ?
            """
            cursor = db_conn.execute(query, (source_id, user_id))
        else:
            # Direct query without user filtering
            query = "SELECT COUNT(*) FROM ai_chat_messages WHERE source_id = ?"
            cursor = db_conn.execute(query, (source_id,))
        
        total = cursor.fetchone()[0]
        
    except Exception as e:
        logger.error("Failed to read canonical messages from ai_chat_messages table: %s", e)
        return {
            "status": "error",
            "source_id": source_id,
            "total_messages": 0,
            "processed_messages": 0,
            "unprocessed_messages": 0,
            "enrichment_jobs": source_def.canonical_enrichment_jobs,
            "enrichment_trigger": getattr(source_def, "enrichment_trigger", "automatic"),
            "message": f"Error reading canonical table: {e}",
        }
    
    # Get unprocessed messages count (reuse the logic from _find_unprocessed_messages)
    unprocessed = await _find_unprocessed_messages(source_id, dataset_id)
    unprocessed_count = len(unprocessed)
    processed_count = total - unprocessed_count
    
    return {
        "status": "ok",
        "source_id": source_id,
        "total_messages": total,
        "processed_messages": processed_count,
        "unprocessed_messages": unprocessed_count,
        "enrichment_jobs": source_def.canonical_enrichment_jobs,
        "enrichment_trigger": getattr(source_def, "enrichment_trigger", "automatic"),
    }


@router.post("/enrichment/process", dependencies=[Depends(require_api_key)])
async def process_enrichment(
    source_id: str = Body(...),
    dataset_id: Optional[str] = Body(None),
    job_names: Optional[List[str]] = Body(None),
    force_reprocess: bool = Body(False),
) -> Dict[str, Any]:
    """Manually trigger enrichment for unprocessed messages.
    
    Args:
        source_id: Source identifier
        dataset_id: Optional dataset ID to filter by
        job_names: Optional list of specific enrichment jobs to run
        force_reprocess: If True, reprocess even if already enriched
        
    Returns:
        Processing results
    """
    try:
        return await _process_enrichment_core(
            source_id=source_id,
            dataset_id=dataset_id,
            job_names=job_names,
            force_reprocess=force_reprocess,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("Manual enrichment failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/enrichment/status", dependencies=[Depends(require_api_key)])
async def get_processing_status(
    source_id: str,
    dataset_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Get enrichment status for a source.
    
    Returns:
        Status information including counts of processed/unprocessed messages
    """
    try:
        return await _get_enrichment_status_core(
            source_id=source_id,
            dataset_id=dataset_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("Failed to get enrichment status: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


def _list_source_enrichments_core(source_id: str) -> Dict[str, Any]:
    """List enrichment capabilities for a specific source (shared by HTTP and batch handlers)."""
    from ..enrichment.catalog import get_catalog_entry, jobs_configured_for_source

    source_def = REGISTRY.get(source_id)
    if not source_def:
        raise ValueError(f"Source {source_id} not found")

    raw_jobs = list(getattr(source_def, "raw_enrichment_jobs", []) or [])
    canonical_jobs = list(getattr(source_def, "canonical_enrichment_jobs", []) or [])
    implemented_backfills = sorted(
        enrichment_name
        for sid, enrichment_name in _RAW_SOURCE_BACKFILL_HANDLERS
        if sid == source_id
    )
    capabilities: List[Dict[str, Any]] = []
    for name in raw_jobs:
        key = (source_id, name)
        entry = get_catalog_entry(name)
        capabilities.append(
            {
                "name": name,
                "supports_backfill": key in _RAW_SOURCE_BACKFILL_HANDLERS
                or bool(entry and entry.supports_backfill and not entry.stub),
                "supports_test": key in _RAW_SOURCE_TEST_HANDLERS,
                "test_input_schema": _RAW_SOURCE_TEST_SCHEMAS.get(key),
            }
        )

    # Unified per-job view across all three lanes, driven by the catalog.
    lanes = jobs_configured_for_source(source_def)
    unified: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for lane in ("raw", "canonical", "signal"):
        for job_id in lanes.get(lane) or []:
            if job_id in seen:
                continue
            seen.add(job_id)
            entry = get_catalog_entry(job_id)
            job_lanes = [l for l in ("raw", "canonical", "signal") if job_id in (lanes.get(l) or [])]
            unified.append(
                {
                    "job_id": job_id,
                    "title": entry.title if entry else job_id,
                    "description": entry.description if entry else "",
                    "lanes": job_lanes,
                    "baseline": bool(entry and entry.baseline),
                    "cost_tier": entry.cost_tier if entry else None,
                    "default_provider": entry.default_provider if entry else None,
                    "default_model": entry.default_model if entry else None,
                    "output_tables": list(entry.output_tables) if entry else [],
                    "supports_backfill": bool(entry and entry.supports_backfill),
                    "supports_delete": bool(entry and entry.supports_delete),
                    "supports_test": (source_id, job_id) in _RAW_SOURCE_TEST_HANDLERS,
                    "stub": bool(entry and entry.stub),
                }
            )

    return {
        "status": "ok",
        "source_id": source_id,
        "ingestion_trigger": getattr(source_def, "ingestion_trigger", "automatic"),
        "enrichment_trigger": getattr(source_def, "enrichment_trigger", "automatic"),
        "raw_enrichments": raw_jobs,
        "raw_enrichment_capabilities": capabilities,
        "canonical_enrichments": canonical_jobs,
        "signal_enrichments": list(lanes.get("signal") or []),
        "enrichments": unified,
        "raw_backfill_supported": implemented_backfills,
    }


@router.get(
    "/sources/{source_id}/enrichments",
    dependencies=[Depends(require_api_key)],
)
async def list_source_enrichments(source_id: str) -> Dict[str, Any]:
    """List enrichment capabilities for a specific source."""
    try:
        return _list_source_enrichments_core(source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _toggle_source_enrichment_core(
    source_id: str, job_name: str, enabled: Optional[bool]
) -> Dict[str, Any]:
    """Enable/disable an enrichment for one source (shared by HTTP and WS)."""
    from ..enrichment.catalog import jobs_configured_for_source
    from ..enrichment.source_overrides import set_source_enrichment_override

    source_def = REGISTRY.get(source_id)
    if not source_def:
        raise ValueError(f"Source {source_id} not found")
    overrides = set_source_enrichment_override(source_id, job_name, enabled)
    lanes = jobs_configured_for_source(source_def)
    enabled_lanes = [lane for lane, jobs in lanes.items() if job_name in (jobs or [])]
    return {
        "status": "ok",
        "source_id": source_id,
        "job_id": job_name,
        "enabled": bool(enabled_lanes),
        "enabled_lanes": enabled_lanes,
        "override": overrides.get(job_name),
    }


@router.patch(
    "/sources/{source_id}/enrichments/{enrichment_name}",
    dependencies=[Depends(require_api_key)],
)
async def toggle_source_enrichment(
    source_id: str,
    enrichment_name: str,
    body: Dict[str, Any] = Body(default_factory=dict),
) -> Dict[str, Any]:
    """Attach/detach an enrichment for this source at runtime.

    Body: ``{"enabled": true | false | null}`` — null clears the override,
    reverting to the source definition's default.
    """
    if "enabled" not in body:
        raise HTTPException(
            status_code=400, detail="body must include 'enabled' (true, false, or null)"
        )
    enabled = body.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        raise HTTPException(status_code=400, detail="'enabled' must be true, false, or null")
    try:
        return _toggle_source_enrichment_core(source_id, enrichment_name, enabled)
    except ValueError as exc:
        message = str(exc)
        status = 404 if "not found" in message.lower() else 400
        raise HTTPException(status_code=status, detail=message) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post(
    "/sources/{source_id}/enrichments/{enrichment_name}/backfill",
    dependencies=[Depends(require_api_key)],
)
async def backfill_source_enrichment(
    source_id: str,
    enrichment_name: str,
    only_missing: bool = Body(True),
    limit: Optional[int] = Body(None),
) -> Dict[str, Any]:
    """Backfill an enrichment for an ingestion source's existing rows.

    This endpoint is source-scoped (raw/source layer), separate from canonical
    message enrichment endpoints.
    """
    from ..enrichment.catalog import all_jobs_for_source, get_catalog_entry

    source_def = REGISTRY.get(source_id)
    if not source_def:
        raise HTTPException(status_code=404, detail=f"Source {source_id} not found")

    configured_jobs = set(all_jobs_for_source(source_def))
    if enrichment_name not in configured_jobs:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Enrichment '{enrichment_name}' is not configured for source '{source_id}'. "
                f"Configured enrichments: {sorted(configured_jobs)}"
            ),
        )

    catalog_entry = get_catalog_entry(enrichment_name)
    handler = _RAW_SOURCE_BACKFILL_HANDLERS.get((source_id, enrichment_name))
    if not handler and (not catalog_entry or not catalog_entry.supports_backfill or catalog_entry.stub):
        raise HTTPException(
            status_code=501,
            detail=f"Backfill for source='{source_id}' enrichment='{enrichment_name}' is not implemented",
        )

    db_conn = get_db_connection()
    if not db_conn:
        raise HTTPException(status_code=503, detail="Database connection not available")

    try:
        if handler:
            result = await handler(
                db_conn=db_conn,
                only_missing=only_missing,
                limit=limit,
            )
        else:
            result = await _generic_backfill_core(
                source_def=source_def,
                job_name=enrichment_name,
                only_missing=only_missing,
                limit=limit,
            )
        return {
            "status": "ok",
            "source_id": source_id,
            "enrichment_name": enrichment_name,
            "only_missing": only_missing,
            "limit": limit,
            **result,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Source enrichment backfill failed: source=%s enrichment=%s error=%s",
            source_id,
            enrichment_name,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc))


@router.post(
    "/sources/{source_id}/enrichments/{enrichment_name}/test",
    dependencies=[Depends(require_api_key)],
)
async def test_source_enrichment(
    source_id: str,
    enrichment_name: str,
    data_packet: Dict[str, Any] = Body(...),
) -> Dict[str, Any]:
    """Test-run a source enrichment against a provided data packet."""
    source_def = REGISTRY.get(source_id)
    if not source_def:
        raise HTTPException(status_code=404, detail=f"Source {source_id} not found")

    configured_raw_jobs = set(getattr(source_def, "raw_enrichment_jobs", []) or [])
    if enrichment_name not in configured_raw_jobs:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Enrichment '{enrichment_name}' is not configured for source '{source_id}'. "
                f"Configured raw enrichments: {sorted(configured_raw_jobs)}"
            ),
        )

    handler = _RAW_SOURCE_TEST_HANDLERS.get((source_id, enrichment_name))
    if not handler:
        raise HTTPException(
            status_code=501,
            detail=f"Test for source='{source_id}' enrichment='{enrichment_name}' is not implemented",
        )

    try:
        result = await handler(data_packet=data_packet)
        return {
            "status": "ok",
            "source_id": source_id,
            "enrichment_name": enrichment_name,
            **result,
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Source enrichment test failed: source=%s enrichment=%s error=%s",
            source_id,
            enrichment_name,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/enrichments/catalog", dependencies=[Depends(require_api_key)])
async def get_enrichments_catalog(source_id: Optional[str] = None) -> Dict[str, Any]:
    """Unified enrichment catalog across all lanes, optionally annotated per source."""
    try:
        return _enrichment_catalog_core(source_id=source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/enrichments/preview", dependencies=[Depends(require_api_key)])
async def get_enrichments_preview(source_id: str, limit: int = 20) -> Dict[str, Any]:
    """Recent records for a source with their enrichment outputs attached."""
    try:
        return _enrichment_preview_core(source_id=source_id, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("Enrichment preview failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/enrichments/coverage", dependencies=[Depends(require_api_key)])
async def get_enrichments_coverage(source_id: str) -> Dict[str, Any]:
    """Per-enrichment coverage stats (rows enriched vs canonical total) for a source."""
    try:
        return _enrichment_coverage_core(source_id=source_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("Enrichment coverage failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=str(exc))


@router.delete(
    "/sources/{source_id}/enrichments/{enrichment_name}/data",
    dependencies=[Depends(require_api_key)],
)
async def delete_source_enrichment_data(
    source_id: str,
    enrichment_name: str,
) -> Dict[str, Any]:
    """Delete one enrichment's output rows for a source (owner lifecycle control)."""
    try:
        return _delete_enrichment_data_core(source_id=source_id, job_name=enrichment_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Enrichment delete failed: source=%s enrichment=%s error=%s",
            source_id,
            enrichment_name,
            exc,
            exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc))
