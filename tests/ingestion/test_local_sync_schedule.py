"""The node's own iMessage sync schedule: when it fires, when it skips, what it records.

Every run it starts goes through the same enqueue as the "Sync now" button, as a
since-last sync in small paced batches. Synthetic node databases only; the job
worker is never started here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from topos.ingestion import local_sync_schedule as schedule_mod
from topos.ingestion.local_sync_schedule import (
    SCHEDULED_BATCH_SIZE,
    SCHEDULED_PAUSE_SECONDS,
    TABLE,
    describe_schedule,
    get_schedule,
    latest_slot,
    next_slot,
    put_schedule,
    run_schedule_tick,
    scheduled_sync_options,
    slot_times,
    start_scheduler,
)
from topos.storage.db import write_gate
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up

DS = "owner:topos:enrolled"
CHICAGO = "America/Chicago"


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "node.db"), check_same_thread=False)
    apply_pipeline_jobs_v1_up(db)
    yield db
    db.close()


class _Enqueue:
    """Stands in for enqueue_local_sync_blocking and records every call."""

    def __init__(self, *outcomes: dict) -> None:
        self.calls: list[dict] = []
        self.outcomes = list(outcomes)

    def __call__(self, conn, *, source_id, dataset_id, sync_options):
        self.calls.append({"source_id": source_id, "dataset_id": dataset_id, "sync_options": sync_options})
        if self.outcomes:
            return self.outcomes.pop(0)
        return {"status": "ok", "job_id": f"job-{len(self.calls)}", "already_running": False}


def _enable(conn, *, now: str, daily_time: str = "03:00", interval_hours=None, tz: str = CHICAGO) -> dict:
    return put_schedule(
        conn, DS, "imessage",
        {"enabled": True, "daily_time": daily_time, "interval_hours": interval_hours, "timezone": tz},
        now=_utc(now),
    )


# --- slots ------------------------------------------------------------------


def test_slot_times_are_the_daily_time_then_every_n_hours_from_it() -> None:
    assert slot_times("03:00", None) == [(3, 0)]
    assert slot_times("03:00", 6) == [(3, 0), (9, 0), (15, 0), (21, 0)]
    assert slot_times("22:30", 5) == [(3, 30), (8, 30), (13, 30), (18, 30), (22, 30)]
    assert len(slot_times("00:00", 1)) == 24


def test_latest_and_next_slot_follow_the_owners_wall_clock() -> None:
    daily = {"daily_time": "03:00", "interval_hours": None, "timezone": CHICAGO}
    # 04:00 CDT on 29 Sep: today's 03:00 has passed.
    assert latest_slot(daily, _utc("2026-09-29T09:00:00")) == _utc("2026-09-29T08:00:00")
    assert next_slot(daily, _utc("2026-09-29T09:00:00")) == _utc("2026-09-30T08:00:00")
    # 02:00 CDT: the latest is yesterday's.
    assert latest_slot(daily, _utc("2026-09-29T07:00:00")) == _utc("2026-09-28T08:00:00")


def test_the_daily_time_stays_put_across_a_clock_change() -> None:
    daily = {"daily_time": "03:00", "interval_hours": None, "timezone": CHICAGO}
    # US clocks go back on 1 Nov 2026: 03:00 is 08:00Z before and 09:00Z after.
    assert next_slot(daily, _utc("2026-10-31T09:00:00")) == _utc("2026-11-01T09:00:00")


# --- saving the setting --------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"daily_time": "25:00"},
        {"daily_time": "3am"},
        {"interval_hours": 0},
        {"interval_hours": 25},
        {"interval_hours": "often"},
        {"interval_hours": True},
        {"timezone": "Mars/Olympus_Mons"},
        {"when": "daily"},
    ],
)
def test_a_bad_setting_is_refused(conn: sqlite3.Connection, changes: dict) -> None:
    with pytest.raises(ValueError):
        put_schedule(conn, DS, "imessage", {"enabled": True, **changes}, now=_utc("2026-09-29T15:00:00"))
    assert get_schedule(conn, DS, "imessage") is None


def test_only_imessage_is_scheduled(conn: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        put_schedule(conn, DS, "signal", {"enabled": True}, now=_utc("2026-09-29T15:00:00"))


def test_turning_it_on_waits_for_the_next_slot(conn: sqlite3.Connection) -> None:
    described = _enable(conn, now="2026-09-29T15:00:00")
    assert described["enabled"] is True
    assert described["next_run_at"] == "2026-09-30T08:00:00+00:00"

    enqueue = _Enqueue()
    run_schedule_tick(conn, now=_utc("2026-09-29T15:01:00"), enqueue=enqueue)
    assert enqueue.calls == [], "the 03:00 that already passed today is not run on enabling"

    run_schedule_tick(conn, now=_utc("2026-09-30T08:00:30"), enqueue=enqueue)
    assert len(enqueue.calls) == 1


def test_the_setting_lives_in_its_own_table_not_a_watched_one(conn: sqlite3.Connection) -> None:
    """The enrollment's source clock watches engine_config, user_ingestion_sources,
    source_settings and source_runtime_installs. The schedule writes none of them."""
    from topos.permissions_v2.ingest_provenance import _SOURCE_TABLES

    _enable(conn, now="2026-09-29T15:00:00")
    assert TABLE not in _SOURCE_TABLES
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not tables & set(_SOURCE_TABLES)


# --- firing -------------------------------------------------------------------


def test_a_due_slot_enqueues_one_paced_since_last_sync(conn: sqlite3.Connection) -> None:
    _enable(conn, now="2026-09-29T15:00:00")
    enqueue = _Enqueue()

    summary = run_schedule_tick(conn, now=_utc("2026-09-30T08:00:30"), enqueue=enqueue)

    assert summary["enqueued"] == 1
    assert enqueue.calls == [{
        "source_id": "imessage",
        "dataset_id": DS,
        "sync_options": {
            "mode": "since_last",
            "trigger": "schedule",
            "batch_size": SCHEDULED_BATCH_SIZE,
            "pause_seconds": SCHEDULED_PAUSE_SECONDS,
        },
    }]
    stored = get_schedule(conn, DS, "imessage")
    assert stored["last_status"] == "running"
    assert stored["last_job_id"] == "job-1"
    assert stored["last_slot_at"] == "2026-09-30T08:00:00+00:00"


def test_a_slot_fires_once(conn: sqlite3.Connection) -> None:
    _enable(conn, now="2026-09-29T15:00:00")
    enqueue = _Enqueue()
    for minute in range(0, 50, 10):
        run_schedule_tick(conn, now=_utc(f"2026-09-30T08:{minute:02d}:30"), enqueue=enqueue)
    assert len(enqueue.calls) == 1


def test_a_node_that_was_off_runs_the_latest_missed_slot_once(conn: sqlite3.Connection) -> None:
    _enable(conn, now="2026-09-26T15:00:00", interval_hours=6)
    enqueue = _Enqueue()

    run_schedule_tick(conn, now=_utc("2026-09-29T16:00:00"), enqueue=enqueue)
    run_schedule_tick(conn, now=_utc("2026-09-29T16:01:00"), enqueue=enqueue)

    assert len(enqueue.calls) == 1, "three days of missed slots are one run, not twelve"
    # 15:00 CDT (every 6 h from 03:00) is 20:00Z, so the one run covered 09:00 CDT (14:00Z).
    assert get_schedule(conn, DS, "imessage")["last_slot_at"] == "2026-09-29T14:00:00+00:00"


def test_a_disabled_schedule_never_fires(conn: sqlite3.Connection) -> None:
    _enable(conn, now="2026-09-29T15:00:00")
    put_schedule(conn, DS, "imessage", {"enabled": False}, now=_utc("2026-09-29T16:00:00"))
    enqueue = _Enqueue()
    run_schedule_tick(conn, now=_utc("2026-10-05T08:00:30"), enqueue=enqueue)
    assert enqueue.calls == []


def test_a_node_nobody_scheduled_reads_one_pragma_and_creates_nothing(tmp_path: Path) -> None:
    db = sqlite3.connect(str(tmp_path / "bare.db"))
    assert run_schedule_tick(db, now=_utc("2026-09-30T08:00:30"), enqueue=_Enqueue())["enqueued"] == 0
    assert db.execute("SELECT name FROM sqlite_master").fetchall() == []


# --- skipping and failing -----------------------------------------------------


def test_a_slot_while_another_sync_runs_is_skipped(conn: sqlite3.Connection) -> None:
    _enable(conn, now="2026-09-29T15:00:00")
    enqueue = _Enqueue({"status": "ok", "job_id": "manual-job", "already_running": True})

    summary = run_schedule_tick(conn, now=_utc("2026-09-30T08:00:30"), enqueue=enqueue)

    assert summary["skipped"] == 1
    stored = get_schedule(conn, DS, "imessage")
    assert stored["last_status"] == "skipped"
    assert stored["last_job_id"] == "manual-job"
    assert "already running" in stored["last_result"]["reason"]


def test_a_slot_while_its_own_last_run_is_still_going_keeps_tracking_it(conn: sqlite3.Connection) -> None:
    _enable(conn, now="2026-09-29T14:30:00", interval_hours=1)
    enqueue = _Enqueue()
    run_schedule_tick(conn, now=_utc("2026-09-29T15:00:30"), enqueue=enqueue)  # 10:00 CDT slot
    enqueue.outcomes.append({"status": "ok", "job_id": "job-1", "already_running": True})
    # job-1 has no pipeline row, so give it one that is still running.
    conn.execute(
        "INSERT INTO pipeline_jobs (job_id, kind, status, payload_json, created_at, updated_at) "
        "VALUES ('job-1', 'local_sync', 'running', '{}', datetime('now'), datetime('now'))"
    )
    conn.commit()

    run_schedule_tick(conn, now=_utc("2026-09-29T16:00:30"), enqueue=enqueue)

    stored = get_schedule(conn, DS, "imessage")
    assert stored["last_status"] == "running"
    assert stored["last_job_id"] == "job-1"


def test_a_refused_start_is_recorded_with_its_reason(conn: sqlite3.Connection) -> None:
    _enable(conn, now="2026-09-29T15:00:00")
    enqueue = _Enqueue({"status": "error", "error": "iMessage on this node is enrolled for a different dataset."})

    run_schedule_tick(conn, now=_utc("2026-09-30T08:00:30"), enqueue=enqueue)

    stored = get_schedule(conn, DS, "imessage")
    assert stored["last_status"] == "failed"
    assert "enrolled" in stored["last_error"]


# --- how a run ended ---------------------------------------------------------------


def _finished_job(conn, job_id: str, status: str, detail: dict) -> None:
    conn.execute(
        "INSERT INTO pipeline_jobs (job_id, kind, status, payload_json, detail_json, created_at, updated_at) "
        "VALUES (?, 'local_sync', ?, '{}', ?, datetime('now'), datetime('now'))",
        (job_id, status, json.dumps(detail)),
    )
    conn.commit()


def test_a_run_waiting_for_the_owner_is_recorded_with_its_plan(conn: sqlite3.Connection) -> None:
    _enable(conn, now="2026-09-29T15:00:00")
    run_schedule_tick(conn, now=_utc("2026-09-30T08:00:30"), enqueue=_Enqueue())
    _finished_job(conn, "job-1", "done", {"status": "ok", "sync": {
        "outcome": "needs_confirmation",
        "records_processed": 0,
        "plan": {"reason": "no_checkpoint", "trusted": False, "start_rowid": 0, "to_import": 1234,
                 "messages": 1300, "first_at": "2024-01-02T00:00:00+00:00", "last_at": "2026-09-30T07:00:00+00:00"},
    }})

    # What the screen shows before the loop records it: looked up, not written.
    assert describe_schedule(conn, DS, "imessage", now=_utc("2026-09-30T08:01:00"))["last_status"] == "needs_confirmation"
    assert get_schedule(conn, DS, "imessage")["last_status"] == "running"

    summary = run_schedule_tick(conn, now=_utc("2026-09-30T08:01:30"), enqueue=_Enqueue())

    assert summary["settled"] == 1
    stored = get_schedule(conn, DS, "imessage")
    assert stored["last_status"] == "needs_confirmation"
    assert stored["last_result"]["plan"]["to_import"] == 1234
    assert stored["last_result"]["plan"]["reason"] == "no_checkpoint"


def test_a_run_that_imported_records_its_counts(conn: sqlite3.Connection) -> None:
    _enable(conn, now="2026-09-29T15:00:00")
    run_schedule_tick(conn, now=_utc("2026-09-30T08:00:30"), enqueue=_Enqueue())
    _finished_job(conn, "job-1", "done", {"status": "ok", "sync": {
        "outcome": "imported", "records_processed": 42, "records_skipped": 3, "records_held": 1,
    }})

    run_schedule_tick(conn, now=_utc("2026-09-30T08:05:00"), enqueue=_Enqueue())

    stored = get_schedule(conn, DS, "imessage")
    assert stored["last_status"] == "imported"
    assert (stored["last_result"]["records_processed"], stored["last_result"]["records_skipped"]) == (42, 3)
    assert stored["last_run_at"] == "2026-09-30T08:00:30+00:00", "settling keeps when the run started"


def test_a_failed_run_is_recorded_with_its_error(conn: sqlite3.Connection) -> None:
    _enable(conn, now="2026-09-29T15:00:00")
    run_schedule_tick(conn, now=_utc("2026-09-30T08:00:30"), enqueue=_Enqueue())
    _finished_job(conn, "job-1", "failed", {"error": "Cannot copy chat.db: Full Disk Access may be required."})

    run_schedule_tick(conn, now=_utc("2026-09-30T08:05:00"), enqueue=_Enqueue())

    stored = get_schedule(conn, DS, "imessage")
    assert stored["last_status"] == "failed"
    assert "Full Disk Access" in stored["last_error"]


# --- the real enqueue --------------------------------------------------------------


def test_the_tick_goes_through_the_buttons_enqueue(conn: sqlite3.Connection) -> None:
    _enable(conn, now="2026-09-29T14:30:00", interval_hours=1)

    run_schedule_tick(conn, now=_utc("2026-09-29T15:00:30"))
    run_schedule_tick(conn, now=_utc("2026-09-29T16:00:30"))

    jobs = conn.execute("SELECT job_id, status, payload_json FROM pipeline_jobs WHERE kind='local_sync'").fetchall()
    assert len(jobs) == 1, "the second slot found the first run still queued and did not start a rival"
    payload = json.loads(jobs[0][2])
    assert payload["dataset_id"] == DS
    assert payload["sync_options"] == scheduled_sync_options()
    assert get_schedule(conn, DS, "imessage")["last_status"] == "running"


# --- the loop ------------------------------------------------------------------------


@pytest.fixture
def loop_gate_warnings():
    """write_gate's own logger (engine logging does not propagate to caplog)."""
    logger = logging.getLogger("topos.storage.db.write_gate")
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture(level=logging.WARNING)
    write_gate.reset_loop_warning_state()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def _loop_acquisitions(records) -> list[str]:
    return [r.getMessage() for r in records if "acquired on the event-loop thread" in r.getMessage()]


