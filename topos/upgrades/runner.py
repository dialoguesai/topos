"""Startup upgrade runner — the manifests' mailman.

At boot the node compares its stamped upgrade BASELINE (the last version whose
re-derivation steps all completed) against the shipped version and executes
``steps_between(baseline, shipped)`` through the derivation ledger:

  * fresh install (no baseline, no data): nothing is derived yet and ingestion
    derives with current code — stamp the shipped version and skip;
  * bootstrap (no baseline, data present): the node predates the ledger; every
    such node is ≤ 1.1.0, so the baseline is assumed "1.1.0" and everything
    since runs (documented in topos/upgrades/manifests.json);
  * resumable: each step ledgers pending→running→done/failed; interrupted
    ('running' after a crash) and failed steps re-run on the next boot; the
    baseline only advances when EVERY step for the target version is done;
  * kill-switch: TOPOS_UPGRADE_RUNNER=off disables execution (nothing stamps,
    so re-enabling picks up where it left off).

Heavy steps are deferred until the UI can bootstrap (control-plane ready +
grace window), then run in a daemon thread with a TUI progress bar. Enrichment
reprocess steps run only the declared ``job_names`` — they do NOT fan out into
the signal/LLM lane (that was starving startup UI traffic).

Executors are injectable for tests; the real ones dispatch step kinds to
engine internals (no HTTP self-calls).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from . import _version_key, declaring_versions, steps_between, steps_through
from ..storage.db.write_gate import commit_connection, with_db_write

logger = logging.getLogger("topos.upgrades.runner")

_BASELINE_KEY = "engine.upgrade.baseline"
_BOOTSTRAP_BASELINE = "1.1.0"
_DEFAULT_UI_GRACE_SECONDS = 20.0
_DEFAULT_READY_TIMEOUT_SECONDS = 60.0

#: Slice for polling ``stop_event`` while waiting on the UI-ready event. Short
#: enough that shutdown does not wait out the remaining ready-timeout.
_STOP_POLL_SLICE_S = 0.05

ExecutorFn = Callable[[Dict[str, Any], sqlite3.Connection], Dict[str, Any]]

# Status snapshot for the /upgrade/status surface (module-level, single node).
_state_lock = threading.Lock()
_runner_state: Dict[str, Any] = {
    "running": False,
    "waiting_for_ui": False,
    "current_step": None,
    "progress": None,
    "last_result": None,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _enabled() -> bool:
    return os.environ.get("TOPOS_UPGRADE_RUNNER", "on").strip().lower() not in (
        "0", "false", "off", "no",
    )


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return default


def ui_grace_seconds() -> float:
    """Seconds to wait after readiness so the React UI can fetch bootstrap data."""
    return _env_float("TOPOS_UPGRADE_UI_GRACE_SECONDS", _DEFAULT_UI_GRACE_SECONDS)


def ready_timeout_seconds() -> float:
    return _env_float("TOPOS_UPGRADE_READY_TIMEOUT_SECONDS", _DEFAULT_READY_TIMEOUT_SECONDS)


def _shipped_version() -> str:
    from topos.__version__ import __version__

    return __version__


def _set_runner_state(**updates: Any) -> None:
    with _state_lock:
        _runner_state.update(updates)


def _progress_snapshot(
    *,
    step_id: str,
    step_index: int,
    steps_total: int,
    unit_label: str,
    completed: int,
    total: int,
    detail: Optional[str] = None,
) -> Dict[str, Any]:
    total = max(0, int(total))
    completed = max(0, min(int(completed), total if total else int(completed)))
    fraction = (completed / total) if total else 0.0
    # Blend step index with in-step fraction so multi-step upgrades read smoothly.
    if steps_total > 0:
        overall = (max(0, step_index) + fraction) / steps_total
    else:
        overall = fraction
    return {
        "step_id": step_id,
        "step_index": step_index,
        "steps_total": steps_total,
        "unit_label": unit_label,
        "completed": completed,
        "total": total,
        "percent": round(100.0 * overall, 1),
        "detail": detail,
    }


def read_baseline(conn: sqlite3.Connection) -> Optional[str]:
    try:
        row = conn.execute(
            "SELECT value FROM engine_config WHERE key=?", (_BASELINE_KEY,)
        ).fetchone()
        return str(row[0]) if row else None
    except sqlite3.Error:
        return None


def _ensure_engine_config(conn: sqlite3.Connection) -> None:
    # engine_config is bootstrapped outside the migration set; the stamp must
    # not depend on boot order.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS engine_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def _stamp_baseline(conn: sqlite3.Connection, version: str) -> None:
    with with_db_write():
        _ensure_engine_config(conn)
        conn.execute(
            "INSERT INTO engine_config (key, value, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (_BASELINE_KEY, version),
        )
        commit_connection(conn)


def _has_data(conn: sqlite3.Connection) -> bool:
    for table in ("timeline", "entities"):
        try:
            if conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                return True
        except sqlite3.Error:
            continue
    return False


def plan_upgrade(conn: sqlite3.Connection, shipped: Optional[str] = None) -> Dict[str, Any]:
    shipped = shipped or _shipped_version()
    baseline = read_baseline(conn)
    fresh = baseline is None and not _has_data(conn)
    if baseline is None and not fresh:
        baseline = _BOOTSTRAP_BASELINE
    if fresh:
        steps: List[Dict[str, Any]] = []
    else:
        window = [] if baseline == shipped else steps_between(baseline, shipped)
        steps = _plan_steps(conn, window, shipped)
    return {
        "shipped": shipped,
        "baseline": baseline,
        "fresh_install": fresh,
        "steps": steps,
    }


def _plan_steps(
    conn: sqlite3.Connection, window: List[Dict[str, Any]], shipped: str
) -> List[Dict[str, Any]]:
    """This hop's steps, plus anything an earlier hop left unfinished.

    The version window alone cannot express "this failed, run it again": once
    the baseline moves past the release that declared a step, the step drops
    out of every future plan and can never be retried. That is how 1.3.7's
    ``backfill-attention-triage-redo`` stayed failed across 1.3.8→1.3.11 while
    the UI promised a retry on every restart.

    Only steps that HAVE a ledger row short of 'done' come back. A step with no
    row at all was legitimately never owed — fresh installs stamp the baseline
    without running history, and dragging all of it back would be a far worse
    failure than the gap it closes.
    """
    in_window = {str(s["id"]) for s in window}
    declared = declaring_versions()
    planned: List[Dict[str, Any]] = []
    for step in steps_through(shipped):
        step_id = str(step["id"])
        if step_id in in_window:
            planned.append(step)
            continue
        status = _effective_status(conn, step_id, declared.get(step_id) or shipped)
        if status not in (None, "done"):
            planned.append(step)
    return planned


# --- ledger -----------------------------------------------------------------


def _ledger_version(step_id: str, shipped: str, declared: Optional[Dict[str, str]] = None) -> str:
    """The release a step's ledger row is filed under — where it was declared.

    Not the shipped version: filing under "whatever was running at the time"
    scatters one step across a row per upgrade hop and detaches its failures
    from the step itself.
    """
    declared = declaring_versions() if declared is None else declared
    return declared.get(str(step_id)) or shipped


def _effective_status(
    conn: sqlite3.Connection, step_id: str, declaring_version: str
) -> Optional[str]:
    """A step's status across every version it was ever ledgered under.

    Rows written before this fix are keyed by the shipped version at run time,
    so one step can own several (``retry-recorded-derivation-debt`` ledgered
    'done' under both 1.3.9 and 1.3.10). Any 'done' among them means done.
    Rows older than the declaring release belong to a PREVIOUS declaration of
    the same id and are ignored, which is what lets a re-declared step re-run.
    """
    try:
        rows = conn.execute(
            "SELECT version, status FROM derivation_ledger WHERE step_id=?",
            (str(step_id),),
        ).fetchall()
    except sqlite3.Error:
        return None
    try:
        floor = _version_key(declaring_version)
    except (TypeError, ValueError):
        floor = None
    latest: Optional[tuple] = None
    for version, status in rows:
        try:
            key = _version_key(str(version))
        except (TypeError, ValueError):
            continue
        if floor is not None and key < floor:
            continue
        if str(status) == "done":
            return "done"
        if latest is None or key > latest[0]:
            latest = (key, str(status))
    return latest[1] if latest else None


def _ledger_set(
    conn: sqlite3.Connection, version: str, step_id: str, status: str,
    detail: Optional[Dict[str, Any]] = None, started: bool = False,
) -> None:
    with with_db_write():
        conn.execute(
            """
            INSERT INTO derivation_ledger (version, step_id, status, started_at, finished_at, detail_json)
            VALUES (?, ?, ?, CASE WHEN ? THEN datetime('now') END, NULL, ?)
            ON CONFLICT(version, step_id) DO UPDATE SET
                status=excluded.status,
                started_at=COALESCE(CASE WHEN ? THEN datetime('now') END, derivation_ledger.started_at),
                finished_at=CASE WHEN excluded.status IN ('done','failed') THEN datetime('now') END,
                detail_json=COALESCE(excluded.detail_json, derivation_ledger.detail_json)
            """,
            (version, step_id, status, started, json.dumps(detail) if detail else None, started),
        )
        commit_connection(conn)


def ledger_rows(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    try:
        rows = conn.execute(
            "SELECT version, step_id, status, started_at, finished_at, detail_json "
            "FROM derivation_ledger ORDER BY started_at"
        ).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for version, step_id, status, started_at, finished_at, detail_json in rows:
        try:
            detail = json.loads(detail_json) if detail_json else None
        except (TypeError, ValueError):
            detail = None
        out.append({
            "version": version, "step_id": step_id, "status": status,
            "started_at": started_at, "finished_at": finished_at, "detail": detail,
        })
    return out


# --- real executors ----------------------------------------------------------


def _real_source_ids(conn: sqlite3.Connection) -> List[str]:
    skip = ("demo_", "enrichment_lab", "sanity", "test", "manual_enrichment")
    try:
        rows = conn.execute(
            "SELECT DISTINCT source_id FROM timeline WHERE source_id IS NOT NULL"
        ).fetchall()
    except sqlite3.Error:
        return []
    return sorted(
        s for (s,) in rows if s and not any(str(s).startswith(p) for p in skip)
    )


def _exec_enrichment_reprocess(step: Dict[str, Any], conn: sqlite3.Connection) -> Dict[str, Any]:
    """Walk real sources through the enrichment core with force_reprocess.

    Upgrade manifests declare specific ``job_names`` (e.g. attention_triage).
    Unless a step explicitly sets ``include_signal: true``, we keep the FULL
    signal / LLM derivation fan-out OFF — otherwise every source fans out into
    embeddings, cluster labeling, dimension briefs, and conversation-context LLM
    calls and starves the UI during first boot after an upgrade.

    Declared jobs are routed by the registry that actually owns them. A job in
    SIGNAL_JOB_REGISTRY but not CANONICAL_JOBS (attention_triage,
    topic_clusters, dimension_summary, ...) is invisible to
    ``EnrichmentOrchestrator.run_canonical``, which filters ``job_names``
    against its canonical list — so routing it down the canonical lane ran
    nothing while still ledgering ``done``. Such jobs go through the signal
    lane as a narrow ``signal_job_names`` list, which keeps them off the
    fan-out that ``include_signal`` guards.
    """
    import asyncio

    from ..api.enrichment import _process_enrichment_core
    from ..enrichment.jobs import CANONICAL_JOBS, SIGNAL_JOB_REGISTRY
    from ..enrichment.progress_bar import ProgressBar

    params = step.get("params") or {}
    job_names = params.get("job_names") or None
    include_signal = bool(params.get("include_signal", False))

    # Split declared jobs by owning registry. Canonical wins when a name is in
    # both (entities, embeddings, ...) — those already worked via run_canonical.
    canonical_names = {job.get_job_name() for job in CANONICAL_JOBS}
    declared = [str(n) for n in (job_names or [])]
    canonical_jobs = [n for n in declared if n in canonical_names]
    signal_jobs = [n for n in declared if n not in canonical_names and n in SIGNAL_JOB_REGISTRY]
    unknown_jobs = [
        n for n in declared if n not in canonical_names and n not in SIGNAL_JOB_REGISTRY
    ]

    source_ids = _real_source_ids(conn)
    step_id = str(step.get("id") or "enrichment_reprocess")
    step_index = int(step.get("_runner_step_index") or 0)
    steps_total = int(step.get("_runner_steps_total") or 1)
    detail: Dict[str, Any] = {"sources": {}, "include_signal": include_signal}
    if signal_jobs:
        detail["signal_jobs"] = list(signal_jobs)
    if unknown_jobs:
        # A manifest naming a job no registry owns would otherwise no-op in
        # silence — exactly the failure mode this routing split exists to end.
        detail["unknown_jobs"] = list(unknown_jobs)
        logger.warning(
            "upgrade step %s declares unknown enrichment job(s) %s — nothing will run for them",
            step_id,
            unknown_jobs,
        )
    def _run_sources(pbar: Optional[ProgressBar] = None) -> None:
        for idx, source_id in enumerate(source_ids):
            _set_runner_state(
                progress=_progress_snapshot(
                    step_id=step_id,
                    step_index=step_index,
                    steps_total=steps_total,
                    unit_label="sources",
                    completed=idx,
                    total=len(source_ids),
                    detail=source_id,
                )
            )
            if pbar is not None:
                pbar.set_description(f"Upgrade {step_id} · {source_id}")
            try:
                out = asyncio.run(
                    _process_enrichment_core(
                        source_id=source_id,
                        job_names=(canonical_jobs if declared else None),
                        # Default False: stale-predicate (spec_version) resumes;
                        # manifests may still set force_reprocess=true for wipes.
                        force_reprocess=bool(params.get("force_reprocess", False)),
                        include_signal=include_signal,
                        # A list (even empty) whenever the manifest declared
                        # jobs, so the core treats the canonical split as
                        # authoritative instead of widening [] back out to the
                        # source's full default job set.
                        signal_job_names=(signal_jobs if declared else None),
                    )
                )
                detail["sources"][source_id] = str(out.get("status") or "ok")
            except ValueError:
                detail["sources"][source_id] = "unknown_source_skipped"
            except Exception as exc:  # noqa: BLE001 — one source must not kill the walk
                detail["sources"][source_id] = f"error: {exc}"
            if pbar is not None:
                pbar.update(1)

    if source_ids:
        with ProgressBar(total=len(source_ids), desc=f"Upgrade {step_id}", width=40) as pbar:
            _run_sources(pbar)
    else:
        _run_sources(None)
    _set_runner_state(
        progress=_progress_snapshot(
            step_id=step_id,
            step_index=step_index,
            steps_total=steps_total,
            unit_label="sources",
            completed=len(source_ids),
            total=len(source_ids),
        )
    )
    return detail


def _exec_engine_endpoint(step: Dict[str, Any], conn: sqlite3.Connection) -> Dict[str, Any]:
    """Dispatch declared endpoints to engine internals — no HTTP self-call."""
    path = str((step.get("params") or {}).get("path") or "")
    if path == "/v1/signal/entities/graph/rebuild":
        # Subprocess when file-backed: the rebuild's compute starves the GIL
        # (2026-08-08), and upgrades run inside the live node process.
        from ..features.entities.rebuild_subprocess import run_graph_rebuild

        return dict(run_graph_rebuild(conn))
    if path == "/v1/signal/derivation-debt/retry":
        # Recorded debt is not self-healing: a failed row is only re-queued by
        # the next organic failure of the same (batch, job), and recover_stale_jobs
        # resets 'running' rows, never 'failed' ones. So a node that recorded debt
        # while its executor was missing keeps the gap until something sweeps it.
        import asyncio

        from ..enrichment.derivation_recovery import retry_pending_derivations

        params = step.get("params") or {}
        return dict(
            asyncio.run(
                retry_pending_derivations(
                    conn,
                    source_id=params.get("source_id"),
                    limit=int(params.get("limit") or 200),
                )
            )
        )
    if path == "/v1/canonical/journal/repair-ingest-dates":
        # entry_at written from the import clock while starts_at held the real
        # session time. New writes are fixed in SQLiteCanonicalStore; rows
        # already on disk only move if something sweeps them.
        from ..storage.canonical.journal_repair import repair_ingest_clock_dates

        params = step.get("params") or {}
        return dict(
            repair_ingest_clock_dates(conn, dry_run=bool(params.get("dry_run", False)))
        )
    raise ValueError(f"no internal dispatch for endpoint step: {path!r}")


def _exec_canonical_reprocess(step: Dict[str, Any], conn: sqlite3.Connection) -> Dict[str, Any]:
    """Re-run raw→canonical (or canonical-only) for declared sources."""
    import asyncio

    from ..ingestion.reprocess import reprocess_source

    params = step.get("params") or {}
    from_stage = str(params.get("from_stage") or "raw")
    if from_stage not in ("raw", "canonical"):
        raise ValueError(f"canonical_reprocess from_stage must be raw|canonical, got {from_stage!r}")
    # A re-map and its derived layers are separable, and sometimes must be:
    # reprocess_source defaults to running the source's full baseline signal
    # fan-out, which for any canonical-mapped source includes topic_clusters —
    # and a batch over _INCREMENTAL_MAX_BATCH forces a FULL cluster recompute
    # (repartition, not just relabel). A step that only needs canonical rows
    # corrected declares run_enrichment: false and leaves derivation to a
    # release that is ready for it.
    run_enrichment = bool(params.get("run_enrichment", True))
    source_ids = list(params.get("source_ids") or []) or _real_source_ids(conn)
    detail: Dict[str, Any] = {
        "sources": {},
        "from_stage": from_stage,
        "run_enrichment": run_enrichment,
    }
    for source_id in source_ids:
        try:
            out = asyncio.run(
                reprocess_source(
                    source_id=str(source_id),
                    dataset_id=str(params.get("dataset_id") or "default"),
                    from_stage=from_stage,  # type: ignore[arg-type]
                    force=bool(params.get("force", False)),
                    run_enrichment=run_enrichment,
                )
            )
            detail["sources"][source_id] = str(out.get("status") or "ok")
        except Exception as exc:  # noqa: BLE001
            detail["sources"][source_id] = f"error: {exc}"
    return detail


def _exec_derived_rebuild(step: Dict[str, Any], conn: sqlite3.Connection) -> Dict[str, Any]:
    """Rebuild derived layers: graph, topic clusters, and/or timeline."""
    params = step.get("params") or {}
    targets = list(params.get("targets") or params.get("layers") or [])
    if not targets:
        targets = ["entity_graph"]
    detail: Dict[str, Any] = {"targets": {}}
    for target in targets:
        name = str(target)
        try:
            if name in ("entity_graph", "graph", "entities_graph"):
                from ..features.entities.rebuild_subprocess import run_graph_rebuild

                detail["targets"][name] = dict(run_graph_rebuild(conn))
            elif name in ("topic_clusters", "clusters"):
                from ..features.signal.topic_clustering import recompute_topic_clusters

                detail["targets"][name] = dict(recompute_topic_clusters(conn) or {})
            elif name in ("topic_cluster_labels", "cluster_labels"):
                # Labels only: a prompt change owes existing clusters new names,
                # not a new partition (a recompute would reshuffle membership).
                from ..features.signal.topic_clustering import (
                    relabel_existing_clusters,
                    write_top_topics_signal_facts,
                )
                from ..storage.adapters.factory import AdapterFactory

                outcome = dict(relabel_existing_clusters(conn) or {})
                if outcome.get("changed"):
                    bundle = AdapterFactory.create("local_database", conn=conn)
                    outcome["top_topics_facts"] = write_top_topics_signal_facts(bundle, conn)
                detail["targets"][name] = outcome
            elif name in ("blackhole_rebuilds", "blackholes"):
                # Completed rebuilds are the ones carrying data from a surface
                # the job did not know about yet, and run_pending_rebuilds skips
                # exactly those.
                from ..features.lifecycle.blackhole_rebuild import rerun_all_rebuilds

                reports = rerun_all_rebuilds(conn)
                detail["targets"][name] = {
                    "entities": len(reports),
                    "cluster_labels_withdrawn": sum(
                        int(r.get("cluster_labels_withdrawn") or 0) for r in reports
                    ),
                    "cluster_member_previews_blanked": sum(
                        int(r.get("cluster_member_previews_blanked") or 0) for r in reports
                    ),
                }
            elif name in ("timeline",):
                from ..features.timeline_projection import repair_timeline_for_source

                written = 0
                for source_id in _real_source_ids(conn):
                    report = repair_timeline_for_source(
                        conn, source_id, missing_only=True, dry_run=False
                    )
                    written += int((report or {}).get("totals", {}).get("written", 0))
                detail["targets"][name] = {"written": written}
            else:
                raise ValueError(f"unknown derived_rebuild target {name!r}")
        except Exception as exc:  # noqa: BLE001
            detail["targets"][name] = {"error": str(exc)}
            raise
    return detail


def _exec_reembed(step: Dict[str, Any], conn: sqlite3.Connection) -> Dict[str, Any]:
    """Re-embed via enrichment job then rebuild ANN (vec0) from signal_embeddings."""
    params = dict(step.get("params") or {})
    params.setdefault("job_names", ["embeddings"])
    params.setdefault("include_signal", True)
    params.setdefault("force_reprocess", False)
    annotated = dict(step)
    annotated["params"] = params
    enrich_detail = _exec_enrichment_reprocess(annotated, conn)
    from ..storage.db.migrations.vector_storage_v4 import rebuild_vec_table
    from ..engine.backends.huggingface import embedding_model_profile

    profile = embedding_model_profile()
    dims = int(profile.get("dims") or 384)
    rebuild_vec_table(conn, dims=dims)
    return {"enrichment": enrich_detail, "vec_dims": dims}


def _exec_none(step: Dict[str, Any], conn: sqlite3.Connection) -> Dict[str, Any]:
    """Document-only step — no derived work."""
    return {"noop": True, "id": step.get("id")}


DEFAULT_EXECUTORS: Dict[str, ExecutorFn] = {
    "enrichment_reprocess": _exec_enrichment_reprocess,
    "engine_endpoint": _exec_engine_endpoint,
    "canonical_reprocess": _exec_canonical_reprocess,
    "derived_rebuild": _exec_derived_rebuild,
    "reembed": _exec_reembed,
    "none": _exec_none,
}


# --- runner -------------------------------------------------------------------


def run_pending_upgrades(
    conn: sqlite3.Connection,
    shipped: Optional[str] = None,
    executors: Optional[Dict[str, ExecutorFn]] = None,
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    """Execute the planned steps sequentially. Returns a summary dict.

    ``stop_event`` is checked at each step boundary so app shutdown can end the
    run without waiting out the remaining plan. Steps are not interrupted
    mid-flight — the boundary is the safe point — so a long step still finishes.
    Unset steps stay pending and are retried on the next boot, which is already
    how a failed or consent-blocked step behaves.
    """
    if not _enabled():
        logger.info("upgrade runner disabled (TOPOS_UPGRADE_RUNNER=off)")
        return {"disabled": True, "steps_run": 0, "steps_failed": 0}

    plan = plan_upgrade(conn, shipped=shipped)
    shipped_v = plan["shipped"]
    executors = executors or DEFAULT_EXECUTORS

    if plan["fresh_install"]:
        _stamp_baseline(conn, shipped_v)
        logger.info("fresh install: baseline stamped at %s, no re-derivation needed", shipped_v)
        return {"fresh_install": True, "steps_run": 0, "steps_failed": 0}

    if not plan["steps"]:
        if read_baseline(conn) != shipped_v:
            _stamp_baseline(conn, shipped_v)
        return {"steps_run": 0, "steps_failed": 0}

    ran = failed = pending_consent = 0
    failed_ids: set = set()
    steps = list(plan["steps"])
    steps_total = len(steps)
    declared = declaring_versions()
    stopped_early = False
    for step_index, step in enumerate(steps):
        if stop_event is not None and stop_event.is_set():
            logger.info(
                "upgrade run stopped at step %d/%d (app shutdown); remaining steps "
                "stay pending for the next boot",
                step_index + 1, steps_total,
            )
            stopped_early = True
            break
        step_id = str(step["id"])
        ledger_v = _ledger_version(step_id, shipped_v, declared)
        status = _effective_status(conn, step_id, ledger_v)
        if status == "done":
            continue
        consent = str(step.get("consent") or "auto").strip().lower()
        if consent == "prompt" and status in (None, "pending_consent"):
            # Sticky until POST /v1/upgrade/consent flips status to "pending".
            if status != "pending_consent":
                _ledger_set(
                    conn,
                    ledger_v,
                    step_id,
                    "pending_consent",
                    {
                        "cost": step.get("cost") or "slow",
                        "title": step.get("title"),
                        "why": step.get("why"),
                    },
                )
            pending_consent += 1
            logger.info("upgrade step %s waiting for consent (cost=%s)", step_id, step.get("cost"))
            continue
        deps = step.get("depends_on") or []
        blocked_by_consent = False
        for dep in deps:
            dep_status = _effective_status(conn, dep, _ledger_version(dep, shipped_v, declared))
            if dep in failed_ids or dep_status == "failed":
                logger.warning("step %s skipped: dependency failed (%s)", step_id, deps)
                failed_ids.add(step_id)
                failed += 1
                blocked_by_consent = True
                break
            if dep_status != "done":
                # Waiting on consent or still pending — retry next boot.
                blocked_by_consent = True
                break
        if blocked_by_consent and step_id not in failed_ids:
            continue
        if step_id in failed_ids:
            continue
        executor = executors.get(str(step["kind"]))
        if executor is None:
            _ledger_set(conn, ledger_v, step_id, "failed",
                        {"error": f"no executor for kind {step['kind']!r}",
                         "ran_under": shipped_v})
            failed_ids.add(step_id)
            failed += 1
            continue
        annotated = dict(step)
        annotated["_runner_step_index"] = step_index
        annotated["_runner_steps_total"] = steps_total
        _set_runner_state(
            running=True,
            waiting_for_ui=False,
            current_step=step_id,
            progress=_progress_snapshot(
                step_id=step_id,
                step_index=step_index,
                steps_total=steps_total,
                unit_label="steps",
                completed=0,
                total=1,
            ),
        )
        _ledger_set(conn, ledger_v, step_id, "running", started=True)
        logger.info("upgrade step %s (%s) starting", step_id, step["kind"])
        try:
            detail = executor(annotated, conn)
            record = dict(detail) if isinstance(detail, dict) else {}
            record["ran_under"] = shipped_v
            _ledger_set(conn, ledger_v, step_id, "done", record)
            ran += 1
        except Exception as exc:  # noqa: BLE001 — ledger the failure, keep the node up
            logger.warning("upgrade step %s failed: %s", step_id, exc)
            _ledger_set(conn, ledger_v, step_id, "failed",
                        {"error": str(exc), "ran_under": shipped_v})
            failed_ids.add(step_id)
            failed += 1
    _set_runner_state(running=False, waiting_for_ui=False, current_step=None, progress=None)

    # plan["steps"] carries this hop's window AND every step an earlier hop left
    # unfinished, so this gate is what keeps the baseline behind a failure
    # instead of stranding it one release back.
    all_done = all(
        _effective_status(conn, str(s["id"]), _ledger_version(str(s["id"]), shipped_v, declared))
        == "done"
        for s in plan["steps"]
    )
    if all_done:
        _stamp_baseline(conn, shipped_v)
        logger.info("upgrade to %s complete (%d steps)", shipped_v, ran)
    result = {
        "steps_run": ran,
        "steps_failed": failed,
        "steps_pending_consent": pending_consent,
        "baseline_advanced": all_done,
        "stopped_early": stopped_early,
    }
    _set_runner_state(last_result=result)
    return result


def start_background(
    conn: sqlite3.Connection,
    *,
    ready_event: Optional[threading.Event] = None,
    ui_grace_s: Optional[float] = None,
    ready_timeout_s: Optional[float] = None,
    stop_event: Optional[threading.Event] = None,
) -> Optional[threading.Thread]:
    """Boot entrypoint: plan quickly; heavy steps wait for UI, then run off-loop.

    ``ready_event`` should be set once the control plane (or local HTTP) is able
    to serve the React app's initial bootstrap. A grace window then lets those
    requests finish before enrichment reprocess contends for SQLite / Ollama /
    MPS. Fresh installs stamp-and-skip on a short-lived thread.

    ``stop_event`` is the caller's shutdown handle for THIS runner. Pass it and
    the thread becomes promptly joinable: every wait below wakes on it, and the
    upgrade will not start once it is set. Without it the thread sleeps out the
    full ready-timeout + grace (80s by default) after its app has already shut
    down, then runs migrations against a database the next app instance is also
    migrating — the write-gate convoy that timed out app startup in CI.

    Deliberately a per-runner event and NOT ``runtime_shutdown``: app startup
    calls ``clear_shutdown()``, so a newly starting app would erase the stop
    signal of the previous app's still-running runner — precisely the overlap
    this exists to end.
    """
    if not _enabled():
        return None

    def _stopping() -> bool:
        return stop_event is not None and stop_event.is_set()

    plan = plan_upgrade(conn)
    if plan["fresh_install"] or not plan["steps"]:
        # Cheap (stamp or no-op), but the stamp still commits under the write
        # gate — keep it off the event-loop thread that calls this at startup.
        def _stamp() -> None:
            if _stopping():
                return
            run_pending_upgrades(conn, stop_event=stop_event)

        thread = threading.Thread(target=_stamp, name="topos-upgrade-stamp", daemon=True)
        thread.start()
        return thread
    grace = _DEFAULT_UI_GRACE_SECONDS if ui_grace_s is None else max(0.0, float(ui_grace_s))
    ready_timeout = (
        _DEFAULT_READY_TIMEOUT_SECONDS if ready_timeout_s is None else max(0.0, float(ready_timeout_s))
    )
    logger.info(
        "upgrade %s → %s: %d step(s) queued (%s); waiting for UI readiness "
        "(timeout=%.0fs, grace=%.0fs) before starting",
        plan["baseline"], plan["shipped"], len(plan["steps"]),
        ", ".join(s["id"] for s in plan["steps"]),
        ready_timeout,
        grace,
    )
    _set_runner_state(
        waiting_for_ui=True,
        running=False,
        current_step=None,
        progress={
            "step_id": None,
            "percent": 0.0,
            "detail": "waiting_for_ui",
            "grace_seconds": grace,
        },
    )

    def _wait_for_ready() -> None:
        """Block until the UI is ready, the timeout expires, or we are stopped."""
        if ready_event is None:
            return
        timeout = ready_timeout if ready_timeout > 0 else None
        if stop_event is None:
            if not ready_event.wait(timeout=timeout):
                logger.warning(
                    "upgrade ready-event timed out after %.0fs; starting anyway",
                    ready_timeout,
                )
            return
        # Poll in slices so a shutdown mid-wait is noticed now rather than up to
        # ready_timeout later.
        deadline = None if timeout is None else time.monotonic() + timeout
        while not stop_event.is_set():
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                logger.warning(
                    "upgrade ready-event timed out after %.0fs; starting anyway",
                    ready_timeout,
                )
                return
            slice_s = _STOP_POLL_SLICE_S if remaining is None else min(_STOP_POLL_SLICE_S, remaining)
            if ready_event.wait(timeout=slice_s):
                return

    def _target() -> None:
        try:
            if _stopping():
                return
            _wait_for_ready()
            if _stopping():
                logger.info("upgrade runner stopped before starting (app shutdown)")
                return
            if grace > 0:
                logger.info(
                    "UI grace: deferring upgrade work for %.0fs so bootstrap can finish",
                    grace,
                )
                # Interruptible: a plain time.sleep(grace) held this thread past
                # its app's shutdown for the whole window.
                if stop_event is not None:
                    stop_event.wait(grace)
                else:
                    time.sleep(grace)
            if _stopping():
                logger.info("upgrade runner stopped during UI grace (app shutdown)")
                return
            run_pending_upgrades(conn, stop_event=stop_event)
        finally:
            _set_runner_state(waiting_for_ui=False)

    thread = threading.Thread(target=_target, name="topos-upgrade-runner", daemon=True)
    thread.start()
    return thread


def runner_status(conn: sqlite3.Connection) -> Dict[str, Any]:
    plan = plan_upgrade(conn)
    shipped_v = plan["shipped"]
    declared = declaring_versions()
    pending_consent_steps: List[Dict[str, Any]] = []
    for step in plan["steps"]:
        step_id = str(step["id"])
        status = _effective_status(conn, step_id, _ledger_version(step_id, shipped_v, declared))
        if status == "pending_consent" or (
            str(step.get("consent") or "auto").lower() == "prompt"
            and status in (None, "pending_consent")
        ):
            pending_consent_steps.append(
                {
                    "id": step_id,
                    "title": step.get("title"),
                    "why": step.get("why"),
                    "cost": step.get("cost") or "slow",
                    "kind": step.get("kind"),
                }
            )
    with _state_lock:
        state = dict(_runner_state)
    return {
        "enabled": _enabled(),
        "shipped": plan["shipped"],
        "baseline": read_baseline(conn),
        "fresh_install": plan["fresh_install"],
        "pending_steps": [s["id"] for s in plan["steps"]],
        "pending_consent_steps": pending_consent_steps,
        "running": state["running"],
        "waiting_for_ui": state.get("waiting_for_ui", False),
        "current_step": state["current_step"],
        "progress": state.get("progress"),
        "last_result": state["last_result"],
        "ledger": ledger_rows(conn),
    }


def consent_upgrade_step(
    conn: sqlite3.Connection,
    step_id: str,
    *,
    shipped: Optional[str] = None,
) -> Dict[str, Any]:
    """Approve a ``pending_consent`` step so the next runner pass executes it."""
    plan = plan_upgrade(conn, shipped=shipped)
    shipped_v = plan["shipped"]
    match = next((s for s in plan["steps"] if str(s.get("id")) == str(step_id)), None)
    if match is None:
        raise ValueError(f"unknown upgrade step {step_id!r} for {shipped_v}")
    ledger_v = _ledger_version(str(step_id), shipped_v)
    status = _effective_status(conn, str(step_id), ledger_v)
    if status not in (None, "pending_consent"):
        return {
            "status": "ok",
            "step_id": step_id,
            "ledger_status": status,
            "message": "step already past consent",
        }
    _ledger_set(
        conn,
        ledger_v,
        str(step_id),
        "pending",
        {"consented_at": _now(), "cost": match.get("cost"), "why": match.get("why")},
    )
    return {"status": "ok", "step_id": step_id, "ledger_status": "pending"}
