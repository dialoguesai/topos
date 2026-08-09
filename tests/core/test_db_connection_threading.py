"""Per-thread SQLite connections, and durable derivation-failure records.

Regression cover for the 2026-07-30 incident: an 88-record github_activity
ingest lost its `facts` output and reported the batch clean.

    ERROR job facts failed: cannot commit - no transaction is active
    DEBUG complete source_id=github_activity jobs_run=8 deferred=[]

Root cause was one process-wide sqlite connection shared across the graph
refresher's `threading.Timer` thread and the `asyncio.to_thread` job workers. A
Connection has ONE transaction state, so concurrent users corrupt it.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.p1


# --- the race itself ---------------------------------------------------------


def test_one_shared_connection_corrupts_transactions(tmp_path: Path):
    """Demonstrates WHY per-thread connections are required.

    Two threads on ONE connection produce exactly the errors from the incident.
    If this ever stops raising, sqlite has changed and the fix can be revisited.
    """
    db = tmp_path / "shared.db"
    conn = sqlite3.connect(str(db), check_same_thread=False)
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.commit()

    errors: list[str] = []
    began = threading.Event()

    def writer_a() -> None:
        try:
            conn.execute("BEGIN")
            began.set()
            # Hold the transaction open while B tries to use the same handle.
            threading.Event().wait(0.25)
            conn.execute("INSERT INTO t VALUES (1)")
            conn.commit()
        except sqlite3.Error as exc:
            errors.append(f"A:{exc}")

    def writer_b() -> None:
        began.wait(2)
        try:
            conn.execute("BEGIN")
            conn.execute("INSERT INTO t VALUES (2)")
            conn.commit()
        except sqlite3.Error as exc:
            errors.append(f"B:{exc}")

    ta, tb = threading.Thread(target=writer_a), threading.Thread(target=writer_b)
    ta.start(), tb.start()
    ta.join(5), tb.join(5)
    conn.close()

    assert errors, "expected a transaction-state collision on a shared connection"
    assert any("transaction" in e for e in errors), errors


def test_separate_connections_do_not_collide(tmp_path: Path):
    """The same workload on per-thread connections completes cleanly."""
    db = tmp_path / "perthread.db"
    setup = sqlite3.connect(str(db))
    setup.execute("PRAGMA journal_mode=WAL")
    setup.execute("CREATE TABLE t (v INTEGER)")
    setup.commit()
    setup.close()

    errors: list[str] = []
    lock = threading.Lock()  # stands in for write_gate's writer serialization

    def writer(value: int) -> None:
        own = sqlite3.connect(str(db), timeout=5)
        try:
            with lock:
                own.execute("BEGIN IMMEDIATE")
                own.execute("INSERT INTO t VALUES (?)", (value,))
                own.commit()
        except sqlite3.Error as exc:  # pragma: no cover - failure path
            errors.append(str(exc))
        finally:
            own.close()

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert errors == []
    check = sqlite3.connect(str(db))
    assert check.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 8
    check.close()


# --- get_db_connection hands each thread its own -----------------------------


def test_get_db_connection_is_per_thread(tmp_path, monkeypatch):
    from topos.core import state

    db = tmp_path / "engine.db"
    monkeypatch.setattr(state, "db_conn", None, raising=False)
    monkeypatch.setattr(state, "_db_conn_path", None, raising=False)
    monkeypatch.setattr(state, "_conn_owner_thread", None, raising=False)
    monkeypatch.setattr(state, "_thread_state", threading.local(), raising=False)
    monkeypatch.setattr(state, "_resolve_database_path_from_settings", lambda: db, raising=False)

    main_conn = state.get_db_connection()
    if main_conn is None:
        pytest.skip("database could not be opened in this environment")

    # Same thread -> same object (no churn).
    assert state.get_db_connection() is main_conn

    seen: dict[str, object] = {}

    def worker() -> None:
        seen["conn"] = state.get_db_connection()
        seen["again"] = state.get_db_connection()
        state.close_thread_db_connection()

    t = threading.Thread(target=worker)
    t.start()
    t.join(10)

    assert seen.get("conn") is not None
    assert seen["conn"] is not main_conn, "worker thread must not share the owner connection"
    assert seen["again"] is seen["conn"], "per-thread connection should be cached"


def test_in_memory_handle_is_shared_not_reopened(monkeypatch):
    """`:memory:` cannot be reopened per thread — each copy would be empty."""
    from topos.core import state

    mem = sqlite3.connect(":memory:", check_same_thread=False)
    mem.row_factory = sqlite3.Row
    mem.execute("CREATE TABLE marker (v INTEGER)")
    mem.commit()

    monkeypatch.setattr(state, "db_conn", mem, raising=False)
    monkeypatch.setattr(state, "_db_conn_path", None, raising=False)
    monkeypatch.setattr(state, "_conn_owner_thread", None, raising=False)
    monkeypatch.setattr(state, "_thread_state", threading.local(), raising=False)

    seen: dict[str, object] = {}

    def worker() -> None:
        conn = state.get_db_connection()
        seen["conn"] = conn
        # The shared in-memory DB must still have the table.
        seen["rows"] = conn.execute("SELECT COUNT(*) FROM marker").fetchone()[0]

    t = threading.Thread(target=worker)
    t.start()
    t.join(10)

    assert seen.get("conn") is mem
    assert seen.get("rows") == 0
    mem.close()


# --- durable derivation failures ---------------------------------------------


def _memory_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def test_failed_derivation_is_recorded_and_listed():
    from topos.enrichment.derivation_recovery import (
        list_pending_derivation_retries,
        pending_derivation_summary,
        record_failed_derivation,
    )

    conn = _memory_db()
    job_id = record_failed_derivation(
        conn,
        source_id="github_activity",
        sync_batch_id="1bcab09c",
        job_name="facts",
        error="cannot commit - no transaction is active",
        record_ids=[f"push:{i}" for i in range(88)],
        record_count=88,
    )
    assert job_id

    pending = list_pending_derivation_retries(conn)
    assert len(pending) == 1
    entry = pending[0]
    assert entry["job_name"] == "facts"
    assert entry["source_id"] == "github_activity"
    assert entry["record_count"] == 88
    assert "no transaction is active" in entry["error"]

    summary = pending_derivation_summary(conn)
    assert summary["known_gaps"] is True
    assert summary["pending_derivations"] == 1
    assert summary["affected_records"] == 88
    assert summary["by_job"] == {"facts": 1}
    conn.close()


def test_recording_the_same_failure_twice_is_one_debt():
    from topos.enrichment.derivation_recovery import (
        list_pending_derivation_retries,
        record_failed_derivation,
    )

    conn = _memory_db()
    for _ in range(3):
        record_failed_derivation(
            conn,
            source_id="github_activity",
            sync_batch_id="batch-1",
            job_name="facts",
            error="boom",
            record_count=5,
        )
    assert len(list_pending_derivation_retries(conn)) == 1
    conn.close()


def test_success_clears_the_debt():
    from topos.enrichment.derivation_recovery import (
        clear_derivation_retry,
        list_pending_derivation_retries,
        pending_derivation_summary,
        record_failed_derivation,
    )

    conn = _memory_db()
    record_failed_derivation(
        conn,
        source_id="github_activity",
        sync_batch_id="batch-1",
        job_name="facts",
        error="boom",
        record_count=5,
    )
    assert clear_derivation_retry(conn, sync_batch_id="batch-1", job_name="facts") is True
    assert list_pending_derivation_retries(conn) == []
    assert pending_derivation_summary(conn)["known_gaps"] is False
    conn.close()


def test_recording_never_raises_without_a_connection():
    """Losing the DB must not turn a partial batch into a crashed one."""
    from topos.enrichment.derivation_recovery import (
        clear_derivation_retry,
        list_pending_derivation_retries,
        pending_derivation_summary,
        record_failed_derivation,
    )

    assert record_failed_derivation(
        None, source_id="s", sync_batch_id="b", job_name="j", error="e"
    ) is None
    assert clear_derivation_retry(None, sync_batch_id="b", job_name="j") is False
    assert list_pending_derivation_retries(None) == []
    assert pending_derivation_summary(None)["known_gaps"] is False


# --- pipeline coordination ---------------------------------------------------


def test_derivation_in_flight_is_visible_to_background_rebuilds():
    from topos.enrichment.pipeline_activity import (
        derivation_in_flight,
        is_derivation_in_flight,
        reset_for_tests,
    )

    reset_for_tests()
    assert is_derivation_in_flight() is False
    with derivation_in_flight():
        assert is_derivation_in_flight() is True
        # Nested/concurrent batches must not clear the flag early.
        with derivation_in_flight():
            assert is_derivation_in_flight() is True
        assert is_derivation_in_flight() is True
    assert is_derivation_in_flight() is False


def test_derivation_flag_clears_on_exception():
    from topos.enrichment.pipeline_activity import (
        derivation_in_flight,
        is_derivation_in_flight,
        reset_for_tests,
    )

    reset_for_tests()
    with pytest.raises(ValueError):
        with derivation_in_flight():
            raise ValueError("batch blew up")
    assert is_derivation_in_flight() is False, "a failed batch must not wedge the flag"


def test_graph_refresh_defers_while_a_batch_is_running():
    """A 77s rebuild must not fire mid-batch and hold the write gate."""
    from topos.enrichment.pipeline_activity import derivation_in_flight, reset_for_tests
    from topos.features.entities.graph_refresh import _Refresher

    reset_for_tests()
    ran: list[int] = []
    refresher = _Refresher(rebuild_fn=lambda: ran.append(1))

    with derivation_in_flight():
        refresher._fire()
        assert ran == [], "rebuild should have deferred while the batch is in flight"

    refresher._fire()
    assert ran == [1], "rebuild should run once the batch is done"
    refresher.shutdown()


# --- DDL is issued once per connection, not once per record ------------------


def test_enrichment_ddl_runs_once_per_connection():
    """One 88-record import built 75 managers, each re-issuing the same DDL."""
    from topos.enrichment.derived_tables import (
        DerivedTablesManager,
        reset_ensured_tables_cache,
    )

    class Counting(sqlite3.Connection):
        creates = 0

        def execute(self, sql, *a, **k):  # type: ignore[override]
            if "CREATE" in str(sql).upper():
                Counting.creates += 1
            return super().execute(sql, *a, **k)

    reset_ensured_tables_cache()
    conn = sqlite3.connect(":memory:", factory=Counting)
    conn.row_factory = sqlite3.Row
    try:
        for _ in range(75):
            DerivedTablesManager(conn=conn)
        first_pass = Counting.creates
        assert 0 < first_pass <= 10, f"expected one DDL pass, got {first_pass} statements"

        # A different connection must still get its own DDL.
        Counting.creates = 0
        other = sqlite3.connect(":memory:", factory=Counting)
        other.row_factory = sqlite3.Row
        DerivedTablesManager(conn=other)
        assert Counting.creates > 0, "a new connection must still be initialized"
        other.close()
    finally:
        reset_ensured_tables_cache()
        conn.close()


# --- global recompute is deferred out of the ingest path ---------------------


def test_consolidation_is_queued_not_run_inline():
    """A full recompute across 27 sources took 44s inside an 88-record batch."""
    from topos.enrichment.jobs.canonical.topic_clusters_job import (
        TOPIC_CONSOLIDATION_KIND,
        _defer_consolidation,
    )

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        assert _defer_consolidation(conn) is True
        # Idempotent: a hundred batches leave exactly one pending consolidation.
        for _ in range(10):
            _defer_consolidation(conn)
        count = conn.execute(
            "SELECT COUNT(*) FROM pipeline_jobs WHERE kind=?", (TOPIC_CONSOLIDATION_KIND,)
        ).fetchone()[0]
        assert count == 1, f"consolidation should coalesce, found {count} rows"
    finally:
        conn.close()


def test_consolidation_deferral_can_be_disabled(monkeypatch):
    """Escape hatch: a consolidation that never happens is worse than a slow one."""
    from topos.enrichment.jobs.canonical.topic_clusters_job import _defer_consolidation

    monkeypatch.setenv("TOPOS_DEFER_TOPIC_CONSOLIDATION", "off")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    try:
        assert _defer_consolidation(conn) is False, "should fall back to running inline"
    finally:
        conn.close()


def test_consolidation_falls_back_inline_when_queue_unavailable():
    from topos.enrichment.jobs.canonical.topic_clusters_job import _defer_consolidation

    assert _defer_consolidation(None) is False


def test_topic_consolidation_executor_is_registered():
    from topos.pipeline.job_runner import EXECUTORS
    from topos.enrichment.jobs.canonical.topic_clusters_job import TOPIC_CONSOLIDATION_KIND

    assert TOPIC_CONSOLIDATION_KIND in EXECUTORS, "deferred work needs a worker that runs it"


def test_derivation_retry_executor_is_registered():
    """The same assertion, for the kind that did NOT have one.

    ``record_failed_derivation`` enqueued `signal_derive_retry` rows for weeks
    with nothing registered to run them, so the worker claimed each one and
    immediately failed it as an unknown kind — the retry meant to repair a lost
    derivation destroyed the record of it instead. This is the check that would
    have caught it on the first run.
    """
    from topos.pipeline.job_runner import EXECUTORS
    from topos.enrichment.derivation_recovery import SIGNAL_DERIVE_RETRY_KIND

    assert SIGNAL_DERIVE_RETRY_KIND in EXECUTORS, "recorded derivation debt needs a worker that runs it"


# --- the diagnostic must not become the problem ------------------------------


def test_loop_warning_is_rate_limited_per_call_site():
    """A 250ms poll loop turned this warning into thousands of stack traces."""
    import asyncio
    import logging

    from topos.storage.db import write_gate

    write_gate.reset_loop_warning_state()
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    write_gate.logger.addHandler(handler)
    try:

        async def hammer() -> None:
            for _ in range(50):
                with write_gate.with_db_write():
                    pass

        asyncio.run(hammer())
    finally:
        write_gate.logger.removeHandler(handler)
        write_gate.reset_loop_warning_state()

    warned = [r for r in records if "acquired on the event-loop thread" in r.getMessage()]
    assert len(warned) == 1, f"expected one warning for 50 acquisitions, got {len(warned)}"
    # It has to say WHERE, or it is unactionable.
    assert "in hammer" in warned[0].getMessage()


def test_distinct_call_sites_each_get_warned():
    """A noisy first offender must never mask a second one."""
    import asyncio
    import logging

    from topos.storage.db import write_gate

    write_gate.reset_loop_warning_state()
    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = _Capture()
    write_gate.logger.addHandler(handler)
    try:

        async def site_one() -> None:
            with write_gate.with_db_write():
                pass

        async def site_two() -> None:
            with write_gate.with_db_write():
                pass

        async def both() -> None:
            await site_one()
            await site_one()
            await site_two()

        asyncio.run(both())
    finally:
        write_gate.logger.removeHandler(handler)
        write_gate.reset_loop_warning_state()

    warned = [m for m in messages if "acquired on the event-loop thread" in m]
    assert len(warned) == 2, f"one warning per site expected, got {len(warned)}"
    assert any("site_one" in m for m in warned)
    assert any("site_two" in m for m in warned)


def test_commit_connection_warns_on_the_event_loop():
    """commit_connection takes the same blocking lock as with_db_write but
    bypassed its instrument — during the 2026-08-07 rebuild stall the caller
    queuing behind the 120s gate hold was invisible for exactly this reason."""
    import asyncio
    import logging
    import sqlite3

    from topos.storage.db import write_gate

    write_gate.reset_loop_warning_state()
    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = _Capture()
    write_gate.logger.addHandler(handler)
    conn = sqlite3.connect(":memory:")
    try:

        async def commit_site() -> None:
            write_gate.commit_connection(conn)

        asyncio.run(commit_site())
    finally:
        write_gate.logger.removeHandler(handler)
        write_gate.reset_loop_warning_state()
        conn.close()

    warned = [m for m in messages if "acquired on the event-loop thread" in m]
    assert len(warned) == 1, f"expected commit_connection to warn, got {warned}"
    assert "in commit_site" in warned[0], warned[0]


def test_batched_writes_warns_on_the_event_loop():
    import asyncio
    import logging
    import sqlite3

    from topos.storage.db import write_gate

    write_gate.reset_loop_warning_state()
    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = _Capture()
    write_gate.logger.addHandler(handler)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (v TEXT)")
    try:

        async def batch_site() -> None:
            with write_gate.batched_writes(conn):
                conn.execute("INSERT INTO t (v) VALUES ('x')")

        asyncio.run(batch_site())
    finally:
        write_gate.logger.removeHandler(handler)
        write_gate.reset_loop_warning_state()
        conn.close()

    warned = [m for m in messages if "acquired on the event-loop thread" in m]
    assert len(warned) == 1, f"expected batched_writes to warn, got {warned}"
    assert "in batch_site" in warned[0], warned[0]


def test_commit_connection_defer_path_stays_silent_off_the_lock():
    """Inside batched_writes the commit is deferred and no lock is taken, so
    the nested commit_connection call must not double-warn for the same site."""
    import asyncio
    import logging
    import sqlite3

    from topos.storage.db import write_gate

    write_gate.reset_loop_warning_state()
    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = _Capture()
    write_gate.logger.addHandler(handler)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (v TEXT)")
    try:

        async def batch_then_commit() -> None:
            with write_gate.batched_writes(conn):
                conn.execute("INSERT INTO t (v) VALUES ('x')")
                write_gate.commit_connection(conn)  # deferred: no lock, no warn

        asyncio.run(batch_then_commit())
    finally:
        write_gate.logger.removeHandler(handler)
        write_gate.reset_loop_warning_state()
        conn.close()

    warned = [m for m in messages if "acquired on the event-loop thread" in m]
    assert len(warned) == 1, f"only batched_writes should warn, got {warned}"


def test_commit_connection_flags_writes_executed_outside_the_gate():
    """In WAL an ungated INSERT takes SQLite's write lock at execute time; the
    caller then queues on the gate holding it, and the gate holder blocks on
    SQLite until busy_timeout — the lock-order inversion behind the stretched
    2026-08-07 rebuild holds. commit_connection is the chokepoint that can see
    the pattern: open transaction + gate not held by this thread."""
    import logging
    import sqlite3

    from topos.storage.db import write_gate

    write_gate.reset_loop_warning_state()
    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = _Capture()
    write_gate.logger.addHandler(handler)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.commit()
    try:
        conn.execute("INSERT INTO t (v) VALUES ('ungated')")  # opens the txn
        assert conn.in_transaction
        write_gate.commit_connection(conn)
    finally:
        write_gate.logger.removeHandler(handler)
        write_gate.reset_loop_warning_state()
        conn.close()

    warned = [m for m in messages if "open write transaction" in m]
    assert len(warned) == 1, f"expected the ungated-transaction warning, got {messages}"
    assert "test_commit_connection_flags_writes_executed_outside_the_gate" in warned[0]


def test_commit_connection_stays_silent_when_gate_held_around_writes():
    """The correct pattern (with_db_write around execute AND commit) must not
    trip the ungated-transaction diagnostic."""
    import logging
    import sqlite3

    from topos.storage.db import write_gate

    write_gate.reset_loop_warning_state()
    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = _Capture()
    write_gate.logger.addHandler(handler)
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.commit()
    try:
        with write_gate.with_db_write():
            conn.execute("INSERT INTO t (v) VALUES ('gated')")
            write_gate.commit_connection(conn)
    finally:
        write_gate.logger.removeHandler(handler)
        write_gate.reset_loop_warning_state()
        conn.close()

    warned = [m for m in messages if "open write transaction" in m]
    assert not warned, f"gated write pattern must not warn: {warned}"


def test_watchdog_is_wired_to_connections_from_get_db_connection(tmp_path, monkeypatch):
    """The never-committed write is only visible if the watchdog is actually
    attached to the handles the engine hands out — every one of them comes from
    get_db_connection, so that wiring is the thing worth pinning. This is the
    StatisticsJob._should_promote shape end to end: a worker thread writes
    ungated through its own connection, returns without committing, and exits.
    """
    import logging
    import time

    from topos.core import state
    from topos.storage.db import write_gate

    db = tmp_path / "watched.db"
    monkeypatch.setattr(state, "db_conn", None, raising=False)
    monkeypatch.setattr(state, "_db_conn_path", None, raising=False)
    monkeypatch.setattr(state, "_conn_owner_thread", None, raising=False)
    monkeypatch.setattr(state, "_thread_state", threading.local(), raising=False)
    monkeypatch.setattr(state, "_resolve_database_path_from_settings", lambda: db, raising=False)

    messages: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            messages.append(record.getMessage())

    handler = _Capture()
    threshold = write_gate._watchdog_threshold_s
    interval = write_gate._watchdog_interval_s
    write_gate.reset_loop_warning_state()
    # Enable BEFORE anything opens a connection: registration is what attaches.
    write_gate.enable_txn_watchdog(interval_s=3600.0)
    write_gate.logger.addHandler(handler)
    owner = None
    worker_conn: dict[str, object] = {}
    try:
        owner = state.get_db_connection()
        if owner is None:
            pytest.skip("database could not be opened in this environment")
        owner.execute("CREATE TABLE IF NOT EXISTS watched (v TEXT)")
        owner.commit()

        def _should_promote(conn) -> bool:
            conn.execute("UPDATE watched SET v='x' WHERE v='__nope__'")
            return False

        def worker() -> None:
            conn = state.get_db_connection()
            worker_conn["conn"] = conn
            _should_promote(conn)  # ungated, never committed

        t = threading.Thread(target=worker, name="stats-worker")
        t.start()
        t.join(10)

        assert worker_conn["conn"] is not owner, "worker must have its own connection"
        assert worker_conn["conn"].in_transaction, "the leaked transaction is the premise"

        reported = write_gate._scan_open_transactions(now=time.monotonic() + 60)
        assert reported == 1, f"expected the stuck transaction to be reported, got {messages}"
    finally:
        write_gate.logger.removeHandler(handler)
        if worker_conn.get("conn") is not None:
            worker_conn["conn"].rollback()
            write_gate.unregister_connection(worker_conn["conn"])
        if owner is not None:
            write_gate.unregister_connection(owner)
        write_gate.disable_txn_watchdog()
        write_gate.reset_loop_warning_state()
        write_gate._watchdog_threshold_s = threshold
        write_gate._watchdog_interval_s = interval

    # Exactly one: the migrations that just ran on the owner connection took the
    # gate, so none of them may be reported alongside the real offender.
    warned = [m for m in messages if "has stayed open" in m]
    assert len(warned) == 1, f"expected exactly one report, got {warned}"
    assert "in _should_promote" in warned[0], warned[0]
    assert "stats-worker" in warned[0], warned[0]


def test_worker_loop_claims_off_the_event_loop():
    """claim_next_job takes the blocking write gate; it must not run on the loop."""
    import inspect

    from topos.pipeline import job_runner

    src = inspect.getsource(job_runner._worker_loop)
    assert "asyncio.to_thread(_claim)" in src, (
        "the worker must offload the claim — polling it on the event loop "
        "stalls every coroutine behind the write gate four times a second"
    )
    # And it must open its OWN connection in that thread. Passing the loop
    # thread's connection across would silently reinstate the cross-thread
    # sharing that caused the transaction corruption this file exists for.
    claim_body = src[src.index("def _claim"):]
    assert "conn_factory()" in claim_body, (
        "the offloaded claim must re-invoke the factory inside the worker "
        "thread, not capture the event loop's connection"
    )
    # And it should ease off when there is nothing to do.
    assert "_MAX_POLL_SECONDS" in src


# --- the summary must not overstate what it knows -----------------------------


def test_clean_summary_never_claims_completeness():
    """`known_gaps: false` means "nothing failed while watching", NOT "you have
    everything". The field names exist so a UI cannot casually misread it."""
    from topos.enrichment.derivation_recovery import (
        DEBT_COVERAGE,
        pending_derivation_summary,
    )

    conn = _memory_db()
    try:
        summary = pending_derivation_summary(conn)
        assert summary["known_gaps"] is False
        assert summary["pending_derivations"] == 0
        # The caveats travel WITH the clean answer, not just the dirty one.
        assert summary["recording_since"], "a clean report must say since when"
        assert summary["covers"] == DEBT_COVERAGE
        assert "not ingestion" in summary["covers"]
        # And the old overstating field is gone for good.
        assert "healthy" not in summary
    finally:
        conn.close()


