"""Tests for durable pipeline job queue."""

from __future__ import annotations

import sqlite3

import pytest

from topos.pipeline.job_runner import process_pending_jobs_once, recover_pipeline_jobs
from topos.pipeline.job_store import claim_next_job, enqueue_job, get_job, recover_stale_jobs
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "pipeline.db"))
    apply_pipeline_jobs_v1_up(db)
    yield db
    db.close()


def test_enqueue_is_idempotent_by_key(conn: sqlite3.Connection) -> None:
    first = enqueue_job(
        conn,
        kind="file_ingestion",
        payload={"job_id": "j1"},
        job_id="j1",
        idempotency_key="file_ingestion:j1",
    )
    second = enqueue_job(
        conn,
        kind="file_ingestion",
        payload={"job_id": "j1"},
        job_id="j1",
        idempotency_key="file_ingestion:j1",
    )
    assert first == second
    rows = conn.execute("SELECT COUNT(*) FROM pipeline_jobs").fetchone()[0]
    assert rows == 1


def test_interrupted_running_job_is_retried(conn: sqlite3.Connection) -> None:
    enqueue_job(conn, kind="file_ingestion", payload={"job_id": "j2"}, job_id="j2")
    claimed = claim_next_job(conn, lease_owner="worker-a")
    assert claimed is not None
    assert claimed["status"] == "running"

    conn.execute(
        "UPDATE pipeline_jobs SET lease_expires_at='2000-01-01T00:00:00+00:00' WHERE job_id='j2'"
    )
    conn.commit()

    assert recover_stale_jobs(conn) == 1
    job = get_job(conn, "j2")
    assert job is not None
    assert job["status"] == "queued"


@pytest.mark.asyncio
async def test_process_pending_jobs_once(conn: sqlite3.Connection) -> None:
    seen: list[str] = []

    async def _exec(payload: dict) -> dict:
        seen.append(str(payload.get("marker")))
        return {"status": "ok", "messages_processed": 1, "records_created": {}}

    import topos.pipeline.job_runner as runner

    runner.EXECUTORS["file_ingestion"] = _exec
    enqueue_job(
        conn,
        kind="file_ingestion",
        payload={"marker": "done"},
        job_id="job-process",
    )
    processed = await process_pending_jobs_once(lambda: conn, limit=5)
    assert processed == 1
    assert seen == ["done"]
    job = get_job(conn, "job-process")
    assert job is not None
    assert job["status"] == "done"
