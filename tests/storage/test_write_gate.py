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
    begin_immediate,
    commit_connection,
    sqlite_retry_busy,
    with_db_write,
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
    conn = sqlite3.connect(str(tmp_path / "pipeline.db"))
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
        await process_job(conn, job)

    updated = get_job(conn, job_id)
    assert updated is not None
    assert updated["status"] == "failed"
    assert not is_derivation_complete(conn, "w1")


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
