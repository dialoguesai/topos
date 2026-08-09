"""Start ingestion durable job tests."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest

from topos.core.handlers import handle_control_plane_request
from topos.pipeline import job_runner
from topos.pipeline.job_runner import process_pending_jobs_once, recover_pipeline_jobs
from topos.pipeline.job_store import get_job
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up


def _stub_file_ingestion(monkeypatch: pytest.MonkeyPatch, result: dict) -> list[dict]:
    """Route file_ingestion dispatch to a stub, and record what it was called with.

    EXECUTORS binds the executor functions at import time, so patching the
    module attribute ``_execute_file_ingestion`` leaves dispatch pointing at the
    original — the stub never runs and the real ingestion reads whatever
    ``file_path`` says. Patch the dict entry that dispatch actually reads.
    """
    calls: list[dict] = []

    async def _fake_ingest(payload: dict) -> dict:
        calls.append(payload)
        return result

    monkeypatch.setitem(job_runner.EXECUTORS, "file_ingestion", _fake_ingest)
    return calls


@pytest.fixture
def conn(tmp_path, monkeypatch):
    db = sqlite3.connect(str(tmp_path / "ingest.db"), check_same_thread=False)
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
    # No close: background job bookkeeping runs on executor threads that can
    # outlive the test's event loop; closing the shared handle under a live
    # thread segfaults CPython's sqlite3. The tmp-path db is reaped by pytest.


@pytest.mark.asyncio
async def test_start_ingestion_persists_job_before_ack(conn, monkeypatch, tmp_path) -> None:
    _stub_file_ingestion(monkeypatch, {"status": "ok", "records_processed": 3, "records_total": 3})
    sample = tmp_path / "sample.jsonl"
    sample.write_text("", encoding="utf-8")

    result = await handle_control_plane_request(
        {
            "id": "req-start",
            "type": "start_ingestion",
            "payload": {
                "job_id": "cp-job-1",
                "dataset_id": "user:dataset",
                "schema_id": "chatgpt.conversation.v1",
                "file_path": str(sample),
            },
        }
    )
    assert result["status"] == "ok"
    job = get_job(conn, "cp-job-1")
    assert job is not None
    assert job["kind"] == "file_ingestion"
    assert job["status"] in {"queued", "running", "done"}


@pytest.mark.asyncio
async def test_start_ingestion_survives_simulated_restart(conn, monkeypatch, tmp_path) -> None:
    calls = _stub_file_ingestion(
        monkeypatch, {"status": "ok", "records_processed": 1, "records_total": 1}
    )
    monkeypatch.setattr(
        "topos.pipeline.job_runner.start_pipeline_worker",
        lambda _factory: None,
    )
    sample = tmp_path / "sample.jsonl"
    sample.write_text("", encoding="utf-8")

    await handle_control_plane_request(
        {
            "id": "req-start-2",
            "type": "start_ingestion",
            "payload": {
                "job_id": "cp-job-2",
                "dataset_id": "user:dataset",
                "schema_id": "chatgpt.conversation.v1",
                "file_path": str(sample),
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
    # The recovered job has to reach the executor. Without this the test passes
    # whenever real ingestion happens to succeed, which is how a dead stub went
    # unnoticed here.
    assert len(calls) == 1
