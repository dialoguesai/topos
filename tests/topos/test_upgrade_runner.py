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
