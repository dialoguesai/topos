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

from . import steps_between
from ..storage.db.write_gate import commit_connection, with_db_write

logger = logging.getLogger("topos.upgrades.runner")

_BASELINE_KEY = "engine.upgrade.baseline"
_BOOTSTRAP_BASELINE = "1.1.0"
_DEFAULT_UI_GRACE_SECONDS = 20.0
_DEFAULT_READY_TIMEOUT_SECONDS = 60.0

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
    steps = [] if fresh or baseline == shipped else steps_between(baseline, shipped)
    return {
        "shipped": shipped,
        "baseline": baseline,
        "fresh_install": fresh,
        "steps": steps,
    }


# --- ledger -----------------------------------------------------------------


def _ledger_status(conn: sqlite3.Connection, version: str, step_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT status FROM derivation_ledger WHERE version=? AND step_id=?",
        (version, step_id),
    ).fetchone()
    return str(row[0]) if row else None


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
    raise ValueError(f"no internal dispatch for endpoint step: {path!r}")


def _exec_canonical_reprocess(step: Dict[str, Any], conn: sqlite3.Connection) -> Dict[str, Any]:
    """Re-run raw→canonical (or canonical-only) for declared sources."""
    import asyncio

    from ..ingestion.reprocess import reprocess_source

    params = step.get("params") or {}
    from_stage = str(params.get("from_stage") or "raw")
    if from_stage not in ("raw", "canonical"):
        raise ValueError(f"canonical_reprocess from_stage must be raw|canonical, got {from_stage!r}")
    source_ids = list(params.get("source_ids") or []) or _real_source_ids(conn)
    detail: Dict[str, Any] = {"sources": {}, "from_stage": from_stage}
    for source_id in source_ids:
        try:
            out = asyncio.run(
                reprocess_source(
                    source_id=str(source_id),
                    dataset_id=str(params.get("dataset_id") or "default"),
                    from_stage=from_stage,  # type: ignore[arg-type]
                    force=bool(params.get("force", False)),
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
) -> Dict[str, Any]:
    """Execute the planned steps sequentially. Returns a summary dict."""
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
    for step_index, step in enumerate(steps):
        step_id = str(step["id"])
        status = _ledger_status(conn, shipped_v, step_id)
        if status == "done":
            continue
        consent = str(step.get("consent") or "auto").strip().lower()
        if consent == "prompt" and status in (None, "pending_consent"):
            # Sticky until POST /v1/upgrade/consent flips status to "pending".
            if status != "pending_consent":
                _ledger_set(
                    conn,
                    shipped_v,
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
            dep_status = _ledger_status(conn, shipped_v, dep)
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
            _ledger_set(conn, shipped_v, step_id, "failed",
                        {"error": f"no executor for kind {step['kind']!r}"})
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
        _ledger_set(conn, shipped_v, step_id, "running", started=True)
        logger.info("upgrade step %s (%s) starting", step_id, step["kind"])
        try:
            detail = executor(annotated, conn)
            _ledger_set(conn, shipped_v, step_id, "done", detail if isinstance(detail, dict) else None)
            ran += 1
        except Exception as exc:  # noqa: BLE001 — ledger the failure, keep the node up
            logger.warning("upgrade step %s failed: %s", step_id, exc)
            _ledger_set(conn, shipped_v, step_id, "failed", {"error": str(exc)})
            failed_ids.add(step_id)
            failed += 1
    _set_runner_state(running=False, waiting_for_ui=False, current_step=None, progress=None)

    all_done = all(
        _ledger_status(conn, shipped_v, str(s["id"])) == "done" for s in plan["steps"]
    )
    if all_done:
        _stamp_baseline(conn, shipped_v)
        logger.info("upgrade to %s complete (%d steps)", shipped_v, ran)
    result = {
        "steps_run": ran,
        "steps_failed": failed,
        "steps_pending_consent": pending_consent,
        "baseline_advanced": all_done,
    }
    _set_runner_state(last_result=result)
    return result


def start_background(
    conn: sqlite3.Connection,
    *,
    ready_event: Optional[threading.Event] = None,
    ui_grace_s: Optional[float] = None,
    ready_timeout_s: Optional[float] = None,
) -> Optional[threading.Thread]:
    """Boot entrypoint: plan quickly; heavy steps wait for UI, then run off-loop.

    ``ready_event`` should be set once the control plane (or local HTTP) is able
    to serve the React app's initial bootstrap. A grace window then lets those
    requests finish before enrichment reprocess contends for SQLite / Ollama /
    MPS. Fresh installs stamp-and-skip on a short-lived thread.
    """
    if not _enabled():
        return None
    plan = plan_upgrade(conn)
    if plan["fresh_install"] or not plan["steps"]:
        # Cheap (stamp or no-op), but the stamp still commits under the write
        # gate — keep it off the event-loop thread that calls this at startup.
        thread = threading.Thread(
            target=lambda: run_pending_upgrades(conn),
            name="topos-upgrade-stamp",
            daemon=True,
        )
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

    def _target() -> None:
        try:
            if ready_event is not None:
                signaled = ready_event.wait(timeout=ready_timeout if ready_timeout > 0 else None)
                if not signaled:
                    logger.warning(
                        "upgrade ready-event timed out after %.0fs; starting anyway",
                        ready_timeout,
                    )
            if grace > 0:
                logger.info(
                    "UI grace: deferring upgrade work for %.0fs so bootstrap can finish",
                    grace,
                )
                time.sleep(grace)
            run_pending_upgrades(conn)
        finally:
            _set_runner_state(waiting_for_ui=False)

    thread = threading.Thread(target=_target, name="topos-upgrade-runner", daemon=True)
    thread.start()
    return thread


def runner_status(conn: sqlite3.Connection) -> Dict[str, Any]:
    plan = plan_upgrade(conn)
    shipped_v = plan["shipped"]
    pending_consent_steps: List[Dict[str, Any]] = []
    for step in plan["steps"]:
        step_id = str(step["id"])
        if _ledger_status(conn, shipped_v, step_id) == "pending_consent" or (
            str(step.get("consent") or "auto").lower() == "prompt"
            and _ledger_status(conn, shipped_v, step_id) in (None, "pending_consent")
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
    status = _ledger_status(conn, shipped_v, str(step_id))
    if status not in (None, "pending_consent"):
        return {
            "status": "ok",
            "step_id": step_id,
            "ledger_status": status,
            "message": "step already past consent",
        }
    _ledger_set(
        conn,
        shipped_v,
        str(step_id),
        "pending",
        {"consented_at": _now(), "cost": match.get("cost"), "why": match.get("why")},
    )
    return {"status": "ok", "step_id": step_id, "ledger_status": "pending"}
