"""Tests for durable pipeline job queue."""

from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest

from topos.enrichment import pipeline_activity
from topos.enrichment.derivation_recovery import (
    SIGNAL_DERIVE_RETRY_KIND,
    list_pending_derivation_retries,
    record_failed_derivation,
    retry_pending_derivations,
)
from topos.pipeline.job_runner import EXECUTORS, process_pending_jobs_once, recover_pipeline_jobs
from topos.pipeline.job_store import (
    claim_matching_queued_jobs,
    claim_next_job,
    enqueue_job,
    get_job,
    is_derivation_complete,
    recover_stale_jobs,
    requeue_job,
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


# --- signal_derive_retry: the worker half of derivation-debt recovery -------
#
# Regression for 2026-08-07: debt records were enqueued with a kind no executor
# handled, so the worker claimed each one and instantly failed it with
# "Unknown job kind: signal_derive_retry".


def _record_debt(conn: sqlite3.Connection) -> str:
    job_id = record_failed_derivation(
        conn,
        source_id="github_activity",
        sync_batch_id="batch-1",
        job_name="timeline",
        error="database is locked",
        record_ids=["r1", "r2"],
        record_count=2,
    )
    assert job_id
    return job_id


def test_signal_derive_retry_has_an_executor() -> None:
    assert SIGNAL_DERIVE_RETRY_KIND in EXECUTORS


@pytest.mark.asyncio
async def test_worker_reruns_recorded_derivation_debt(conn: sqlite3.Connection) -> None:
    job_id = _record_debt(conn)
    calls: list[dict] = []

    async def _fake_retry(_conn, **kwargs) -> dict:
        calls.append(kwargs)
        return {"outcome": "recovered", "records": 2, "created": 2}

    with (
        patch("topos.enrichment.derivation_recovery.retry_single_derivation", _fake_retry),
        patch("topos.core.state.get_db_connection", lambda: conn),
    ):
        processed = await process_pending_jobs_once(lambda: conn, limit=5)

    assert processed == 1
    assert calls == [
        {
            "source_id": "github_activity",
            "sync_batch_id": "batch-1",
            "job_name": "timeline",
            "record_ids": ["r1", "r2"],
        }
    ]
    assert get_job(conn, job_id)["status"] == "done"
    assert list_pending_derivation_retries(conn) == []


@pytest.mark.asyncio
async def test_failed_retry_parks_debt_visible_with_real_error(
    conn: sqlite3.Connection,
) -> None:
    job_id = _record_debt(conn)

    async def _fake_retry(_conn, **_kwargs) -> dict:
        return {"outcome": "still_failing", "error": "database is locked"}

    with (
        patch("topos.enrichment.derivation_recovery.retry_single_derivation", _fake_retry),
        patch("topos.core.state.get_db_connection", lambda: conn),
    ):
        await process_pending_jobs_once(lambda: conn, limit=5)

    job = get_job(conn, job_id)
    assert job["status"] == "failed"
    assert job["detail"]["error"] == "database is locked"
    # A failed retry is still an outstanding debt.
    pending = list_pending_derivation_retries(conn)
    assert [p["job_name"] for p in pending] == ["timeline"]


@pytest.mark.asyncio
async def test_unretryable_debt_is_not_silently_discharged(
    conn: sqlite3.Connection,
) -> None:
    job_id = _record_debt(conn)

    async def _fake_retry(_conn, **_kwargs) -> dict:
        return {"outcome": "skipped", "reason": "unknown source"}

    with (
        patch("topos.enrichment.derivation_recovery.retry_single_derivation", _fake_retry),
        patch("topos.core.state.get_db_connection", lambda: conn),
    ):
        await process_pending_jobs_once(lambda: conn, limit=5)

    job = get_job(conn, job_id)
    assert job["status"] == "failed"
    assert job["detail"]["error"] == "cannot retry: unknown source"
    assert list_pending_derivation_retries(conn) != []


@pytest.mark.asyncio
async def test_retry_defers_while_derivation_batch_in_flight(
    conn: sqlite3.Connection,
) -> None:
    job_id = _record_debt(conn)

    with (
        patch("topos.enrichment.derivation_recovery._IN_FLIGHT_WAIT_SECONDS", 0.0),
        pipeline_activity.derivation_in_flight(),
    ):
        processed = await process_pending_jobs_once(lambda: conn, limit=1)

    # Claimed once, then handed back: a deferral is not an attempt.
    assert processed == 1
    job = get_job(conn, job_id)
    assert job["status"] == "queued"
    assert job["lease_owner"] is None
    assert [p["status"] for p in list_pending_derivation_retries(conn)] == ["queued"]


@pytest.mark.asyncio
async def test_retry_pending_derivations_maps_shared_runner_outcomes(
    conn: sqlite3.Connection,
) -> None:
    _record_debt(conn)

    async def _fake_retry(_conn, **_kwargs) -> dict:
        return {"outcome": "recovered", "records": 2, "created": 2}

    with patch("topos.enrichment.derivation_recovery.retry_single_derivation", _fake_retry):
        out = await retry_pending_derivations(conn)

    assert out["attempted"] == 1
    assert out["recovered"] == [
        {
            "job_name": "timeline",
            "source_id": "github_activity",
            "records": 2,
            "created": 2,
            "batch": "batch-1",
        }
    ]
    assert out["still_failing"] == []


@pytest.mark.asyncio
async def test_unknown_kind_parks_queued_instead_of_failing(
    conn: sqlite3.Connection,
) -> None:
    enqueue_job(conn, kind="mystery_kind", payload={}, job_id="jm1")
    processed = await process_pending_jobs_once(lambda: conn, limit=5)
    assert processed == 0
    assert get_job(conn, "jm1")["status"] == "queued"


def test_requeue_job_returns_claim_to_queue(conn: sqlite3.Connection) -> None:
    enqueue_job(conn, kind="file_ingestion", payload={}, job_id="jr1")
    claimed = claim_next_job(conn, lease_owner="worker-a")
    assert claimed is not None and claimed["status"] == "running"
    requeue_job(conn, "jr1")
    job = get_job(conn, "jr1")
    assert job["status"] == "queued"
    assert job["lease_owner"] is None
    assert job["lease_expires_at"] is None


def test_requeue_job_does_not_resurrect_settled_rows(conn: sqlite3.Connection) -> None:
    enqueue_job(conn, kind="file_ingestion", payload={}, job_id="jr2")
    claim_next_job(conn, lease_owner="worker-a")
    # clear_derivation_retry can settle a claimed debt out-of-band.
    conn.execute("UPDATE pipeline_jobs SET status='done' WHERE job_id='jr2'")
    conn.commit()
    requeue_job(conn, "jr2")
    assert get_job(conn, "jr2")["status"] == "done"
