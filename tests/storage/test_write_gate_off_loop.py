"""Guard: the hot paths never take the SQLite write gate on the event loop.

The gate is a blocking OS lock, so holding it inside a coroutine stalls every
other coroutine on the engine loop — including the control-plane keepalive,
which is the node-side half of the 2026-08-15 relay outage (relayed requests
answered late or not at all under enrichment).

``write_gate`` already warns when a gate acquisition happens on the loop thread.
These tests pin that warning to zero for the paths that produced the most of
them in the field: the sources API (``install_service``, 147 warnings in one
log) and the Usage Inbox dedupe store. A regression here reads as a returning
stall, not as a style violation.
"""

from __future__ import annotations

import logging
import sqlite3

import pytest

from topos.storage.db import write_gate

pytestmark = pytest.mark.asyncio

_LOOP_ACQUISITION = "acquired on the event-loop thread"


@pytest.fixture
def loop_gate_warnings():
    """Records that write_gate emitted for a loop-thread acquisition.

    Handler attached to write_gate's own logger rather than via ``caplog``:
    engine logging does not propagate these to root, so a caplog-based fixture
    captures nothing and every "no warnings" assertion below would pass
    vacuously. ``test_guard_still_fires_when_work_stays_on_the_loop`` is what
    keeps that honest.
    """
    logger = logging.getLogger("topos.storage.db.write_gate")
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture(level=logging.WARNING)
    # Rate limiting is per call site for 300s, so an earlier test in the same
    # process would otherwise silence the very warning being asserted on.
    write_gate.reset_loop_warning_state()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _loop_sites(records) -> list[str]:
    return [r.getMessage() for r in records if _LOOP_ACQUISITION in r.getMessage()]


async def test_list_sources_does_not_gate_on_the_loop(loop_gate_warnings) -> None:
    """The sources API ensures the install schema — off the loop.

    ``_list_sources_core`` calls rehydrate + list_installs, both of which run
    ``ensure_install_schema`` (DDL under the gate). It was the single busiest
    loop-thread acquisition in the field log.
    """
    from topos.api import source_install

    result = await source_install._list_sources_core(
        {"user_id": "u1", "dataset_id": "u1:default", "topos_id": "t1"}
    )

    assert result["status"] == "ok"
    assert _loop_sites(loop_gate_warnings) == []


async def test_check_inbox_write_does_not_gate_on_the_loop(
    tmp_path, monkeypatch, loop_gate_warnings
) -> None:
    """The dedupe lookup opens its schema under the gate — off the loop."""
    conn = sqlite3.connect(str(tmp_path / "dedupe.db"), check_same_thread=False)
    monkeypatch.setattr(
        "topos.ingestion.usage_inbox_dedupe.get_db_connection", lambda: conn
    )
    try:
        from topos.core.handlers.ingest import handle_check_inbox_write

        result = await handle_check_inbox_write(
            {"id": "req-1", "type": "check_inbox_write", "payload": {"write_id": "w-1"}}
        )

        assert result["status"] == "ok"
        assert result["payload"]["delivered"] is False
        assert _loop_sites(loop_gate_warnings) == []
    finally:
        conn.close()


async def test_guard_still_fires_when_work_stays_on_the_loop(loop_gate_warnings) -> None:
    """The other tests would pass if the guard were broken; this proves it is not."""
    with write_gate.with_db_write():
        pass

    assert _loop_sites(loop_gate_warnings), "write_gate stopped reporting loop acquisitions"


async def test_reapplying_pipeline_jobs_schema_does_not_gate_on_the_loop(
    tmp_path, loop_gate_warnings
) -> None:
    """job_store.ensure_pipeline_jobs_schema used to take the write gate on
    every enqueue, including from the event loop. After the first apply it
    must be a read."""
    import asyncio

    from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up

    conn = sqlite3.connect(str(tmp_path / "jobs.db"), check_same_thread=False)
    try:
        await asyncio.to_thread(apply_pipeline_jobs_v1_up, conn)
        write_gate.reset_loop_warning_state()
        apply_pipeline_jobs_v1_up(conn)
        assert _loop_sites(loop_gate_warnings) == []
    finally:
        conn.close()


