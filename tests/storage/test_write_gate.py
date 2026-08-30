"""Tests for process-wide SQLite write gate and busy retry."""

from __future__ import annotations

import logging
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
from topos.storage.db import write_gate
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

WRITE_GATE_LOGGER = "topos.storage.db.write_gate"


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


def test_pipeline_jobs_v1_first_apply_takes_the_write_gate(file_conn: sqlite3.Connection) -> None:
    """The skip is only for a recorded migration — a fresh database must still write."""
    holds: list[str] = []
    original = write_gate.with_db_write

    def _count_holds():
        holds.append("enter")
        return original()

    with patch("topos.storage.db.write_gate.with_db_write", _count_holds):
        apply_pipeline_jobs_v1_up(file_conn)

    assert holds
    tables = {
        row[0]
        for row in file_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "pipeline_jobs" in tables


def test_pipeline_jobs_v1_skips_write_gate_when_already_applied(file_conn: sqlite3.Connection) -> None:
    """Re-ensure must not take the write gate — that path ran on the event loop
    and stalled /healthcheck (2026-08-30 tray flicker)."""
    apply_pipeline_jobs_v1_up(file_conn)
    holds: list[str] = []

    original = write_gate.with_db_write

    def _count_holds():
        holds.append("enter")
        return original()

    with patch("topos.storage.db.write_gate.with_db_write", _count_holds):
        apply_pipeline_jobs_v1_up(file_conn)

    assert holds == []
    row = file_conn.execute(
        "SELECT 1 FROM wiki_schema_migrations WHERE migration_id='pipeline_jobs_v1'"
    ).fetchone()
    assert row is not None


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


def test_caller_site_names_the_calling_frame() -> None:
    # Every write_gate diagnostic is only as useful as this string, and it now
    # runs on the watchdog's hot path too, so pin the format and the frame.
    def opener() -> str:
        return write_gate._caller_site()

    site = opener()
    assert site.startswith("test_write_gate.py:")
    assert site.endswith(" in opener")


def test_caller_site_skips_contextlib_and_write_gate_frames(
    file_conn: sqlite3.Connection, caplog, monkeypatch
) -> None:
    # with_db_write is a @contextmanager living in write_gate, so its own
    # generator frame AND contextlib's __exit__ sit between the warning and the
    # caller. Naming either would make the warning unactionable.
    monkeypatch.setattr(write_gate, "_SLOW_HOLD_WARN_S", 0.0)
    write_gate.reset_loop_warning_state()
    with caplog.at_level(logging.WARNING, logger=WRITE_GATE_LOGGER):
        with with_db_write():
            pass
    # caplog.text carries the emitting module's own filename, so read the
    # rendered message rather than the record prefix. Naming this test function
    # is itself the proof that both intervening frames were skipped.
    message = caplog.records[-1].getMessage()
    assert "slow section at test_write_gate.py:" in message
    assert " in test_caller_site_skips_contextlib_and_write_gate_frames" in message
    assert "contextlib.py" not in message


def test_ungated_commit_warning_names_the_caller(
    file_conn: sqlite3.Connection, caplog
) -> None:
    # The commit-time half of the pair: reached from inside commit_connection,
    # so the site must still resolve past write_gate's own frames.
    write_gate.reset_loop_warning_state()
    file_conn.execute("INSERT INTO t (v) VALUES ('ungated')")
    with caplog.at_level(logging.WARNING, logger=WRITE_GATE_LOGGER):
        commit_connection(file_conn)
    assert "arrived with an open write transaction" in caplog.text
    assert " in test_ungated_commit_warning_names_the_caller" in caplog.text


# --- open-transaction watchdog ---------------------------------------------


@pytest.fixture()
def watched_conn(file_conn: sqlite3.Connection):
    """``file_conn`` under the watchdog, with its background thread parked.

    The real 5s threshold is kept and time is injected into the scan instead,
    so these tests assert the shipped behaviour without sleeping. The scan
    interval is pushed out of the way so the daemon thread cannot consume a
    rate-limit slot mid-test.
    """
    threshold = write_gate._watchdog_threshold_s
    interval = write_gate._watchdog_interval_s
    write_gate.reset_loop_warning_state()
    write_gate.enable_txn_watchdog(interval_s=3600.0)
    write_gate.register_connection(file_conn)
    try:
        yield file_conn
    finally:
        write_gate.unregister_connection(file_conn)
        write_gate.disable_txn_watchdog()
        write_gate.reset_loop_warning_state()
        write_gate._watchdog_threshold_s = threshold
        write_gate._watchdog_interval_s = interval


def _scan_in(seconds: float) -> int:
    """Run a scan as if ``seconds`` had passed since now."""
    return write_gate._scan_open_transactions(now=time.monotonic() + seconds)


def test_watchdog_reports_never_committed_ungated_write(watched_conn, caplog) -> None:
    # The blind spot _warn_ungated_transaction cannot see: this write took
    # SQLite's RESERVED lock and no commit will ever arrive to announce it.
    watched_conn.execute("INSERT INTO t (v) VALUES ('stuck')")
    assert watched_conn.in_transaction

    # Young transactions are ordinary; only overstaying is a symptom.
    assert _scan_in(0.0) == 0

    with caplog.at_level(logging.WARNING, logger=WRITE_GATE_LOGGER):
        assert _scan_in(30.0) == 1
    assert "has stayed open" in caplog.text
    assert "test_write_gate.py" in caplog.text

    # A rollback (like a commit) is what clears the record.
    watched_conn.rollback()
    write_gate.reset_loop_warning_state()
    assert _scan_in(30.0) == 0


def test_watchdog_names_the_function_that_opened_the_transaction(
    watched_conn, caplog
) -> None:
    # The StatisticsJob._should_promote shape: a helper writes, returns, and
    # the caller moves on. Naming the helper is the whole point — the thread
    # is long gone from this frame by the time the watchdog notices.
    def _should_promote() -> bool:
        watched_conn.execute("UPDATE t SET v='x' WHERE id=-1")
        return False

    _should_promote()
    assert watched_conn.in_transaction

    with caplog.at_level(logging.WARNING, logger=WRITE_GATE_LOGGER):
        assert _scan_in(30.0) == 1
    assert "_should_promote" in caplog.text
    watched_conn.rollback()


def test_watchdog_ignores_gated_and_committed_write(watched_conn, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=WRITE_GATE_LOGGER):
        with with_db_write():
            watched_conn.execute("INSERT INTO t (v) VALUES ('ok')")
            commit_connection(watched_conn)
        assert not watched_conn.in_transaction
        assert _scan_in(3600.0) == 0
    assert "has stayed open" not in caplog.text


def test_watchdog_ignores_a_section_still_holding_the_gate(watched_conn) -> None:
    # The criterion is the gate, not the commit: a gated writer's RESERVED
    # lock is the serialization the gate exists to provide, however long the
    # section runs (that case belongs to the slow-section warning).
    with with_db_write():
        watched_conn.execute("INSERT INTO t (v) VALUES ('slow')")
        assert watched_conn.in_transaction
        assert _scan_in(3600.0) == 0
        commit_connection(watched_conn)


def test_watchdog_ignores_long_read_only_section(watched_conn) -> None:
    for _ in range(5):
        watched_conn.execute("SELECT COUNT(*) FROM t").fetchone()
    assert not watched_conn.in_transaction
    assert _scan_in(3600.0) == 0


def test_watchdog_rate_limits_repeat_reports(watched_conn) -> None:
    # A stuck transaction is scanned every couple of seconds; warning on each
    # pass would reproduce the log flood _LOOP_WARN_INTERVAL_S exists to stop.
    watched_conn.execute("INSERT INTO t (v) VALUES ('noisy')")
    assert _scan_in(30.0) == 1
    assert _scan_in(31.0) == 0
    assert _scan_in(32.0) == 0
    watched_conn.rollback()


def test_watchdog_reports_then_forgets_a_transaction_whose_thread_exited(
    watched_conn, caplog
) -> None:
    def orphan() -> None:
        watched_conn.execute("INSERT INTO t (v) VALUES ('orphan')")

    thread = threading.Thread(target=orphan, name="orphan-writer")
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert watched_conn.in_transaction

    with caplog.at_level(logging.WARNING, logger=WRITE_GATE_LOGGER):
        assert _scan_in(30.0) == 1
    assert "which has since exited" in caplog.text

    # No COMMIT trace can ever clear it, so the record is dropped rather than
    # re-reported at every scan for the life of the process.
    write_gate.reset_loop_warning_state()
    assert _scan_in(31.0) == 0
    watched_conn.rollback()


def test_registration_is_a_no_op_while_disabled(file_conn: sqlite3.Connection) -> None:
    # Production default: no trace hook, no registry entry, nothing to scan,
    # and unregister never reaches into the sqlite3 object.
    write_gate.disable_txn_watchdog()
    assert not write_gate.txn_watchdog_enabled()
    write_gate.register_connection(file_conn)
    assert id(file_conn) not in write_gate._traced_conns
    file_conn.execute("INSERT INTO t (v) VALUES ('unwatched')")
    assert file_conn.in_transaction
    assert id(file_conn) not in write_gate._open_txns
    assert _scan_in(3600.0) == 0
    file_conn.rollback()


def test_enabling_starts_a_daemon_thread_on_first_registration(
    file_conn: sqlite3.Connection,
) -> None:
    threshold = write_gate._watchdog_threshold_s
    interval = write_gate._watchdog_interval_s
    write_gate.disable_txn_watchdog()
    try:
        write_gate.enable_txn_watchdog(interval_s=3600.0)
        assert write_gate._watchdog_thread is None, "thread must not start at enable"
        write_gate.register_connection(file_conn)
        thread = write_gate._watchdog_thread
        assert thread is not None and thread.is_alive()
        assert thread.daemon, "the watchdog must never hold up interpreter shutdown"
    finally:
        write_gate.unregister_connection(file_conn)
        write_gate.disable_txn_watchdog()
        write_gate._watchdog_threshold_s = threshold
        write_gate._watchdog_interval_s = interval
    assert not thread.is_alive(), "disable must stop the thread"
