"""Startup upgrade runner: manifests get a mailman.

Boot compares the stamped upgrade baseline against the shipped version and
executes steps_between() through the derivation ledger — resumable, injectable
executors, kill-switch. Bootstrap rule: data present + no baseline means the
node predates the ledger (all such nodes are ≤1.1.0 → run everything since).
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.storage.db.migrations import apply_all_migrations
from topos.upgrades import steps_between
from topos.upgrades.runner import (
    plan_upgrade,
    read_baseline,
    run_pending_upgrades,
)


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "u.db"), check_same_thread=False)
    apply_all_migrations(c)
    yield c
    c.close()


def _seed_data(conn):
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, is_self) "
        "VALUES ('e1', 'person', 'A', 'a', 0)"
    )
    conn.commit()


def _recording_executors(log):
    def exec_reprocess(step, conn):
        log.append(("reprocess", step["id"]))
        return {"ok": True}

    def exec_endpoint(step, conn):
        log.append(("endpoint", step["id"]))
        return {"ok": True}

    return {"enrichment_reprocess": exec_reprocess, "engine_endpoint": exec_endpoint}


def test_fresh_install_stamps_and_skips(conn):
    # empty DB → nothing derived yet; ingestion derives with current code.
    plan = plan_upgrade(conn, shipped="1.2.0")
    assert plan["fresh_install"] is True
    log = []
    result = run_pending_upgrades(conn, shipped="1.2.0", executors=_recording_executors(log))
    assert log == []
    assert read_baseline(conn) == "1.2.0"
    assert result["steps_run"] == 0


def test_bootstrap_rule_data_without_baseline_means_1_1_0(conn):
    _seed_data(conn)
    plan = plan_upgrade(conn, shipped="1.2.0")
    assert plan["fresh_install"] is False
    assert plan["baseline"] == "1.1.0"
    ids = [s["id"] for s in plan["steps"]]
    assert "reextract-entities" in ids and "rebuild-entity-graph" in ids


def test_runs_steps_in_order_and_advances_baseline(conn):
    _seed_data(conn)
    log = []
    result = run_pending_upgrades(conn, shipped="1.2.0", executors=_recording_executors(log))
    assert [op for op, _ in log] == ["reprocess", "endpoint"]
    assert [sid for _, sid in log] == ["reextract-entities", "rebuild-entity-graph"]
    assert read_baseline(conn) == "1.2.0"
    assert result["steps_run"] == 2
    statuses = dict(conn.execute("SELECT step_id, status FROM derivation_ledger").fetchall())
    assert statuses == {"reextract-entities": "done", "rebuild-entity-graph": "done"}


def test_noop_when_baseline_current(conn):
    _seed_data(conn)
    log = []
    run_pending_upgrades(conn, shipped="1.2.0", executors=_recording_executors(log))
    log.clear()
    result = run_pending_upgrades(conn, shipped="1.2.0", executors=_recording_executors(log))
    assert log == [] and result["steps_run"] == 0


def test_failed_step_blocks_baseline_and_retries_next_boot(conn):
    _seed_data(conn)
    attempts = []

    def failing(step, conn_):
        attempts.append(step["id"])
        raise RuntimeError("simulated interrupt")

    execs = _recording_executors([])
    execs["enrichment_reprocess"] = failing
    result = run_pending_upgrades(conn, shipped="1.2.0", executors=execs)
    # the real failure AND its dependency-blocked follower both count
    assert result["steps_failed"] == 2
    assert read_baseline(conn) != "1.2.0"
    status = conn.execute(
        "SELECT status FROM derivation_ledger WHERE step_id='reextract-entities'"
    ).fetchone()[0]
    assert status == "failed"
    # dependent step must NOT have run after its dependency failed
    dep = conn.execute(
        "SELECT status FROM derivation_ledger WHERE step_id='rebuild-entity-graph'"
    ).fetchone()
    assert dep is None or dep[0] in ("pending",)

    # next boot: retry succeeds, baseline advances
    log = []
    result = run_pending_upgrades(conn, shipped="1.2.0", executors=_recording_executors(log))
    assert result["steps_run"] == 2
    assert read_baseline(conn) == "1.2.0"


def _stamp(conn, version):
    # engine_config is bootstrapped outside the migration set, so go through
    # the runner's own stamp rather than assuming the table exists.
    from topos.upgrades.runner import _stamp_baseline

    _stamp_baseline(conn, version)


def _ledger(conn, version, step_id, status):
    conn.execute(
        "INSERT INTO derivation_ledger (version, step_id, status) VALUES (?, ?, ?)",
        (version, step_id, status),
    )
    conn.commit()


def test_failed_step_is_retried_after_the_baseline_moves_past_it(conn):
    """A failure must survive later releases, not fall out of the version window.

    Live regression (2026-08-09): `backfill-attention-triage-redo`, declared in
    1.3.7, failed under shipped 1.3.9 — and was ledgered under 1.3.9, because
    rows were keyed by whatever was shipping rather than by the declaring
    release. The node went on to 1.3.10 and 1.3.11, the baseline passed 1.3.7,
    and `steps_between(baseline, shipped)` could no longer name the step. It
    was unretryable forever while the banner promised a retry every restart.
    """
    _seed_data(conn)
    _stamp(conn, "1.3.8")  # baseline already past the declaring release
    _ledger(conn, "1.3.9", "backfill-attention-triage-redo", "failed")

    # This hop's window genuinely cannot see a 1.3.7 step — that is the trap.
    assert "backfill-attention-triage-redo" not in [
        s["id"] for s in steps_between("1.3.8", "1.3.11")
    ]
    plan = plan_upgrade(conn, shipped="1.3.11")
    assert "backfill-attention-triage-redo" in [s["id"] for s in plan["steps"]]

    log = []
    run_pending_upgrades(conn, shipped="1.3.11", executors=_recording_executors(log))
    assert ("reprocess", "backfill-attention-triage-redo") in log
    assert read_baseline(conn) == "1.3.11"
    # The retry files under the declaring release, collapsing the legacy row.
    assert ("1.3.7", "done") in conn.execute(
        "SELECT version, status FROM derivation_ledger "
        "WHERE step_id='backfill-attention-triage-redo'"
    ).fetchall()


def test_release_with_no_steps_does_not_stamp_over_a_failure(conn):
    """The live node's exact state: baseline == shipped, one orphaned failure.

    1.3.10 and 1.3.11 declare no steps, so the window is empty and the old
    short circuit stamped the baseline and returned without ever looking at the
    outstanding failure.
    """
    _seed_data(conn)
    _stamp(conn, "1.3.11")
    _ledger(conn, "1.3.9", "backfill-attention-triage-redo", "failed")
    assert steps_between("1.3.11", "1.3.11") == []

    log = []
    run_pending_upgrades(conn, shipped="1.3.11", executors=_recording_executors(log))
    assert ("reprocess", "backfill-attention-triage-redo") in log


def test_baseline_never_advances_past_an_outstanding_failure(conn):
    """The stamp gate must see failures from earlier hops, not just this one."""
    _seed_data(conn)
    _stamp(conn, "1.3.8")
    _ledger(conn, "1.3.9", "backfill-attention-triage-redo", "failed")

    def failing(step, conn_):
        raise RuntimeError("stderr closed")

    execs = _recording_executors([])
    execs["enrichment_reprocess"] = failing
    run_pending_upgrades(conn, shipped="1.3.11", executors=execs)
    assert read_baseline(conn) != "1.3.11"


def test_legacy_row_under_a_later_shipped_version_still_counts_as_done(conn):
    """Rows written before the re-keying must not cause a re-run.

    `retry-recorded-derivation-debt` (declared 1.3.9) ledgered 'done' under
    both 1.3.9 and 1.3.10 on real nodes. Reading only the declaring version's
    row would miss a 'done' filed under a later one and re-run finished work.
    """
    _seed_data(conn)
    conn.execute(
        "INSERT INTO derivation_ledger (version, step_id, status) "
        "VALUES ('1.3.10', 'retry-recorded-derivation-debt', 'done')"
    )
    conn.commit()
    log = []
    run_pending_upgrades(conn, shipped="1.3.10", executors=_recording_executors(log))
    assert "retry-recorded-derivation-debt" not in [sid for _, sid in log]


def test_fresh_install_does_not_drag_back_unrun_history(conn):
    """No ledger row means never owed — only an unfinished row is retried."""
    plan = plan_upgrade(conn, shipped="1.3.11")
    assert plan["fresh_install"] is True
    run_pending_upgrades(conn, shipped="1.3.11", executors=_recording_executors([]))
    assert read_baseline(conn) == "1.3.11"
    # A later boot on the stamped baseline must stay empty, not resurrect 1.2.0.
    assert plan_upgrade(conn, shipped="1.3.11")["steps"] == []


def test_interrupted_running_step_is_retried(conn):
    _seed_data(conn)
    conn.execute(
        "INSERT INTO derivation_ledger (version, step_id, status, started_at) "
        "VALUES ('1.2.0', 'reextract-entities', 'running', '2026-07-10T00:00:00Z')"
    )
    conn.commit()
    log = []
    run_pending_upgrades(conn, shipped="1.2.0", executors=_recording_executors(log))
    assert ("reprocess", "reextract-entities") in log


def test_kill_switch(conn, monkeypatch):
    _seed_data(conn)
    monkeypatch.setenv("TOPOS_UPGRADE_RUNNER", "off")
    log = []
    result = run_pending_upgrades(conn, shipped="1.2.0", executors=_recording_executors(log))
    assert log == [] and result.get("disabled") is True
    assert read_baseline(conn) is None  # nothing stamped; retries when re-enabled


def _reprocess_calls(conn, monkeypatch, params, step_id="step"):
    """Run _exec_enrichment_reprocess with the enrichment core stubbed out."""
    from topos.upgrades import runner as upgrade_runner

    conn.execute(
        "INSERT INTO timeline (event_at, record_id, source_id) "
        "VALUES (datetime('now'), 'r1', 'gmail_messages')"
    )
    conn.commit()

    calls = []

    async def fake_core(**kwargs):
        calls.append(kwargs)
        return {"status": "ok"}

    monkeypatch.setattr("topos.api.enrichment._process_enrichment_core", fake_core)
    step = {
        "id": step_id,
        "kind": "enrichment_reprocess",
        "params": params,
        "_runner_step_index": 0,
        "_runner_steps_total": 1,
    }
    detail = upgrade_runner._exec_enrichment_reprocess(step, conn)
    return calls, detail


def test_enrichment_reprocess_keeps_signal_lane_off(conn, monkeypatch):
    """Upgrade steps must not fan into embeddings/cluster LLM/dimension briefs."""
    calls, detail = _reprocess_calls(
        conn,
        monkeypatch,
        {"job_names": ["attention_triage"], "force_reprocess": True},
        step_id="backfill-attention-triage",
    )
    assert calls, "enrichment core should have been invoked"
    # include_signal stays off: that flag guards the FULL fan-out, which is what
    # starves startup UI traffic. The narrow list below is not that fan-out.
    assert calls[0]["include_signal"] is False
    assert detail["include_signal"] is False


def test_signal_jobs_route_to_signal_lane_not_run_canonical(conn, monkeypatch):
    """attention_triage is a SIGNAL job; run_canonical() would silently drop it.

    Regression for the 1.3.0–1.3.6 defect: the step ledgered 'done' with
    jobs_run=0 and wrote zero triage_verdicts on every real node, because the
    runner handed a SIGNAL_JOB_REGISTRY name to the canonical lane, which
    filters job_names against CANONICAL_JOBS.
    """
    from topos.enrichment.jobs import CANONICAL_JOBS, SIGNAL_JOB_REGISTRY

    assert "attention_triage" in SIGNAL_JOB_REGISTRY
    assert "attention_triage" not in {j.get_job_name() for j in CANONICAL_JOBS}

    calls, detail = _reprocess_calls(
        conn,
        monkeypatch,
        {"job_names": ["attention_triage"], "force_reprocess": True},
        step_id="backfill-attention-triage",
    )
    assert calls[0]["signal_job_names"] == ["attention_triage"]
    # Empty, NOT None: None would widen back out to the source's full default
    # canonical job set.
    assert calls[0]["job_names"] == []
    assert detail["signal_jobs"] == ["attention_triage"]


def test_canonical_jobs_still_route_to_canonical_lane(conn, monkeypatch):
    calls, _ = _reprocess_calls(
        conn, monkeypatch, {"job_names": ["entities"], "force_reprocess": True}
    )
    assert calls[0]["job_names"] == ["entities"]
    assert not calls[0]["signal_job_names"]


def test_unknown_job_name_runs_nothing_instead_of_everything(conn, monkeypatch):
    """A manifest typo must not silently widen into the full default job set."""
    calls, detail = _reprocess_calls(
        conn, monkeypatch, {"job_names": ["not_a_real_job"], "force_reprocess": True}
    )
    assert calls[0]["job_names"] == []
    assert calls[0]["signal_job_names"] == []
    assert detail["unknown_jobs"] == ["not_a_real_job"]


def test_no_declared_jobs_preserves_source_defaults(conn, monkeypatch):
    """Steps that declare no job_names still fall back to the source's defaults."""
    calls, _ = _reprocess_calls(conn, monkeypatch, {"force_reprocess": True})
    assert calls[0]["job_names"] is None
    assert calls[0]["signal_job_names"] is None