@pytest.mark.asyncio
async def test_the_loop_never_takes_the_write_gate_on_the_event_loop(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, loop_gate_warnings
) -> None:
    started: list = []
    monkeypatch.setattr("topos.pipeline.job_runner.start_pipeline_worker", lambda factory: started.append(factory))
    # Due now: enabled a day ago, every hour. Set up before measuring: this
    # write runs on the loop, which is the test's doing, not the scheduler's.
    _enable(conn, now=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), interval_hours=1)
    write_gate.reset_loop_warning_state()
    loop_gate_warnings.clear()
    factory = lambda: conn  # noqa: E731

    task = asyncio.create_task(schedule_mod.run_scheduler_loop(factory, tick_seconds=3600, startup_delay_seconds=0))
    try:
        for _ in range(200):
            if conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0]:
                break
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0] == 1
    assert started == [factory], "the job worker is started for the queued run"
    assert _loop_acquisitions(loop_gate_warnings) == []


@pytest.mark.asyncio
async def test_the_gate_guard_above_is_live(conn: sqlite3.Connection, loop_gate_warnings) -> None:
    """The same tick run ON the loop is caught, so the empty list above means something."""
    _enable(conn, now=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), interval_hours=1)
    write_gate.reset_loop_warning_state()
    loop_gate_warnings.clear()
    run_schedule_tick(conn)
    assert _loop_acquisitions(loop_gate_warnings)


