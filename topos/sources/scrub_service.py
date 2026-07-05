"""Source scrub orchestration — Remove vs Scrub presets and structured reports."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from ..config.settings import settings
from ..core.state import get_db_connection
from ..pipeline.audit import SQLiteIngestAuditStore, StageAuditRow
from ..pipeline.stages import PipelineStage
from . import install_service
from .scrub_attribution import (
    TableAction,
    plan_attributed_rows,
    plan_remove_raw_and_flat_tables,
    remove_raw_and_flat_tables,
    scrub_attributed_rows,
)

logger = logging.getLogger("topos.sources.scrub_service")

_RESIDUE_SUMMARY = (
    "Blended dimension briefs and topic themes may retain minor semantic residue "
    "from cross-source summaries until refresh/recompute completes."
)

_SCRUB_GUARD = threading.Lock()
_ACTIVE_SCRUBS: set[str] = set()


class ScrubInProgressError(RuntimeError):
    """Raised when a scrub is already running for the same source_id."""


@dataclass
class ScrubOptions:
    keep_install: bool = False
    purge_attributed_rows: bool = True
    remove_raw_and_flat: bool = True
    cleanup_vector_index: bool = True
    recompute_topic_clusters: bool = True
    refresh_dimension_briefs: bool = True
    # Propagate into derived intelligence (entity spine, stats refold, facts,
    # dossiers). Aggregates are non-subtractable, so this recomputes them from
    # the remaining corpus — see features/lifecycle/derived_scrub.py.
    purge_derived_intelligence: bool = True
    brief_dimensions: Optional[List[str]] = None
    purge_brief_revision_history: bool = False
    dry_run: bool = False


REMOVE_SOURCE_OPTIONS = ScrubOptions(
    keep_install=False,
    purge_attributed_rows=False,
    remove_raw_and_flat=True,
    cleanup_vector_index=False,
    recompute_topic_clusters=False,
    refresh_dimension_briefs=False,
    # Remove keeps canonical rows, so derived intelligence stays valid.
    purge_derived_intelligence=False,
)

SCRUB_SOURCE_OPTIONS = ScrubOptions(
    keep_install=False,
    purge_attributed_rows=True,
    remove_raw_and_flat=True,
    cleanup_vector_index=True,
    recompute_topic_clusters=True,
    refresh_dimension_briefs=True,
    purge_derived_intelligence=True,
)

SCRUB_LITE_OPTIONS = ScrubOptions(
    keep_install=False,
    purge_attributed_rows=True,
    remove_raw_and_flat=True,
    cleanup_vector_index=True,
    recompute_topic_clusters=True,
    refresh_dimension_briefs=False,
    purge_derived_intelligence=True,
)


def scrub_options_from_dict(raw: Optional[Dict[str, Any]]) -> ScrubOptions:
    raw = raw if isinstance(raw, dict) else {}
    base = SCRUB_SOURCE_OPTIONS
    return ScrubOptions(
        keep_install=bool(raw.get("keep_install", base.keep_install)),
        purge_attributed_rows=bool(raw.get("purge_attributed_rows", base.purge_attributed_rows)),
        remove_raw_and_flat=bool(raw.get("remove_raw_and_flat", base.remove_raw_and_flat)),
        cleanup_vector_index=bool(raw.get("cleanup_vector_index", base.cleanup_vector_index)),
        recompute_topic_clusters=bool(raw.get("recompute_topic_clusters", base.recompute_topic_clusters)),
        refresh_dimension_briefs=bool(raw.get("refresh_dimension_briefs", base.refresh_dimension_briefs)),
        purge_derived_intelligence=bool(
            raw.get("purge_derived_intelligence", base.purge_derived_intelligence)
        ),
        brief_dimensions=raw.get("brief_dimensions") if isinstance(raw.get("brief_dimensions"), list) else None,
        purge_brief_revision_history=bool(
            raw.get("purge_brief_revision_history", base.purge_brief_revision_history)
        ),
        dry_run=_coerce_bool(raw.get("dry_run"), default=False),
    )


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    """Parse booleans safely; reject string 'false' being truthy via bool()."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    return bool(value)