def test_start_background_waits_for_ready_event(conn, monkeypatch):
    import threading
    import time

    from topos.upgrades.runner import start_background

    _seed_data(conn)
    ran = threading.Event()

    def fake_run(conn_, shipped=None, executors=None):
        ran.set()
        return {"steps_run": 0, "steps_failed": 0}

    monkeypatch.setattr("topos.upgrades.runner.run_pending_upgrades", fake_run)
    monkeypatch.setattr("topos.upgrades.runner.plan_upgrade", lambda _c: {
        "shipped": "1.3.0",
        "baseline": "1.2.7",
        "fresh_install": False,
        "steps": [{"id": "backfill-attention-triage"}],
    })

    ready = threading.Event()
    thread = start_background(conn, ready_event=ready, ui_grace_s=0.05, ready_timeout_s=2.0)
    assert thread is not None
    time.sleep(0.1)
    assert not ran.is_set(), "upgrade must not start before UI ready-event"
    ready.set()
    assert ran.wait(timeout=2.0), "upgrade should start after ready-event + grace"
    thread.join(timeout=2.0)


# --- engine_endpoint dispatch ------------------------------------------------


def test_derivation_debt_retry_endpoint_dispatches_to_the_sweep(conn, monkeypatch) -> None:
    """The `retry-recorded-derivation-debt` step must reach the debt sweep.

    Recorded debt is not self-healing — a failed row is only re-queued by the
    next organic failure of the same (batch, job), and recover_stale_jobs resets
    'running' rows, never 'failed' ones. If this dispatch is missing the step
    raises "no internal dispatch", the upgrade fails, and every node keeps the
    derivation gap the fix was meant to close.
    """
    from topos.upgrades.runner import _exec_engine_endpoint

    seen: dict = {}

    async def fake_retry(conn_, *, source_id=None, limit=20, dry_run=False):
        seen.update({"conn": conn_, "source_id": source_id, "limit": limit})
        return {"attempted": 2, "recovered": ["b1:timeline"], "pending_after": 0}

    monkeypatch.setattr(
        "topos.enrichment.derivation_recovery.retry_pending_derivations", fake_retry
    )

    out = _exec_engine_endpoint(
        {"params": {"method": "POST", "path": "/v1/signal/derivation-debt/retry", "limit": 200}},
        conn,
    )
    assert out["attempted"] == 2
    assert out["pending_after"] == 0
    assert seen["conn"] is conn, "the step's connection must be the one swept"
    assert seen["limit"] == 200, "the manifest's limit must reach the sweep"


