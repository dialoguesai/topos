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