def normalize_scrub_payload(payload: Dict[str, Any]) -> tuple[str, ScrubOptions]:
    """Merge top-level scrub fields into options; reject conflicting dry_run values."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    source_id = str(payload.get("source_id") or "").strip()
    if not source_id:
        raise ValueError("source_id is required")

    opts_raw: Dict[str, Any] = dict(payload.get("options") or {}) if isinstance(payload.get("options"), dict) else {}

    if "dry_run" in payload:
        top_dry = _coerce_bool(payload.get("dry_run"))
        if "dry_run" in opts_raw and _coerce_bool(opts_raw.get("dry_run")) != top_dry:
            raise ValueError("conflicting dry_run values in payload and options")
        opts_raw.setdefault("dry_run", top_dry)

    preset = str(payload.get("preset") or opts_raw.get("preset") or "").strip().lower()
    if preset in {"remove", "remove_source"}:
        base = REMOVE_SOURCE_OPTIONS
    elif preset in {"scrub_lite", "lite"}:
        base = SCRUB_LITE_OPTIONS
    elif preset in {"scrub", "scrub_source"}:
        base = SCRUB_SOURCE_OPTIONS
    else:
        base = SCRUB_SOURCE_OPTIONS

    merged = {
        "keep_install": opts_raw.get("keep_install", base.keep_install),
        "purge_attributed_rows": opts_raw.get("purge_attributed_rows", base.purge_attributed_rows),
        "remove_raw_and_flat": opts_raw.get("remove_raw_and_flat", base.remove_raw_and_flat),
        "cleanup_vector_index": opts_raw.get("cleanup_vector_index", base.cleanup_vector_index),
        "recompute_topic_clusters": opts_raw.get("recompute_topic_clusters", base.recompute_topic_clusters),
        "refresh_dimension_briefs": opts_raw.get("refresh_dimension_briefs", base.refresh_dimension_briefs),
        "purge_derived_intelligence": opts_raw.get(
            "purge_derived_intelligence", base.purge_derived_intelligence
        ),
        "brief_dimensions": opts_raw.get("brief_dimensions"),
        "purge_brief_revision_history": opts_raw.get(
            "purge_brief_revision_history", base.purge_brief_revision_history
        ),
        "dry_run": opts_raw.get("dry_run", base.dry_run),
    }
    return source_id, scrub_options_from_dict(merged)


@dataclass
class ScrubReport:
    install: Dict[str, Any] = field(default_factory=dict)
    tables: List[Dict[str, Any]] = field(default_factory=list)
    recompute: Dict[str, Any] = field(default_factory=dict)
    residue: Dict[str, Any] = field(default_factory=dict)
    totals: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def scrub_result_to_uninstall_dict(scrub_result: Dict[str, Any], *, scope: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    report = scrub_result.get("report") if isinstance(scrub_result.get("report"), dict) else {}
    totals = report.get("totals") if isinstance(report.get("totals"), dict) else {}
    tables = report.get("tables") if isinstance(report.get("tables"), list) else []
    tables_dropped = sorted(
        {
            str(row.get("table") or "")
            for row in tables
            if isinstance(row, dict) and row.get("action") == "table_dropped" and row.get("table")
        }
    )
    install = report.get("install") if isinstance(report.get("install"), dict) else {}
    scope_out = scope if isinstance(scope, dict) else {}
    if not str(scope_out.get("user_id") or "").strip():
        scope_out = install_service._scope_dict(install_service._scope_key(scope))
    return {
        "status": "ok",
        "source_id": scrub_result.get("source_id"),
        "scope": scope_out,
        "uninstalled": bool(install.get("uninstalled")),
        "delete_source_tables": True,
        "rows_deleted": int(totals.get("rows_deleted") or 0),
        "tables_dropped": tables_dropped,
    }


def _assert_scrub_allowed() -> None:
    pool_mode = str(getattr(settings, "topos_pool_mode", "off") or "off").strip().lower()
    if pool_mode not in {"", "off", "false", "0", "none"}:
        raise RuntimeError("Scrub is not available on pooled engines yet")


def _table_actions_to_dict(actions: List[TableAction]) -> List[Dict[str, Any]]:
    return [{"table": item.table, "action": item.action, "count": int(item.count)} for item in actions]


def _build_totals(table_actions: List[TableAction]) -> Dict[str, int]:
    totals = {
        "rows_deleted": 0,
        "tables_dropped": 0,
        "embeddings_removed": 0,
        "vec_rows_removed": 0,
    }
    for item in table_actions:
        if item.action == "rows_deleted":
            totals["rows_deleted"] += int(item.count)
            if item.table == "signal_embeddings":
                totals["embeddings_removed"] += int(item.count)
        elif item.action == "table_dropped":
            totals["tables_dropped"] += 1
        elif item.action == "vec_rows_deleted":
            totals["vec_rows_removed"] += int(item.count)
    return totals


def _residue_block() -> Dict[str, Any]:
    return {
        "summary": _RESIDUE_SUMMARY,
        "items": [
            {
                "kind": "dimension_brief",
                "detail": "Brief prose is regenerated from remaining canonical rows; paraphrased themes may persist.",
            },
            {
                "kind": "topic_cluster_theme",
                "detail": "Global clusters rebuilt from remaining sources only; similar labels may appear.",
            },
        ],
    }


def _resolve_source_definition(source_id: str, scope: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    source_def = install_service.get_active_source_definition(source_id=source_id, scope=scope)
    if source_def:
        return source_def
    installs = install_service.list_installs(source_id=source_id)
    for rec in installs:
        if isinstance(rec.source_definition_json, dict):
            return rec.source_definition_json
    return None


def _resolve_brief_dimensions(source_def: Dict[str, Any], opts: ScrubOptions, source_id: str) -> List[str]:
    if opts.brief_dimensions:
        return [str(item).strip() for item in opts.brief_dimensions if str(item).strip()]
    from ..features.signal.dimension_registry import dimensions_for_brief_update

    return list(
        dimensions_for_brief_update(
            source_id=source_id,
            canonical_group_id=str(source_def.get("canonical_group_id") or ""),
        )
    )


def _estimate_attributed_rows(conn: Any, source_id: str) -> int:
    return plan_attributed_rows(conn, source_id).rows_deleted


def _append_scrub_audit(conn: Any, *, source_id: str, scrub_id: str, records_out: int, status: str, error: Optional[str] = None) -> None:
    import sqlite3
    from datetime import datetime, timezone

    if not isinstance(conn, sqlite3.Connection):
        return
    finished = datetime.now(timezone.utc).isoformat()
    try:
        SQLiteIngestAuditStore(conn).append_stage(
            StageAuditRow(
                sync_batch_id=scrub_id,
                source_id=source_id,
                stage=PipelineStage.SOURCE_SCRUB,
                status=status,
                job_id=scrub_id,
                records_out=records_out,
                error=error,
                finished_at=finished,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("scrub audit append skipped: %s", exc)


async def _run_recompute_phase(
    conn: Any,
    *,
    opts: ScrubOptions,
    source_def: Dict[str, Any],
    source_id: str,
    scrubbed_record_ids: Optional[set] = None,
) -> tuple[Dict[str, Any], bool]:
    partial = False
    recompute: Dict[str, Any] = {
        "topic_clusters": {"status": "skipped", "reason": "disabled"},
        "dimension_briefs": [],
        "dimension_profiles": {"status": "skipped", "reason": "disabled"},
        "derived_intelligence": {"status": "skipped", "reason": "disabled"},
    }

    # Derived-intelligence propagation runs first: it recomputes stats/facts/
    # entity artifacts from the remaining corpus, which the cluster/brief
    # refresh below then builds on.
    if opts.purge_derived_intelligence and isinstance(conn, sqlite3.Connection):
        try:
            from ..features.lifecycle.derived_scrub import purge_derived_for_source

            recompute["derived_intelligence"] = purge_derived_for_source(
                conn, source_id, scrubbed_record_ids=scrubbed_record_ids
            )
        except Exception as exc:  # noqa: BLE001
            recompute["derived_intelligence"] = {"status": "failed", "error": str(exc)}
            partial = True

    if not (opts.recompute_topic_clusters or opts.refresh_dimension_briefs):
        return recompute, partial

    min_records = int(getattr(settings, "scrub_min_embeddings_for_recluster", 3) or 3)

    if opts.recompute_topic_clusters:
        from ..features.signal.topic_clustering import recompute_topic_clusters, write_top_topics_signal_facts
        from ..storage.adapters.factory import AdapterFactory

        tc_result = recompute_topic_clusters(conn, min_records=min_records)
        recompute["topic_clusters"] = tc_result
        if tc_result.get("status") == "completed":
            bundle = AdapterFactory.create("local_database", conn=conn)
            try:
                write_top_topics_signal_facts(bundle, conn)
            except Exception as exc:  # noqa: BLE001
                partial = True
                recompute["topic_clusters"]["facts_error"] = str(exc)
        elif tc_result.get("status") not in {"skipped", "completed"}:
            partial = True

    if opts.refresh_dimension_briefs:
        from ..features.signal.service import SignalService

        service = SignalService(conn)
        brief_results: List[Dict[str, Any]] = []
        for dimension in _resolve_brief_dimensions(source_def, opts, source_id):
            try:
                result = await service.refresh_brief(dimension, limit=40)
                ok = bool(result.get("ok"))
                brief_results.append(
                    {
                        "dimension": dimension,
                        "status": "refreshed" if ok else "failed",
                        "ok": ok,
                        "error": result.get("error"),
                    }
                )
                if not ok:
                    partial = True
            except Exception as exc:  # noqa: BLE001
                brief_results.append(
                    {"dimension": dimension, "status": "failed", "ok": False, "error": str(exc)}
                )
                partial = True
        recompute["dimension_briefs"] = brief_results

    try:
        from ..features.signal.dimension_profiles import DimensionProfileUpdater
        from ..storage.adapters.factory import AdapterFactory

        bundle = AdapterFactory.create("local_database", conn=conn)
        DimensionProfileUpdater(bundle, conn).upsert_all()
        recompute["dimension_profiles"] = {"status": "updated"}
    except Exception as exc:  # noqa: BLE001
        recompute["dimension_profiles"] = {"status": "failed", "error": str(exc)}
        partial = True

    return recompute, partial


def _acquire_scrub_lock(source_id: str) -> None:
    with _SCRUB_GUARD:
        if source_id in _ACTIVE_SCRUBS:
            raise ScrubInProgressError(f"Scrub already in progress for source_id={source_id}")
        _ACTIVE_SCRUBS.add(source_id)


def _release_scrub_lock(source_id: str) -> None:
    with _SCRUB_GUARD:
        _ACTIVE_SCRUBS.discard(source_id)


async def scrub_source_async(
    *,
    source_id: str,
    scope: Optional[Dict[str, Any]] = None,
    options: Optional[ScrubOptions] = None,
) -> Dict[str, Any]:
    """Run tier-B Remove or tier-C Scrub according to ``options``."""
    _assert_scrub_allowed()
    sid = str(source_id or "").strip()
    if not sid:
        raise ValueError("source_id is required")

    opts = options or SCRUB_SOURCE_OPTIONS
    scrub_id = f"scrub_{uuid.uuid4()}"
    request_id = str(uuid.uuid4())
    started = time.perf_counter()

    _acquire_scrub_lock(sid)
    try:
        source_def = _resolve_source_definition(sid, scope) or {"source_id": sid, "source_type": "ui_stream"}
        conn = get_db_connection()
        if conn is None:
            raise RuntimeError("Database not available")

        table_actions: List[TableAction] = []
        install_info = {"uninstalled": False, "runtime_handle_cleared": False}

        if opts.dry_run:
            if opts.remove_raw_and_flat:
                table_actions.extend(plan_remove_raw_and_flat_tables(conn, source_def, sid))
            if opts.purge_attributed_rows:
                table_actions.extend(plan_attributed_rows(conn, sid).tables)
            report = ScrubReport(
                install={"dry_run": True},
                tables=_table_actions_to_dict(table_actions),
                recompute={
                    "topic_clusters": {"status": "skipped", "reason": "dry_run"},
                    "dimension_briefs": [],
                },
                residue=_residue_block() if opts.purge_attributed_rows else {"summary": "", "items": []},
                totals=_build_totals(table_actions),
            )
            duration_ms = int((time.perf_counter() - started) * 1000)
            return {
                "status": "ok",
                "request_id": request_id,
                "scrub_id": scrub_id,
                "source_id": sid,
                "scrub_status": "dry_run",
                "duration_ms": duration_ms,
                "report": report.to_dict(),
            }

        row_limit = int(getattr(settings, "scrub_sync_row_limit", 50000) or 50000)
        if opts.purge_attributed_rows and _estimate_attributed_rows(conn, sid) > row_limit:
            raise RuntimeError(
                f"Scrub exceeds synchronous row limit ({row_limit}); async scrub not yet available"
            )

        if not opts.keep_install:
            uninstall_result = install_service.uninstall_source(
                source_id=sid,
                scope=scope,
                delete_source_tables=False,
            )
            install_info = {
                "uninstalled": bool(uninstall_result.get("uninstalled")),
                "runtime_handle_cleared": bool(uninstall_result.get("uninstalled")),
            }

        # Snapshot the source's record ids before the attribution sweep deletes
        # them — fact provenance (source_refs) may reference records only, and
        # after the sweep they can no longer be attributed.
        scrubbed_record_ids: set = set()
        if opts.purge_derived_intelligence and opts.purge_attributed_rows:
            try:
                scrubbed_record_ids = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT record_id FROM timeline WHERE source_id = ?", (sid,)
                    ).fetchall()
                }
            except Exception:  # noqa: BLE001
                scrubbed_record_ids = set()

        if opts.remove_raw_and_flat:
            table_actions.extend(remove_raw_and_flat_tables(conn, source_def, sid))

        if opts.purge_attributed_rows:
            table_actions.extend(scrub_attributed_rows(conn, sid).tables)

        if settings.topos_database_mode != "postgres":
            conn.commit()

        recompute, partial = await _run_recompute_phase(
            conn,
            opts=opts,
            source_def=source_def,
            source_id=sid,
            scrubbed_record_ids=scrubbed_record_ids,
        )

        report = ScrubReport(
            install=install_info,
            tables=_table_actions_to_dict(table_actions),
            recompute=recompute,
            residue=_residue_block() if opts.purge_attributed_rows else {"summary": "", "items": []},
            totals=_build_totals(table_actions),
        )
        scrub_status = "partial" if partial else "completed"
        duration_ms = int((time.perf_counter() - started) * 1000)
        _append_scrub_audit(
            conn,
            source_id=sid,
            scrub_id=scrub_id,
            records_out=int(report.totals.get("rows_deleted") or 0),
            status=scrub_status,
        )
        if settings.topos_database_mode != "postgres":
            conn.commit()
        logger.debug(
            "[PIPELINE:SCRUB] source_id=%s scrub_id=%s status=%s duration_ms=%s totals=%s",
            sid,
            scrub_id,
            scrub_status,
            duration_ms,
            report.totals,
        )
        return {
            "status": "ok",
            "request_id": request_id,
            "scrub_id": scrub_id,
            "source_id": sid,
            "scrub_status": scrub_status,
            "duration_ms": duration_ms,
            "report": report.to_dict(),
        }
    finally:
        _release_scrub_lock(sid)


def scrub_source(
    *,
    source_id: str,
    scope: Optional[Dict[str, Any]] = None,
    options: Optional[ScrubOptions] = None,
) -> Dict[str, Any]:
    """Sync entry point for tests and legacy callers."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            scrub_source_async(source_id=source_id, scope=scope, options=options)
        )
    raise RuntimeError("scrub_source cannot be called from a running event loop; use scrub_source_async")