async def test_the_progress_marker_does_not_wait_for_the_gate_on_the_loop(
    tmp_path, monkeypatch, loop_gate_warnings
) -> None:
    """After every enrichment batch a marker row is written under the gate. Both callers are coroutines on the
    app's loop (the enrichment orchestrator, the manual backfill), and both wrote it where they stood: the loop
    waited for the gate, with every owner-app request behind it, for as long as a share decision held it
    (NR166: 2 to 30 s measured). The write now waits in a worker thread.

    Another thread holds the gate here for 1.5 s. The loop must keep running through all of it, the marker must
    still be written once the gate is free, and the gate must not report an acquisition on a loop thread.
    """
    import asyncio
    import threading
    import time

    from topos.enrichment.orchestrator import EnrichmentOrchestrator
    from topos.enrichment.record_progress import mark_records_processed_in_a_worker, processed_record_ids
    from topos.storage.db.migrations import apply_all_migrations

    conn = sqlite3.connect(str(tmp_path / "progress.db"), check_same_thread=False)
    await asyncio.to_thread(apply_all_migrations, conn)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    records = [{"record_id": f"an-invented-record-{n}", "source_id": "an_invented_source"} for n in range(3)]
    holding, release = threading.Event(), threading.Event()

    def hold_the_gate():
        with write_gate.with_db_write():
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold_the_gate, name="a-share-decision")
    holder.start()
    assert await asyncio.to_thread(holding.wait, 5)
    threading.Timer(1.5, release.set).start()
    gaps, last, done = [0.0], time.monotonic(), asyncio.Event()

    async def tick():
        nonlocal last
        while not done.is_set():
            await asyncio.sleep(0.01)
            now = time.monotonic()
            gaps[0], last = max(gaps[0], now - last), now

    ticker = asyncio.create_task(tick())
    write_gate.reset_loop_warning_state()
    started = time.monotonic()
    try:
        # The manual backfill's call, and the orchestrator's (its own method, in the worker it now runs in).
        marked = await mark_records_processed_in_a_worker(lambda: conn, "an_invented_source", "entities", records, 1)
        await asyncio.to_thread(EnrichmentOrchestrator._mark_processed, object(), "sentiment", records)
    finally:
        done.set()
        await ticker
        release.set()
        await asyncio.to_thread(holder.join, 10)
    waited = time.monotonic() - started
    assert waited >= 1.2, "the gate was not held: this proved nothing"
    assert gaps[0] < 0.5, f"the loop stopped for {gaps[0]:.2f}s while the marker waited for the gate"
    assert marked == 3
    ids = {record["record_id"] for record in records}
    assert await asyncio.to_thread(processed_record_ids, conn, "an_invented_source", "entities", 0) == ids
    assert await asyncio.to_thread(processed_record_ids, conn, "an_invented_source", "sentiment", 0) == ids
    assert _loop_sites(loop_gate_warnings) == []
    conn.close()


async def test_both_callers_of_the_progress_marker_hand_it_to_a_worker() -> None:
    """The two coroutines that write the marker, read from their source: neither calls it where it stands."""
    import inspect

    from topos.api import enrichment as api_enrichment
    from topos.enrichment.orchestrator import EnrichmentOrchestrator

    assert "await asyncio.to_thread(self._mark_processed, job_name, canonical_messages)" in inspect.getsource(
        EnrichmentOrchestrator.run_canonical)
    source = inspect.getsource(api_enrichment)
    assert "await mark_records_processed_in_a_worker(" in source
    assert "marked = _mark_records_processed(" not in source


async def test_the_on_loop_warning_says_which_loop(loop_gate_warnings) -> None:
    """A worker that starts a loop of its own also has "a running loop". Five of the six sites one day's log
    blamed for stalling the node were such a private loop, which stalls only itself. The warning now names the
    loop: the app's, the client's, or a private one."""
    import asyncio
    import threading

    with write_gate.with_db_write():      # this test's loop: nobody named it
        pass
    write_gate.name_event_loop(asyncio.get_running_loop(), "app")
    try:
        write_gate.reset_loop_warning_state()
        with write_gate.with_db_write():
            pass
    finally:
        write_gate._named_loops.pop("app", None)

    def a_worker_with_its_own_loop():
        async def work():
            with write_gate.with_db_write():
                pass
        asyncio.run(work())

    write_gate.reset_loop_warning_state()
    worker = threading.Thread(target=a_worker_with_its_own_loop)
    worker.start()
    await asyncio.to_thread(worker.join, 10)
    private, app, worker_loop = _loop_sites(loop_gate_warnings)
    assert " loop=private " in private and "app and client loops are not waiting" in private
    assert " loop=app " in app and "every coroutine on the node's app loop" in app
    assert " loop=private " in worker_loop
    assert all("acquired on the event-loop thread from " in line for line in (private, app, worker_loop))
