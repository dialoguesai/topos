"""The local-sync runner table (CONTRACTS.md §2.8): one registry, four former literals read it.

iMessage and Signal keep exactly their current behaviour; the three AI-coding-tool sources join
them. A snapshot pins the table, and each former literal site (the job executor's guard and
branch, the enqueue door, the relay guard, the scheduler's eligibility) is shown to read the
table by registering an invented runner and watching it dispatch. Synthetic databases only.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

import topos.core.handlers as hub
from topos.pipeline import job_runner
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up

DS = "owner:topos:enrolled"


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "node.db"), check_same_thread=False)
    apply_pipeline_jobs_v1_up(db)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: db)
    monkeypatch.setattr(hub, "get_db_connection", lambda: db)
    yield db
    db.close()


def _fake_runner(source_id: str, calls: list, *, schedulable: bool = True):
    from topos.ingestion.local_sync_jobs import LocalSyncRunner

    def run(dataset_id, *, sync_options=None, progress_cb=None, batch_size=5000, **kw):
        calls.append({"dataset_id": dataset_id, "sync_options": sync_options, "batch_size": batch_size})
        return {"status": "ok", "outcome": "imported", "records_processed": 2, "last_record_id": "x:2"}

    return LocalSyncRunner(source_id=source_id, run=run, schedulable=schedulable)


# --- the snapshot -------------------------------------------------------------


def test_the_runner_table_is_this_snapshot() -> None:
    """Literal keys: a source that silently joins or leaves the lane is a finding."""
    from topos.ingestion.local_sync_jobs import LOCAL_SYNC_RUNNERS, SUPPORTED_SYNC_SOURCE_IDS

    assert tuple(LOCAL_SYNC_RUNNERS) == (
        "imessage",
        "signal",
        "claude_code_sessions",
        "codex_sessions",
        "cursor_sessions",
    )
    assert tuple(SUPPORTED_SYNC_SOURCE_IDS) == tuple(LOCAL_SYNC_RUNNERS)
    for source_id, runner in LOCAL_SYNC_RUNNERS.items():
        assert runner.source_id == source_id
        assert callable(runner.run)


def test_imessage_keeps_its_current_behaviour() -> None:
    from topos.ingestion.local_sync_jobs import LOCAL_SYNC_RUNNERS

    imessage = LOCAL_SYNC_RUNNERS["imessage"]
    assert imessage.schedulable is True
    assert imessage.default_options(None) == {"mode": "3m"}
    assert imessage.default_options({"mode": "since_last"}) == {"mode": "since_last"}
    assert imessage.describe_checkpoint is not None
    assert imessage.pre_enqueue_refusal is not None
    assert imessage.post_sync is not None
    assert imessage.after_settle is not None


def test_signal_keeps_its_current_behaviour() -> None:
    from topos.ingestion.local_sync_jobs import LOCAL_SYNC_RUNNERS

    signal = LOCAL_SYNC_RUNNERS["signal"]
    assert signal.schedulable is False
    assert signal.default_options is None
    assert signal.describe_checkpoint is None
    assert signal.pre_enqueue_refusal is None
    assert signal.after_settle is None
    assert signal.post_sync is not None


@pytest.mark.parametrize("source_id", ["claude_code_sessions", "codex_sessions", "cursor_sessions"])
def test_the_ai_tools_are_schedulable_and_refuse_before_a_job_exists(source_id: str) -> None:
    from topos.ingestion.local_sync_jobs import LOCAL_SYNC_RUNNERS

    runner = LOCAL_SYNC_RUNNERS[source_id]
    assert runner.schedulable is True
    assert runner.pre_enqueue_refusal is not None
    assert runner.describe_checkpoint is not None
    assert runner.default_options(None) == {"mode": "since_last"}


def test_imessage_describe_checkpoint_is_the_existing_one(conn: sqlite3.Connection) -> None:
    from topos.ingestion.local_sync import describe_imessage_checkpoint
    from topos.ingestion.local_sync_jobs import LOCAL_SYNC_RUNNERS

    assert LOCAL_SYNC_RUNNERS["imessage"].describe_checkpoint(conn, DS) == describe_imessage_checkpoint(conn, DS)


def test_imessage_pre_enqueue_refusal_is_the_enrolled_dataset_guard(conn: sqlite3.Connection) -> None:
    """A node with no enrollment refuses nothing; the guard is the same function the sync runs."""
    from topos.ingestion.local_sync_jobs import LOCAL_SYNC_RUNNERS

    assert LOCAL_SYNC_RUNNERS["imessage"].pre_enqueue_refusal(conn, DS, None) is None


# --- the former literals read the table ----------------------------------------


@pytest.mark.asyncio
async def test_the_job_executor_dispatches_through_the_table(conn: sqlite3.Connection, monkeypatch) -> None:
    from topos.ingestion import local_sync_jobs

    calls: list = []
    monkeypatch.setitem(local_sync_jobs.LOCAL_SYNC_RUNNERS, "invented_lane", _fake_runner("invented_lane", calls))
    monkeypatch.setattr("topos.storage.source_settings.update_sync_result", lambda *a, **k: None)

    result = await job_runner._execute_local_sync(
        {"source_id": "invented_lane", "dataset_id": DS, "sync_options": {"mode": "since_last", "batch_size": 120}}
    )

    assert result["status"] == "ok" and result["messages_processed"] == 2
    assert calls == [{"dataset_id": DS, "sync_options": {"mode": "since_last", "batch_size": 120}, "batch_size": 120}]


@pytest.mark.asyncio
async def test_the_job_executor_still_refuses_a_source_off_the_table() -> None:
    result = await job_runner._execute_local_sync({"source_id": "slack", "dataset_id": DS})
    assert result["status"] == "error" and "slack" in result["error"]


@pytest.mark.asyncio
async def test_the_job_executor_runs_the_runners_post_sync_and_survives_its_failure(conn, monkeypatch) -> None:
    from topos.ingestion import local_sync_jobs
    from topos.ingestion.local_sync_jobs import LocalSyncRunner

    seen: list = []

    def run(dataset_id, **kw):
        return {"status": "ok", "records_processed": 1}

    def post_sync(own, dataset_id, result, sync_options):
        seen.append(dataset_id)
        raise RuntimeError("post_sync exploded")

    monkeypatch.setitem(
        local_sync_jobs.LOCAL_SYNC_RUNNERS, "invented_lane",
        LocalSyncRunner(source_id="invented_lane", run=run, schedulable=False, post_sync=post_sync),
    )
    monkeypatch.setattr("topos.storage.source_settings.update_sync_result", lambda *a, **k: None)
    result = await job_runner._execute_local_sync({"source_id": "invented_lane", "dataset_id": DS})
    assert result["status"] == "ok" and seen == [DS]


def test_the_enqueue_door_reads_the_table(conn: sqlite3.Connection, monkeypatch) -> None:
    from topos.ingestion import local_sync_jobs
    from topos.ingestion.local_sync_jobs import enqueue_local_sync_blocking
    from topos.pipeline.job_store import get_job

    monkeypatch.setitem(local_sync_jobs.LOCAL_SYNC_RUNNERS, "invented_lane", _fake_runner("invented_lane", []))
    outcome = enqueue_local_sync_blocking(conn, source_id="invented_lane", dataset_id=DS, sync_options={"mode": "since_last"})
    assert outcome["status"] == "ok"
    assert get_job(conn, outcome["job_id"])["payload"]["source_id"] == "invented_lane"

    refused = enqueue_local_sync_blocking(conn, source_id="slack", dataset_id=DS, sync_options=None)
    assert refused["status"] == "error" and "slack" in refused["error"]


def test_the_enqueue_door_applies_the_runners_default_options_and_refusal(conn: sqlite3.Connection, monkeypatch) -> None:
    from topos.ingestion import local_sync_jobs
    from topos.ingestion.local_sync_jobs import LocalSyncRunner, enqueue_local_sync_blocking
    from topos.pipeline.job_store import get_job

    def refusal(db, dataset_id, sync_options):
        if dataset_id == "refused-ds":
            return {"status": "error", "code": "invented_refusal", "error": "no: invented reason"}
        return None

    monkeypatch.setitem(
        local_sync_jobs.LOCAL_SYNC_RUNNERS, "invented_lane",
        LocalSyncRunner(
            source_id="invented_lane", run=lambda *a, **k: {"status": "ok"}, schedulable=True,
            default_options=lambda opts: opts or {"mode": "6m"}, pre_enqueue_refusal=refusal,
        ),
    )
    refused = enqueue_local_sync_blocking(conn, source_id="invented_lane", dataset_id="refused-ds", sync_options=None)
    assert refused == {"status": "error", "error": "no: invented reason", "code": "invented_refusal"}
    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 0

    ok = enqueue_local_sync_blocking(conn, source_id="invented_lane", dataset_id=DS, sync_options=None)
    assert get_job(conn, ok["job_id"])["payload"]["sync_options"] == {"mode": "6m"}


def test_the_relay_guard_reads_the_table(conn: sqlite3.Connection, monkeypatch) -> None:
    from topos.core.handlers.sources import handle_source_sync
    from topos.ingestion import local_sync_jobs
    from topos.sources.definitions import DataSourceDefinition
    from topos.sources.registry import REGISTRY

    calls: list = []

    async def fake_enqueue(**kwargs):
        calls.append(kwargs)
        return {"status": "ok", "job_id": "job-synthetic", "already_running": False}

    monkeypatch.setattr(local_sync_jobs, "enqueue_local_sync", fake_enqueue)
    monkeypatch.setitem(local_sync_jobs.LOCAL_SYNC_RUNNERS, "invented_lane", _fake_runner("invented_lane", []))
    monkeypatch.setitem(
        REGISTRY, "invented_lane",
        DataSourceDefinition(source_id="invented_lane", display_name="Invented", source_type="local_sync",
                             schema_id="invented.v1", parser_id="invented.v1", delivery="local_sync"),
    )
    out = asyncio.run(handle_source_sync({"id": "r1", "payload": {"source_id": "invented_lane", "dataset_id": DS}}))
    assert out["status"] == "ok" and out["payload"]["job_id"] == "job-synthetic"
    assert calls and calls[0]["source_id"] == "invented_lane"

    monkeypatch.setitem(
        REGISTRY, "off_table",
        DataSourceDefinition(source_id="off_table", display_name="Off", source_type="local_sync",
                             schema_id="off.v1", parser_id="off.v1", delivery="local_sync"),
    )
    out = asyncio.run(handle_source_sync({"id": "r2", "payload": {"source_id": "off_table", "dataset_id": DS}}))
    assert out["status"] == "error" and "off_table" in out["error"]


def test_the_scheduler_eligibility_reads_the_table(conn: sqlite3.Connection, monkeypatch) -> None:
    from topos.ingestion import local_sync_jobs
    from topos.ingestion.local_sync_schedule import describe_schedule, put_schedule, schedulable_source_ids

    assert schedulable_source_ids() == ("imessage", "claude_code_sessions", "codex_sessions", "cursor_sessions")
    with pytest.raises(ValueError):
        put_schedule(conn, DS, "signal", {"enabled": True})
    assert describe_schedule(conn, DS, "signal")["supported"] is False

    monkeypatch.setitem(local_sync_jobs.LOCAL_SYNC_RUNNERS, "invented_lane", _fake_runner("invented_lane", []))
    described = put_schedule(conn, DS, "invented_lane", {"enabled": True, "interval_hours": 1})
    assert described["enabled"] is True and described["interval_hours"] == 1
    assert describe_schedule(conn, DS, "invented_lane")["supported"] is True


def test_the_scheduler_asks_the_runner_before_enabling(conn: sqlite3.Connection, monkeypatch) -> None:
    from topos.ingestion import local_sync_jobs
    from topos.ingestion.local_sync_jobs import LocalSyncRunner
    from topos.ingestion.local_sync_schedule import get_schedule, put_schedule

    monkeypatch.setitem(
        local_sync_jobs.LOCAL_SYNC_RUNNERS, "invented_lane",
        LocalSyncRunner(
            source_id="invented_lane", run=lambda *a, **k: {"status": "ok"}, schedulable=True,
            pre_enqueue_refusal=lambda db, ds, opts: {"status": "error", "code": "x", "error": "not now"},
        ),
    )
    with pytest.raises(ValueError, match="not now"):
        put_schedule(conn, DS, "invented_lane", {"enabled": True})
    assert get_schedule(conn, DS, "invented_lane") is None


def test_the_settings_screen_reads_the_runners_checkpoint(conn: sqlite3.Connection, monkeypatch) -> None:
    from topos.ingestion import local_sync_jobs
    from topos.ingestion.local_sync_jobs import LocalSyncRunner
    from topos.ingestion.local_sync_schedule import describe_sync_settings

    monkeypatch.setitem(
        local_sync_jobs.LOCAL_SYNC_RUNNERS, "invented_lane",
        LocalSyncRunner(
            source_id="invented_lane", run=lambda *a, **k: {"status": "ok"}, schedulable=True,
            describe_checkpoint=lambda db, ds: {"has_checkpoint": True, "where": ds},
        ),
    )
    described = describe_sync_settings(conn, DS, "invented_lane")
    assert described["sync_checkpoint"] == {"has_checkpoint": True, "where": DS}
    assert described["sync_schedule"]["supported"] is True
    assert describe_sync_settings(conn, DS, "signal") == {}


def test_the_scheduler_hands_over_to_the_runner_after_a_settled_run(conn: sqlite3.Connection, monkeypatch) -> None:
    from datetime import datetime, timezone

    from topos.ingestion import local_sync_jobs
    from topos.ingestion.local_sync_jobs import LocalSyncRunner
    from topos.ingestion.local_sync_schedule import _record_run, _settle_running, put_schedule, STATUS_RUNNING
    from topos.pipeline.job_store import complete_job, enqueue_job

    seen: list = []
    monkeypatch.setitem(
        local_sync_jobs.LOCAL_SYNC_RUNNERS, "invented_lane",
        LocalSyncRunner(
            source_id="invented_lane", run=lambda *a, **k: {"status": "ok"}, schedulable=True,
            after_settle=lambda ds, outcome: seen.append((ds, outcome)),
        ),
    )
    now = datetime(2026, 10, 15, 3, 5, tzinfo=timezone.utc)
    put_schedule(conn, DS, "invented_lane", {"enabled": True}, now=now)
    enqueue_job(conn, kind=job_runner.LOCAL_SYNC_KIND, payload={"source_id": "invented_lane", "dataset_id": DS},
                job_id="job-settle", source_id="invented_lane", idempotency_key="local_sync:job-settle")
    complete_job(conn, "job-settle", detail={"status": "ok", "outcome": "imported", "records_processed": 1})
    from topos.ingestion.local_sync_schedule import get_schedule

    _record_run(conn, get_schedule(conn, DS, "invented_lane"), status=STATUS_RUNNING, now=now, job_id="job-settle",
                result={})
    _settle_running(conn, get_schedule(conn, DS, "invented_lane"), now)
    assert seen == [(DS, "imported")]
