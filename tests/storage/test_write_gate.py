"""Tests for process-wide SQLite write gate and busy retry."""

from __future__ import annotations

import sqlite3
import threading
import time
from unittest.mock import patch

import pytest

from topos.pipeline.job_runner import process_job
from topos.pipeline.job_store import (
    enqueue_job,
    fail_job,
    get_job,
    is_derivation_complete,
    record_derivation_completion,
)
from topos.storage.db.connection_tuning import tune_connection
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up
from topos.storage.db.write_gate import (
    WriteGateDeferred,
    begin_immediate,
    commit_connection,
    db_write_lock,
    sqlite_retry_busy,
    with_db_write,
    with_db_write_cooperative,
)


@pytest.fixture()
def file_conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "write_gate.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    tune_connection(conn)
    conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
    conn.commit()
    yield conn
    conn.close()


def test_write_gate_serializes_concurrent_writers(file_conn: sqlite3.Connection) -> None:
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def writer(tag: str) -> None:
        try:
            barrier.wait(timeout=5)
            with with_db_write():
                file_conn.execute("INSERT INTO t (v) VALUES (?)", (tag,))
                # Hold the gate briefly so the other thread must wait.
                time.sleep(0.05)
                commit_connection(file_conn)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=writer, args=("a",)),
        threading.Thread(target=writer, args=("b",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors
    count = int(file_conn.execute("SELECT COUNT(*) FROM t").fetchone()[0])
    assert count == 2


def test_begin_immediate_recovers_leaked_implicit_transaction(
    file_conn: sqlite3.Connection,
) -> None:
    # A 0-row UPDATE opens an implicit transaction; a writer that returns
    # without committing used to make every later BEGIN IMMEDIATE fail with
    # "cannot start a transaction within a transaction".
    file_conn.execute("UPDATE t SET v='x' WHERE id=-1")
    assert file_conn.in_transaction
    begin_immediate(file_conn)
    assert file_conn.in_transaction
    file_conn.execute("ROLLBACK")
    assert not file_conn.in_transaction


def test_begin_immediate_plain_path(file_conn: sqlite3.Connection) -> None:
    begin_immediate(file_conn)
    file_conn.execute("INSERT INTO t (v) VALUES ('y')")
    file_conn.commit()
    assert not file_conn.in_transaction
    assert int(file_conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]) == 1


def test_sqlite_retry_busy_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    assert sqlite_retry_busy(flaky, attempts=5) == "ok"
    assert calls["n"] == 3


def test_sqlite_retry_busy_gives_up() -> None:
    def always_busy() -> None:
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="locked"):
        sqlite_retry_busy(always_busy, attempts=2)


@pytest.mark.asyncio
async def test_process_job_fails_without_derivation_completion(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "pipeline.db"), check_same_thread=False)
    apply_pipeline_jobs_v1_up(conn)
    job_id = enqueue_job(
        conn,
        kind="inbox_deferred_enrichment",
        payload={"source_id": "grow_journal", "write_id": "w1"},
        job_id="job-fail-1",
        write_id="w1",
        source_id="grow_journal",
        idempotency_key="inbox_derivation:w1",
    )
    # Move to running like the worker would.
    conn.execute(
        "UPDATE pipeline_jobs SET status='running' WHERE job_id=?",
        (job_id,),
    )
    conn.commit()
    job = get_job(conn, job_id)
    assert job is not None

    async def fake_exec(_payload):
        return {
            "status": "error",
            "error": "database is locked",
            "errors": [{"job": "facts", "error": "database is locked"}],
        }

    with patch.dict(
        "topos.pipeline.job_runner.EXECUTORS",
        {"inbox_deferred_enrichment": fake_exec},
    ):
        await process_job(lambda: conn, job)

    updated = get_job(conn, job_id)
    assert updated is not None
    assert updated["status"] == "failed"
    assert not is_derivation_complete(conn, "w1")


def test_cooperative_gate_defers_when_priority_writer_is_active() -> None:
    # Derivation already running: never even try for the gate.
    with pytest.raises(WriteGateDeferred):
        with with_db_write_cooperative(lambda: True, slice_s=0.05):
            pytest.fail("section must not run")


