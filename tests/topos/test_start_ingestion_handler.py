"""Start ingestion durable job tests."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from topos.core.handlers import handle_control_plane_request
from topos.pipeline.job_runner import process_pending_jobs_once, recover_pipeline_jobs
from topos.pipeline.job_store import get_job
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db = sqlite3.connect(str(tmp_path / "ingest.db"))
    apply_pipeline_jobs_v1_up(db)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: db)
    import topos.core.handlers as handlers_module
    import topos.core.handlers.ingest as ingest_module

    monkeypatch.setattr(handlers_module, "get_db_connection", lambda: db)
    monkeypatch.setattr(ingest_module.hub, "get_db_connection", lambda: db)

    async def _kick(factory):
        await process_pending_jobs_once(factory, limit=5)

    monkeypatch.setattr(
        "topos.pipeline.job_runner.start_pipeline_worker",
        lambda factory: asyncio.create_task(_kick(factory)),
    )
    yield db
    db.close()


@pytest.mark.asyncio
async def test_start_ingestion_persists_job_before_ack(conn, monkeypatch) -> None:
    async def _fake_ingest(**kwargs):
        return {"status": "ok", "records_processed": 3, "records_total": 3}

    monkeypatch.setattr("topos.pipeline.job_runner._execute_file_ingestion", _fake_ingest)

    result = await handle_control_plane_request(
        {
            "id": "req-start",
            "type": "start_ingestion",
            "payload": {
                "job_id": "cp-job-1",
                "dataset_id": "user:dataset",
                "schema_id": "chatgpt.conversation.v1",
                "file_path": "/tmp/sample.jsonl",
            },
        }
    )
    assert result["status"] == "ok"
    job = get_job(conn, "cp-job-1")
    assert job is not None
    assert job["kind"] == "file_ingestion"
    assert job["status"] in {"queued", "running", "done"}


@pytest.mark.asyncio
async def test_start_ingestion_survives_simulated_restart(conn, monkeypatch) -> None:
    async def _fake_ingest(**kwargs):
        return {"status": "ok", "records_processed": 1, "records_total": 1}

    monkeypatch.setattr("topos.pipeline.job_runner._execute_file_ingestion", _fake_ingest)
    monkeypatch.setattr(
        "topos.pipeline.job_runner.start_pipeline_worker",
        lambda _factory: None,
    )

    await handle_control_plane_request(
        {
            "id": "req-start-2",
            "type": "start_ingestion",
            "payload": {
                "job_id": "cp-job-2",
                "dataset_id": "user:dataset",
                "schema_id": "chatgpt.conversation.v1",
                "file_path": "/tmp/sample.jsonl",
            },
        }
    )

    conn.execute(
        "UPDATE pipeline_jobs SET status='running', lease_expires_at='2000-01-01T00:00:00+00:00' WHERE job_id='cp-job-2'"
    )
    conn.commit()
    recover_pipeline_jobs(conn)
    await process_pending_jobs_once(lambda: conn, limit=5)

    job = get_job(conn, "cp-job-2")
    assert job is not None
    assert job["status"] == "done"