def test_the_scheduler_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list = []
    monkeypatch.setenv("TOPOS_LOCAL_SYNC_SCHEDULER", "off")
    assert start_scheduler(lambda coro, name: spawned.append(name), lambda: None) is False
    assert spawned == []
    assert describe_schedule(sqlite3.connect(":memory:"), DS, "imessage")["scheduler_running"] is False


def test_the_scheduler_starts_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list = []
    monkeypatch.delenv("TOPOS_LOCAL_SYNC_SCHEDULER", raising=False)

    def _spawn(coro, name):
        spawned.append(name)
        coro.close()

    assert start_scheduler(_spawn, lambda: None) is True
    assert spawned == ["local-sync-scheduler"]


# --- the default schedule: an enabled iMessage source syncs hourly without the owner ---------------
#
# The owner's live node (21 Oct 2026): iMessage enabled since August, six syncs in all, every one a
# button press, the last on 8 Oct. The schedule was opt-in and nothing ever opted in: the AI-coding
# tools get an hourly row when they are enabled, iMessage got none. So the scheduler, alive and
# enqueuing the coding tools every hour, had nothing of iMessage's to run.


def _migrated_node(tmp_path: Path, *rows: tuple[str, int]) -> sqlite3.Connection:
    """A node with the job table and an iMessage source row per (dataset_id, enabled) given."""
    from topos.storage.source_settings import ensure_table as ensure_source_settings_table

    db = sqlite3.connect(str(tmp_path / "node.db"), check_same_thread=False)
    apply_pipeline_jobs_v1_up(db)
    ensure_source_settings_table(db)
    for dataset_id, enabled in rows:
        db.execute(
            "INSERT INTO user_ingestion_sources (dataset_id, source_id, enabled, last_sync_at) "
            "VALUES (?, 'imessage', ?, '2026-10-08T15:23:00+00:00')",
            (dataset_id, enabled),
        )
    db.commit()
    return db


