"""Tests for durable pipeline job queue."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from topos.pipeline.job_runner import process_pending_jobs_once, recover_pipeline_jobs
from topos.pipeline.job_store import (
    claim_matching_queued_jobs,
    claim_next_job,
    enqueue_job,
    get_job,
    is_derivation_complete,
    recover_stale_jobs,
)
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    # check_same_thread=False: process_job now runs its job-store bookkeeping
    # on worker threads (via the conn factory) so the event loop never blocks
    # on the write gate; the factory below hands every thread this handle.
    db = sqlite3.connect(str(tmp_path / "pipeline.db"), check_same_thread=False)
    apply_pipeline_jobs_v1_up(db)
    yield db
    # No close: the cancelled-worker test can leave a claim running on an
    # executor thread; closing the shared handle under it segfaults CPython's
    # sqlite3. The tmp-path db is reaped by pytest.


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


def _enqueue_inbox_job(conn: sqlite3.Connection, *, idx: int, source_id: str) -> str:
    return enqueue_job(
        conn,
        kind="inbox_deferred_enrichment",
        payload={
            "source_id": source_id,
            "sync_batch_id": f"batch-{idx}",
            "canonical_records": [{"record_id": f"r{idx}", "source_id": source_id}],
            "write_id": f"w{idx}",
        },
        job_id=f"inbox-{idx}",
        source_id=source_id,
        write_id=f"w{idx}",
        sync_batch_id=f"batch-{idx}",
        idempotency_key=f"inbox_derivation:w{idx}",
    )


def test_claim_matching_queued_jobs_scopes_by_source(conn: sqlite3.Connection) -> None:
    for idx in range(3):
        _enqueue_inbox_job(conn, idx=idx, source_id="browser_visits")
    _enqueue_inbox_job(conn, idx=9, source_id="imessage")

    claimed = claim_matching_queued_jobs(
        conn,
        lease_owner="worker-a",
        kind="inbox_deferred_enrichment",
        source_id="browser_visits",
    )
    assert {j["job_id"] for j in claimed} == {"inbox-0", "inbox-1", "inbox-2"}
    assert all(j["status"] == "running" for j in claimed)
    assert get_job(conn, "inbox-9")["status"] == "queued"


@pytest.mark.asyncio
async def test_inbox_backlog_coalesces_into_one_batch(conn: sqlite3.Connection) -> None:
    calls: list[dict] = []

    async def _exec(payload: dict) -> dict:
        calls.append(payload)
        return {
            "status": "ok",
            "messages_processed": len(payload.get("canonical_records") or []),
            "records_created": {},
        }

    for idx in range(3):
        _enqueue_inbox_job(conn, idx=idx, source_id="browser_visits")
    _enqueue_inbox_job(conn, idx=9, source_id="imessage")

    with patch.dict(
        "topos.pipeline.job_runner.EXECUTORS",
        {"inbox_deferred_enrichment": _exec},
    ):
        processed = await process_pending_jobs_once(lambda: conn, limit=10)

    # Two claims total: the browser backlog coalesced into one executor call,
    # the imessage job ran on its own.
    assert processed == 2
    assert len(calls) == 2
    by_size = sorted(calls, key=lambda p: len(p["canonical_records"]), reverse=True)
    assert sorted(r["record_id"] for r in by_size[0]["canonical_records"]) == ["r0", "r1", "r2"]
    assert [r["record_id"] for r in by_size[1]["canonical_records"]] == ["r9"]

    for job_id in ("inbox-0", "inbox-1", "inbox-2", "inbox-9"):
        assert get_job(conn, job_id)["status"] == "done"
    # Every coalesced delivery gets its derivation-completion receipt.
    for write_id in ("w0", "w1", "w2", "w9"):
        assert is_derivation_complete(conn, write_id)


@pytest.mark.asyncio
async def test_coalesced_jobs_all_fail_together(conn: sqlite3.Connection) -> None:
    async def _exec(_payload: dict) -> dict:
        return {"status": "error", "error": "boom"}

    for idx in range(2):
        _enqueue_inbox_job(conn, idx=idx, source_id="browser_visits")

    with patch.dict(
        "topos.pipeline.job_runner.EXECUTORS",
        {"inbox_deferred_enrichment": _exec},
    ):
        processed = await process_pending_jobs_once(lambda: conn, limit=10)

    assert processed == 1
    for job_id in ("inbox-0", "inbox-1"):
        assert get_job(conn, job_id)["status"] == "failed"
    assert not is_derivation_complete(conn, "w0")
    assert not is_derivation_complete(conn, "w1")


@pytest.mark.asyncio
async def test_worker_loop_survives_locked_claim(conn: sqlite3.Connection) -> None:
    """Regression for 2026-08-07: an OperationalError('database is locked')
    escaping claim_next_job's bounded busy-retries killed the worker task with
    an unretrieved exception and the queue silently stopped draining."""
    import asyncio

    from topos.pipeline import job_runner
    from topos.pipeline.job_runner import _worker_loop

    real_claim = job_runner.claim_next_job
    failures = {"left": 2}

    def flaky_claim(own, **kwargs):
        if failures["left"] > 0:
            failures["left"] -= 1
            raise sqlite3.OperationalError("database is locked")
        return real_claim(own, **kwargs)

    done: list[dict] = []

    async def _exec(payload: dict) -> dict:
        done.append(payload)
        return {"status": "ok", "messages_processed": 1, "records_created": {}}

    enqueue_job(conn, kind="file_ingestion", payload={"marker": "survive"}, job_id="job-lock-1")

    with patch("topos.pipeline.job_runner.claim_next_job", side_effect=flaky_claim), patch.dict(
        "topos.pipeline.job_runner.EXECUTORS", {"file_ingestion": _exec}
    ):
        task = asyncio.get_running_loop().create_task(_worker_loop(lambda: conn))
        try:
            deadline = asyncio.get_running_loop().time() + 10
            while not done and asyncio.get_running_loop().time() < deadline:
                assert not task.done(), f"worker loop died: {task.exception()}"
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    assert done, "worker never recovered from the locked claim"
    assert failures["left"] == 0
    assert get_job(conn, "job-lock-1")["status"] == "done"


@pytest.mark.asyncio
async def test_job_processing_never_takes_write_gate_on_event_loop(
    conn: sqlite3.Connection, caplog
) -> None:
    """The write gate is a blocking OS lock; job bookkeeping must acquire it on
    worker threads only, or the loop freezes behind long holders (2026-08-07)."""
    import asyncio
    import logging

    from topos.storage.db import write_gate

    async def _exec(_payload: dict) -> dict:
        return {"status": "ok", "messages_processed": 1, "records_created": {}}

    # Enqueue off-loop: enqueue_job itself takes the gate and is not the
    # code under test here.
    await asyncio.to_thread(
        enqueue_job, conn, kind="file_ingestion", payload={}, job_id="job-gate-1"
    )

    write_gate.reset_loop_warning_state()
    caplog.set_level(logging.WARNING, logger="topos.storage.db.write_gate")
    with patch.dict("topos.pipeline.job_runner.EXECUTORS", {"file_ingestion": _exec}):
        processed = await process_pending_jobs_once(lambda: conn, limit=5)

    assert processed == 1
    loop_warnings = [
        rec.message for rec in caplog.records if "event-loop thread" in rec.getMessage()
    ]
    assert not loop_warnings, f"write gate acquired on the event loop: {loop_warnings}"


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
