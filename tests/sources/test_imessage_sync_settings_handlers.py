"""The settings doors carry where the next since-last sync starts and the schedule.

Reading them is open like the rest of the settings; changing the schedule is the
owner's alone, as ``source_sync`` is. A finished sync's outcome and plan reach
the poller through the job's progress. Synthetic node databases only.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

import topos.core.handlers as hub
from topos.core.handlers.enrichment import _progress_dict
from topos.core.handlers.sources import handle_get_source_settings, handle_put_source_settings
from topos.ingestion.checkpoints.checkpoint_store import IngestionCheckpoint
from topos.ingestion.checkpoints.sqlite_checkpoint_store import SqliteCheckpointStore
from topos.ingestion.local_sync import IMESSAGE_SCHEMA_ID
from topos.ingestion.local_sync_schedule import get_schedule
from topos.permissions_v2.ingest_protocol import IMESSAGE_READER_CONTRACT
from topos.principal import OWNER_APP, THIRD_PARTY, Principal, reset_principal, set_principal
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up

DS = "owner:topos:enrolled"


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    # Shared across threads on purpose: the handlers read and write off the loop.
    db = sqlite3.connect(str(tmp_path / "node.db"), check_same_thread=False)
    apply_pipeline_jobs_v1_up(db)
    monkeypatch.setattr(hub, "get_db_connection", lambda: db)
    yield db
    db.close()


def _as(cls: str | None):
    return set_principal(Principal(cls=cls, channel="cp_relay") if cls else None)


def _put(payload: dict, *, principal: str | None = OWNER_APP) -> dict:
    token = _as(principal)
    try:
        return asyncio.run(handle_put_source_settings({"id": "r", "payload": {"dataset_id": DS, **payload}}))
    finally:
        reset_principal(token)


def _get(source_id: str = "imessage") -> dict:
    return asyncio.run(handle_get_source_settings({"id": "g", "payload": {"source_id": source_id, "dataset_id": DS}}))


def test_settings_say_where_the_next_since_last_sync_starts(conn: sqlite3.Connection) -> None:
    SqliteCheckpointStore(conn).save_checkpoint(
        IngestionCheckpoint(dataset_id=DS, schema_id=IMESSAGE_SCHEMA_ID, last_record_id="imessage:97",
                            metadata={"unbounded_last_record_id": "imessage:40"})
    )

    payload = _get()["payload"]

    assert "since_last" in payload["sync_modes"] and "full_history" in payload["sync_modes"]
    assert payload["sync_checkpoint"]["high_water_rowid"] == 97
    assert payload["sync_checkpoint"]["unbounded_rowid"] == 40
    assert payload["sync_schedule"]["enabled"] is False
    assert payload["sync_schedule"]["next_run_at"] is None
    # The existing fields are untouched.
    assert payload["enabled"] is True and payload["exclude_spam"] is True


def test_signal_settings_carry_no_schedule(conn: sqlite3.Connection) -> None:
    payload = _get("signal")["payload"]
    assert "sync_schedule" not in payload and "sync_checkpoint" not in payload


@pytest.mark.parametrize("principal", [None, THIRD_PARTY, "cp_relay"])
def test_a_schedule_change_is_the_owners_alone(conn: sqlite3.Connection, principal) -> None:
    result = _put({"source_id": "imessage", "sync_schedule": {"enabled": True}}, principal=principal)

    assert result["status"] == "error"
    assert result["error"] == "owner_mode_required"
    assert get_schedule(conn, DS, "imessage") is None


def test_the_other_settings_stay_open(conn: sqlite3.Connection) -> None:
    """Only the schedule field is owner-gated; the enable switch behaves as before."""
    result = _put({"source_id": "imessage", "enabled": True}, principal=THIRD_PARTY)
    assert result["status"] == "ok"


def test_the_owner_saves_a_schedule_and_reads_it_back(conn: sqlite3.Connection) -> None:
    result = _put({
        "source_id": "imessage",
        "sync_schedule": {"enabled": True, "daily_time": "03:00", "interval_hours": 6, "timezone": "America/Chicago"},
    })

    assert result["status"] == "ok", result
    saved = result["payload"]["sync_schedule"]
    assert (saved["enabled"], saved["daily_time"], saved["interval_hours"]) == (True, "03:00", 6)
    assert saved["next_run_at"]
    assert _get()["payload"]["sync_schedule"] == saved


def test_a_bad_schedule_is_refused_with_its_reason(conn: sqlite3.Connection) -> None:
    result = _put({"source_id": "imessage", "sync_schedule": {"enabled": True, "daily_time": "25:00"}})
    assert result["status"] == "error"
    assert "HH:MM" in result["error"]


def test_a_schedule_is_refused_for_a_dataset_that_is_not_enrolled(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE ingest_provenance_enrollments (enrollment_id TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL, "
        "dataset_id TEXT NOT NULL UNIQUE, revision INTEGER NOT NULL, state TEXT NOT NULL, source_generation INTEGER "
        "NOT NULL, attestation TEXT NOT NULL, authorized_at INTEGER NOT NULL, channel TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO ingest_provenance_enrollments VALUES ('e', ?, 'owner:elsewhere', 1, 'active', 0, 'a', 0, 'uds')",
        (json.dumps({"reader_contract": IMESSAGE_READER_CONTRACT}),),
    )
    conn.commit()

    result = _put({"source_id": "imessage", "sync_schedule": {"enabled": True}})

    assert result["status"] == "error"
    assert "enrolled" in result["error"]
    assert get_schedule(conn, DS, "imessage") is None


def test_a_schedule_is_only_for_local_sync_sources(conn: sqlite3.Connection) -> None:
    result = _put({"source_id": "browser_visits", "sync_schedule": {"enabled": True}})
    assert result["status"] == "error"


# --- the outcome reaches whoever polls the job --------------------------------------


def test_the_progress_projection_carries_a_syncs_outcome_and_plan() -> None:
    sync = {"outcome": "needs_confirmation", "plan": {"to_import": 12, "start_rowid": 0}}
    projected = _progress_dict({"job_id": "j", "status": "done", "progress": {"status": "completed", "sync": sync}})
    assert projected["status"] == "completed"
    assert projected["sync"] == sync
    # Every other kind's payload is unchanged.
    assert "sync" not in _progress_dict({"job_id": "k", "status": "done", "progress": {"status": "completed"}})


@pytest.mark.asyncio
async def test_a_finished_sync_job_writes_its_outcome_into_progress(conn: sqlite3.Connection, monkeypatch) -> None:
    from topos.pipeline import job_runner
    from topos.pipeline.job_store import enqueue_job, get_job

    sync = {"outcome": "imported", "records_processed": 4, "trigger": "schedule"}

    async def _fake(payload):
        return {"status": "ok", "records_processed": 4, "messages_processed": 4, "sync": sync}

    monkeypatch.setitem(job_runner.EXECUTORS, job_runner.LOCAL_SYNC_KIND, _fake)
    enqueue_job(conn, kind=job_runner.LOCAL_SYNC_KIND, payload={"source_id": "imessage", "dataset_id": DS},
                job_id="job-x", source_id="imessage", idempotency_key="local_sync:job-x")

    await job_runner.process_job(lambda: conn, get_job(conn, "job-x"))

    job = get_job(conn, "job-x")
    assert job["status"] == "done"
    assert job["progress"]["sync"] == sync
    assert _progress_dict(job)["sync"]["outcome"] == "imported"