def _enroll(conn: sqlite3.Connection, dataset_id: str) -> None:
    """An active owner-attested iMessage enrollment for one dataset (the sync guard's own schema)."""
    from topos.permissions_v2.ingest_protocol import IMESSAGE_READER_CONTRACT
    from topos.permissions_v2.ingest_provenance import IngestProvenanceService

    schema = IngestProvenanceService._schema(None, conn, 2)
    for name in ("ingest_provenance_state", "ingest_provenance_enrollments"):
        conn.execute(schema[name])
    conn.execute("INSERT INTO ingest_provenance_state VALUES (1, 'store', '{}', 'rev', 0)")
    conn.execute(
        "INSERT INTO ingest_provenance_enrollments VALUES (?, ?, ?, 1, 'active', 0, 'attested', 0, 'uds')",
        ("enr-1", json.dumps({"reader_contract": IMESSAGE_READER_CONTRACT}), dataset_id),
    )
    conn.commit()


def test_an_enabled_imessage_source_is_scheduled_hourly_without_the_owner(tmp_path: Path) -> None:
    conn = _migrated_node(tmp_path, (DS, 1))
    enqueue = _Enqueue()
    assert get_schedule(conn, DS, "imessage") is None

    # The first tick after start creates the row; the slot just passed counts as done, so nothing
    # runs at once (the same rule as the owner turning it on).
    run_schedule_tick(conn, now=_utc("2026-10-21T15:00:30"), enqueue=enqueue)
    stored = get_schedule(conn, DS, "imessage")
    assert stored is not None and stored["enabled"] is True
    assert stored["interval_hours"] == 1
    assert enqueue.calls == []

    # The next hour's slot runs a paced since-last sync through the button's door.
    summary = run_schedule_tick(conn, now=_utc("2026-10-21T16:00:30"), enqueue=enqueue)
    assert summary["enqueued"] == 1
    assert [c["source_id"] for c in enqueue.calls] == ["imessage"]
    assert enqueue.calls[0]["dataset_id"] == DS
    assert enqueue.calls[0]["sync_options"] == scheduled_sync_options()

    # Once per slot, as ever.
    run_schedule_tick(conn, now=_utc("2026-10-21T16:30:00"), enqueue=enqueue)
    assert len(enqueue.calls) == 1