def test_cooperative_gate_defers_when_priority_writer_starts_mid_wait() -> None:
    # The 2026-08-07 interleaving: the rebuild is waiting for the gate when a
    # derivation batch starts. It must step aside, not queue behind/ahead.
    holder_has_gate = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with db_write_lock():
            holder_has_gate.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert holder_has_gate.wait(timeout=5)
        checks = iter([False, False, True])
        with pytest.raises(WriteGateDeferred):
            with with_db_write_cooperative(lambda: next(checks, True), slice_s=0.05):
                pytest.fail("section must not run")
    finally:
        release.set()
        thread.join(timeout=5)


def test_cooperative_gate_rechecks_after_acquiring_and_releases() -> None:
    # Gate is free, but the priority writer appears between the poll and the
    # acquisition: the section must not run, and the gate must be released.
    checks = iter([False, True])
    with pytest.raises(WriteGateDeferred):
        with with_db_write_cooperative(lambda: next(checks, True), slice_s=0.05):
            pytest.fail("section must not run")

    # The gate is reentrant, so probe from a DIFFERENT thread to prove the
    # deferred acquisition released it.
    acquired = {"ok": False}

    def probe() -> None:
        if db_write_lock().acquire(timeout=1):
            acquired["ok"] = True
            db_write_lock().release()

    thread = threading.Thread(target=probe)
    thread.start()
    thread.join(timeout=5)
    assert acquired["ok"], "deferred acquisition leaked the gate"


def test_cooperative_gate_runs_when_uncontended() -> None:
    ran = []
    with with_db_write_cooperative(lambda: False, slice_s=0.05):
        ran.append(1)
    assert ran == [1]


@pytest.mark.asyncio
async def test_event_loop_stays_responsive_while_gate_is_held(tmp_path) -> None:
    """Regression for the 2026-08-07 freeze: a thread holding the write gate
    must stall job bookkeeping (which now runs on worker threads), never the
    event loop itself."""
    import asyncio

    from topos.pipeline.job_runner import process_job

    conn = sqlite3.connect(str(tmp_path / "pipeline.db"), check_same_thread=False)
    apply_pipeline_jobs_v1_up(conn)
    job_id = enqueue_job(conn, kind="file_ingestion", payload={}, job_id="job-resp-1")
    conn.execute("UPDATE pipeline_jobs SET status='running' WHERE job_id=?", (job_id,))
    conn.commit()
    job = get_job(conn, job_id)
    assert job is not None

    holder_has_gate = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with db_write_lock():
            holder_has_gate.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=holder)
    thread.start()
    assert holder_has_gate.wait(timeout=5)

    ticks = {"n": 0}

    async def heartbeat() -> None:
        while True:
            ticks["n"] += 1
            await asyncio.sleep(0.02)

    async def fake_exec(_payload):
        return {"status": "ok", "messages_processed": 0, "records_created": {}}

    hb = asyncio.get_running_loop().create_task(heartbeat())
    try:
        with patch.dict("topos.pipeline.job_runner.EXECUTORS", {"file_ingestion": fake_exec}):
            worker = asyncio.get_running_loop().create_task(process_job(lambda: conn, job))
            # Bookkeeping is blocked on the gate the holder thread owns; the
            # loop must keep scheduling coroutines the whole time.
            await asyncio.sleep(0.6)
            assert not worker.done(), "bookkeeping should still be waiting on the gate"
            assert ticks["n"] >= 10, "event loop stalled while another thread held the write gate"
            release.set()
            await asyncio.wait_for(worker, timeout=10)
    finally:
        hb.cancel()
        release.set()
        thread.join(timeout=5)

    assert get_job(conn, job_id)["status"] == "done"
    conn.close()


def test_enqueue_requeues_failed_job(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "pipeline.db"))
    apply_pipeline_jobs_v1_up(conn)
    job_id = enqueue_job(
        conn,
        kind="inbox_deferred_enrichment",
        payload={"source_id": "grow_journal", "write_id": "w2"},
        job_id="job-rq-1",
        write_id="w2",
        idempotency_key="inbox_derivation:w2",
    )
    fail_job(conn, job_id, error="database is locked")
    assert get_job(conn, job_id)["status"] == "failed"

    again = enqueue_job(
        conn,
        kind="inbox_deferred_enrichment",
        payload={"source_id": "grow_journal", "write_id": "w2", "recover": True},
        write_id="w2",
        idempotency_key="inbox_derivation:w2",
    )
    assert again == job_id
    assert get_job(conn, job_id)["status"] == "queued"
    # Successful completion path still records derivation.
    record_derivation_completion(conn, write_id="w2", job_id=job_id)
    assert is_derivation_complete(conn, "w2")