def test_recording_since_is_stamped_once_and_is_stable():
    """It answers "since when have you been watching?" — so it must be the
    upgrade moment, not a moving timestamp, and not the date the code shipped."""
    from topos.enrichment.derivation_recovery import (
        RECORDING_SINCE_KEY,
        ensure_recording_since,
    )
    from topos.core.state import get_engine_config_value

    conn = _memory_db()
    try:
        first = ensure_recording_since(conn)
        assert first
        assert ensure_recording_since(conn) == first, "must not move on re-read"
        assert get_engine_config_value(conn, RECORDING_SINCE_KEY) == first
    finally:
        conn.close()


def test_recording_since_degrades_to_none_without_a_database():
    """No connection means we genuinely do not know — say None, do not guess."""
    from topos.enrichment.derivation_recovery import (
        ensure_recording_since,
        pending_derivation_summary,
    )

    assert ensure_recording_since(None) is None
    summary = pending_derivation_summary(None)
    assert summary["recording_since"] is None
    assert summary["known_gaps"] is False


def test_summary_reports_gaps_with_their_scope():
    from topos.enrichment.derivation_recovery import (
        pending_derivation_summary,
        record_failed_derivation,
    )

    conn = _memory_db()
    try:
        record_failed_derivation(
            conn,
            source_id="github_activity",
            sync_batch_id="b1",
            job_name="facts",
            error="cannot commit - no transaction is active",
            record_count=88,
        )
        summary = pending_derivation_summary(conn)
        assert summary["known_gaps"] is True
        assert summary["affected_records"] == 88
        assert summary["by_source"] == {"github_activity": 1}
        # Caveats still present when there ARE gaps — a UI renders one component.
        assert summary["recording_since"]
        assert summary["covers"]
    finally:
        conn.close()