def test_only_the_enrolled_imessage_dataset_gets_the_default_schedule(tmp_path: Path) -> None:
    """Two enabled iMessage rows, one enrollment: the sync would refuse the other dataset, so no row for it."""
    other = "owner:topos:older"
    conn = _migrated_node(tmp_path, (DS, 1), (other, 1))
    _enroll(conn, DS)
    enqueue = _Enqueue()
    run_schedule_tick(conn, now=_utc("2026-10-21T15:00:30"), enqueue=enqueue)
    assert get_schedule(conn, DS, "imessage") is not None
    assert get_schedule(conn, other, "imessage") is None
    run_schedule_tick(conn, now=_utc("2026-10-21T16:00:30"), enqueue=enqueue)
    assert [c["dataset_id"] for c in enqueue.calls] == [DS]


def test_a_disabled_imessage_source_gets_no_default_schedule(tmp_path: Path) -> None:
    conn = _migrated_node(tmp_path, (DS, 0))
    run_schedule_tick(conn, now=_utc("2026-10-21T15:00:30"), enqueue=_Enqueue())
    assert get_schedule(conn, DS, "imessage") is None


def test_a_schedule_the_owner_switched_off_stays_off(tmp_path: Path) -> None:
    """The default fills an absence; a row the owner wrote, on or off, is theirs."""
    conn = _migrated_node(tmp_path, (DS, 1))
    put_schedule(conn, DS, "imessage", {"enabled": False, "daily_time": "04:30"}, now=_utc("2026-10-20T10:00:00"))
    enqueue = _Enqueue()
    run_schedule_tick(conn, now=_utc("2026-10-21T15:00:30"), enqueue=enqueue)
    run_schedule_tick(conn, now=_utc("2026-10-21T16:00:30"), enqueue=enqueue)
    stored = get_schedule(conn, DS, "imessage")
    assert stored["enabled"] is False and stored["daily_time"] == "04:30" and stored["interval_hours"] is None
    assert enqueue.calls == []


def test_the_default_schedule_is_imessages_alone(tmp_path: Path) -> None:
    """The coding tools write their own hourly row when enabled; the default never reaches past iMessage."""
    from topos.ingestion.local_sync_jobs import LOCAL_SYNC_RUNNERS

    assert [sid for sid, r in LOCAL_SYNC_RUNNERS.items() if r.default_schedule] == ["imessage"]
    conn = _migrated_node(tmp_path)
    conn.execute(
        "INSERT INTO user_ingestion_sources (dataset_id, source_id, enabled) VALUES (?, 'codex_sessions', 1)", (DS,)
    )
    conn.commit()
    run_schedule_tick(conn, now=_utc("2026-10-21T15:00:30"), enqueue=_Enqueue())
    assert get_schedule(conn, DS, "codex_sessions") is None
