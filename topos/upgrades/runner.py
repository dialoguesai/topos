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

Executors are injectable for tests; the real ones dispatch step kinds to
engine internals (no HTTP self-calls).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from . import steps_between

logger = logging.getLogger("topos.upgrades.runner")

_BASELINE_KEY = "engine.upgrade.baseline"
_BOOTSTRAP_BASELINE = "1.1.0"

ExecutorFn = Callable[[Dict[str, Any], sqlite3.Connection], Dict[str, Any]]

# Status snapshot for the /upgrade/status surface (module-level, single node).
_state_lock = threading.Lock()
_runner_state: Dict[str, Any] = {"running": False, "current_step": None, "last_result": None}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _enabled() -> bool:
    return os.environ.get("TOPOS_UPGRADE_RUNNER", "on").strip().lower() not in (
        "0", "false", "off", "no",
    )


def _shipped_version() -> str:
    from topos.__version__ import __version__

    return __version__


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
    _ensure_engine_config(conn)
    conn.execute(
        "INSERT INTO engine_config (key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
        (_BASELINE_KEY, version),
    )
    conn.commit()


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
    conn.commit()


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
    """Walk real sources through the enrichment core with force_reprocess."""
    import asyncio

    from ..api.enrichment import _process_enrichment_core

    params = step.get("params") or {}
    job_names = params.get("job_names") or None
    detail: Dict[str, Any] = {"sources": {}}
    for source_id in _real_source_ids(conn):
        try:
            out = asyncio.run(
                _process_enrichment_core(
                    source_id=source_id,
                    job_names=list(job_names) if job_names else None,
                    force_reprocess=bool(params.get("force_reprocess", True)),
                )
            )
            detail["sources"][source_id] = str(out.get("status") or "ok")
        except ValueError:
            detail["sources"][source_id] = "unknown_source_skipped"
        except Exception as exc:  # noqa: BLE001 — one source must not kill the walk
            detail["sources"][source_id] = f"error: {exc}"
    return detail


def _exec_engine_endpoint(step: Dict[str, Any], conn: sqlite3.Connection) -> Dict[str, Any]:
    """Dispatch declared endpoints to engine internals — no HTTP self-call."""
    path = str((step.get("params") or {}).get("path") or "")
    if path == "/v1/signal/entities/graph/rebuild":
        from ..features.entities.maintenance import rebuild_entity_graph

        return dict(rebuild_entity_graph(conn))
    raise ValueError(f"no internal dispatch for endpoint step: {path!r}")


DEFAULT_EXECUTORS: Dict[str, ExecutorFn] = {
    "enrichment_reprocess": _exec_enrichment_reprocess,
    "engine_endpoint": _exec_engine_endpoint,
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

    ran = failed = 0
    failed_ids: set = set()
    for step in plan["steps"]:
        step_id = str(step["id"])
        status = _ledger_status(conn, shipped_v, step_id)
        if status == "done":
            continue
        deps = step.get("depends_on") or []
        if any(d in failed_ids or _ledger_status(conn, shipped_v, d) != "done" for d in deps):
            logger.warning("step %s skipped: dependency not done (%s)", step_id, deps)
            failed_ids.add(step_id)
            failed += 1
            continue
        executor = executors.get(str(step["kind"]))
        if executor is None:
            _ledger_set(conn, shipped_v, step_id, "failed",
                        {"error": f"no executor for kind {step['kind']!r}"})
            failed_ids.add(step_id)
            failed += 1
            continue
        with _state_lock:
            _runner_state.update(running=True, current_step=step_id)
        _ledger_set(conn, shipped_v, step_id, "running", started=True)
        logger.info("upgrade step %s (%s) starting", step_id, step["kind"])
        try:
            detail = executor(step, conn)
            _ledger_set(conn, shipped_v, step_id, "done", detail if isinstance(detail, dict) else None)
            ran += 1
        except Exception as exc:  # noqa: BLE001 — ledger the failure, keep the node up
            logger.warning("upgrade step %s failed: %s", step_id, exc)
            _ledger_set(conn, shipped_v, step_id, "failed", {"error": str(exc)})
            failed_ids.add(step_id)
            failed += 1
    with _state_lock:
        _runner_state.update(running=False, current_step=None)

    all_done = all(
        _ledger_status(conn, shipped_v, str(s["id"])) == "done" for s in plan["steps"]
    )
    if all_done:
        _stamp_baseline(conn, shipped_v)
        logger.info("upgrade to %s complete (%d steps)", shipped_v, ran)
    result = {"steps_run": ran, "steps_failed": failed, "baseline_advanced": all_done}
    with _state_lock:
        _runner_state["last_result"] = result
    return result


def start_background(conn: sqlite3.Connection) -> Optional[threading.Thread]:
    """Boot entrypoint: plan quickly; heavy steps run off the event loop."""
    if not _enabled():
        return None
    plan = plan_upgrade(conn)
    if plan["fresh_install"] or not plan["steps"]:
        run_pending_upgrades(conn)  # cheap: stamp/no-op inline
        return None
    logger.info(
        "upgrade %s → %s: %d step(s) queued (%s)",
        plan["baseline"], plan["shipped"], len(plan["steps"]),
        ", ".join(s["id"] for s in plan["steps"]),
    )
    thread = threading.Thread(
        target=run_pending_upgrades, args=(conn,), name="topos-upgrade-runner", daemon=True
    )
    thread.start()
    return thread


def runner_status(conn: sqlite3.Connection) -> Dict[str, Any]:
    plan = plan_upgrade(conn)
    with _state_lock:
        state = dict(_runner_state)
    return {
        "enabled": _enabled(),
        "shipped": plan["shipped"],
        "baseline": read_baseline(conn),
        "fresh_install": plan["fresh_install"],
        "pending_steps": [s["id"] for s in plan["steps"]],
        "running": state["running"],
        "current_step": state["current_step"],
        "last_result": state["last_result"],
        "ledger": ledger_rows(conn),
    }