def test_engine_endpoint_still_rejects_an_unknown_path(conn) -> None:
    """Adding a dispatch must not turn unknown endpoints into silent no-ops."""
    from topos.upgrades.runner import _exec_engine_endpoint

    with pytest.raises(ValueError, match="no internal dispatch"):
        _exec_engine_endpoint({"params": {"path": "/v1/does/not/exist"}}, conn)


def test_shipped_manifest_declares_the_debt_retry_step() -> None:
    """The dispatch is only reachable if the manifest actually declares it.

    Searches every release rather than `unreleased`: cut_release.py stamps the
    staging entry with the version being cut and resets `unreleased` to empty,
    so pinning this to `unreleased` fails the release gate the moment the step
    ships — which is exactly what it did on the 1.3.9 cut. The step's home
    changes at release time; that it is declared at all does not.
    """
    import json
    from pathlib import Path

    import topos

    data = json.loads(
        (Path(topos.__file__).parent / "upgrades" / "manifests.json").read_text()
    )
    steps = [
        s
        for release in data["releases"]
        for s in release.get("steps", [])
        if s.get("id") == "retry-recorded-derivation-debt"
    ]
    assert steps, "no release declares retry-recorded-derivation-debt"
    assert len(steps) == 1, "the debt sweep should be declared once, not re-run every release"
    step = steps[0]
    assert step["kind"] == "engine_endpoint"
    assert step["params"]["path"] == "/v1/signal/derivation-debt/retry"
    assert step["consent"] == "auto", "repairing recorded loss should not need a prompt"
