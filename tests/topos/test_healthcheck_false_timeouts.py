"""Regressions for the 2026-08-30 tray flicker (red ↔ green on a live node).

The node was busy (ingest, graph rebuild, write-gate waits of ~13s). The tray
treated one late ``/healthcheck`` as "down". These tests pin the three fixes:

1. Tray hysteresis — one miss stays green; two consecutive misses go red.
2. ``apply_pipeline_jobs_v1_up`` is a no-op read after the first apply, so
   ``ensure_pipeline_jobs_schema`` no longer takes the write gate on the loop.
3. Blackhole HTTP/WS writes run off the event loop, so a hold cannot stall
   ``/healthcheck``.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from types import SimpleNamespace

import httpx
import pytest

import topos.core.handlers as hub
from topos.cli import tray
from topos.core.handlers.signal_features import (
    handle_signal_blackhole_entity,
    handle_signal_unblackhole_entity,
)
from topos.features.lifecycle.blackhole import BlackholeStore
from topos.storage.db import write_gate
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up

_LOOP_ACQUISITION = "acquired on the event-loop thread"


class _FakeIcon:
    def __init__(self) -> None:
        self.visible = True
        self.icon = None
        self.menu = None


@pytest.fixture
def loop_gate_warnings():
    logger = logging_get_write_gate()
    records: list = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture(level=logging.WARNING)
    write_gate.reset_loop_warning_state()
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


def logging_get_write_gate():
    import logging

    return logging.getLogger("topos.storage.db.write_gate")


def _loop_sites(records) -> list[str]:
    return [r.getMessage() for r in records if _LOOP_ACQUISITION in r.getMessage()]


def _tray() -> tray.ToposTray:
    t = tray.ToposTray(
        host="127.0.0.1",
        port=9000,
        version="1.0.0",
        package_name="topos-node",
        on_quit=lambda: None,
    )
    t._icon = _FakeIcon()
    t.status = "healthy"
    return t


def _run_poller(t: tray.ToposTray, monkeypatch, health_fn, *, stop_after: int) -> list[float]:
    """Drive ``_poll_health`` for ``stop_after`` probes, then hide the icon."""
    timeouts: list[float] = []
    probes = {"n": 0}

    def fake_get(url, timeout=3.0, **_kwargs):
        if url != t.health_url:
            return SimpleNamespace(status_code=200, json=lambda: {})
        timeouts.append(float(timeout))
        return health_fn(probes)

    def fake_sleep(_seconds):
        if probes["n"] >= stop_after:
            t._icon.visible = False

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(tray.time, "sleep", fake_sleep)
    monkeypatch.setattr(t, "_fetch_shell_status", lambda: None)
    monkeypatch.setattr(t, "_maybe_fetch_topos_name", lambda: None)
    t._poll_health()
    return timeouts


# ---------------------------------------------------------------- tray poller


class TestTrayPollerHysteresis:
    def test_one_timeout_keeps_a_live_node_green(self, monkeypatch):
        t = _tray()

        def health(probes):
            probes["n"] += 1
            raise httpx.TimeoutException("late")

        timeouts = _run_poller(t, monkeypatch, health, stop_after=1)

        assert t.status == "healthy"
        assert timeouts == [tray.HEALTH_TIMEOUT_SECONDS]
        assert tray.HEALTH_TIMEOUT_SECONDS > 2.0

    def test_two_consecutive_timeouts_go_red(self, monkeypatch):
        t = _tray()

        def health(probes):
            probes["n"] += 1
            raise httpx.TimeoutException("late")

        _run_poller(t, monkeypatch, health, stop_after=2)

        assert t.status == "down"

    def test_a_success_after_one_miss_stays_green(self, monkeypatch):
        t = _tray()

        def health(probes):
            probes["n"] += 1
            if probes["n"] == 1:
                raise httpx.TimeoutException("late")
            return SimpleNamespace(status_code=200)

        _run_poller(t, monkeypatch, health, stop_after=2)

        assert t.status == "healthy"

    def test_auth_challenge_is_still_up(self, monkeypatch):
        """401/403 mean health auth is on — the node answered."""
        t = _tray()
        t.status = "starting"

        def health(probes):
            probes["n"] += 1
            return SimpleNamespace(status_code=401)

        _run_poller(t, monkeypatch, health, stop_after=1)

        assert t.status == "healthy"

    def test_nonconsecutive_misses_never_go_red(self):
        status, failures = "healthy", 0
        for probe_ok in (False, True, False):
            status, failures = tray.resolve_tray_health_status(
                probe_ok=probe_ok,
                consecutive_failures=failures,
                current_status=status,
            )
        assert status == "healthy"
        assert failures == 1


# -------------------------------------------------------- pipeline schema


def test_first_apply_still_creates_pipeline_jobs_tables(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "fresh.db"))
    try:
        apply_pipeline_jobs_v1_up(conn)
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "pipeline_jobs" in tables
        assert "wiki_schema_migrations" in tables
        assert (
            conn.execute(
                "SELECT 1 FROM wiki_schema_migrations WHERE migration_id='pipeline_jobs_v1'"
            ).fetchone()
            is not None
        )
    finally:
        conn.close()


def test_reapply_does_not_block_on_a_held_write_gate(tmp_path) -> None:
    """The skip must be a read. If it still entered the gate, a held lock
    would stall this call for the holder's full wait — the 13s field stall."""
    conn = sqlite3.connect(str(tmp_path / "held.db"))
    apply_pipeline_jobs_v1_up(conn)
    release = threading.Event()
    holding = threading.Event()

    def holder() -> None:
        with write_gate.with_db_write():
            holding.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=holder)
    thread.start()
    assert holding.wait(timeout=5)
    try:
        started = time.monotonic()
        apply_pipeline_jobs_v1_up(conn)
        assert time.monotonic() - started < 0.5
    finally:
        release.set()
        thread.join(timeout=5)
        conn.close()


# ----------------------------------------------------- blackhole off-loop


def _seed_entity(conn: sqlite3.Connection, entity_id: str = "ent-bh") -> None:
    conn.execute(
        """
        INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name)
        VALUES (?, 'person', 'Dana Reyes', 'dana reyes')
        """,
        (entity_id,),
    )
    conn.commit()


@pytest.fixture
def blackhole_conn(tmp_path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "bh.db"), check_same_thread=False)
    apply_all_migrations(conn)
    _seed_entity(conn)
    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)

    import topos.core.state as state_mod

    monkeypatch.setattr(state_mod, "get_db_connection", lambda: conn)
    monkeypatch.setattr(state_mod, "close_thread_db_connection", lambda: None)
    # apply_all_migrations takes the write gate on this thread; drop those
    # warnings so the tests only see what the handlers themselves emit.
    write_gate.reset_loop_warning_state()
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_unblackhole_handler_writes_and_stays_off_the_loop(
    blackhole_conn, loop_gate_warnings
) -> None:
    await asyncio.to_thread(
        BlackholeStore(blackhole_conn).blackhole_entity, entity_ref="ent-bh"
    )
    write_gate.reset_loop_warning_state()

    result = await handle_signal_unblackhole_entity(
        {"id": "req-1", "type": "signal_unblackhole_entity", "payload": {"entity_id": "ent-bh"}}
    )

    assert result["status"] == "ok"
    assert result["payload"]["removed"] is True
    assert BlackholeStore(blackhole_conn).get("ent-bh") is None
    assert _loop_sites(loop_gate_warnings) == []


@pytest.mark.asyncio
async def test_blackhole_handler_offloads_the_flag_write(
    blackhole_conn, loop_gate_warnings, monkeypatch
) -> None:
    class _Report:
        def as_dict(self):
            return {"objects_closed": 0}

    monkeypatch.setattr(
        "topos.features.lifecycle.blackhole_rebuild.rebuild_for_blackhole",
        lambda *_a, **_k: _Report(),
    )

    result = await handle_signal_blackhole_entity(
        {"id": "req-1", "type": "signal_blackhole_entity", "payload": {"entity_id": "ent-bh"}}
    )

    assert result["status"] == "ok"
    assert result["payload"]["already_blackholed"] is False
    assert BlackholeStore(blackhole_conn).is_blackholed("ent-bh") is True
    assert _loop_sites(loop_gate_warnings) == []


@pytest.mark.asyncio
async def test_http_blackhole_routes_do_not_gate_on_the_loop(
    blackhole_conn, loop_gate_warnings, monkeypatch
) -> None:
    from topos.api.signal import EntityBlackholeBody, blackhole_entity, unblackhole_entity

    class _Report:
        def as_dict(self):
            return {"objects_closed": 0}

    monkeypatch.setattr(
        "topos.features.lifecycle.blackhole_rebuild.rebuild_for_blackhole",
        lambda *_a, **_k: _Report(),
    )

    flagged = await blackhole_entity("ent-bh", EntityBlackholeBody(), _api_key="test")
    assert flagged["already_blackholed"] is False
    assert BlackholeStore(blackhole_conn).is_blackholed("ent-bh") is True
    assert _loop_sites(loop_gate_warnings) == []

    lifted = await unblackhole_entity("ent-bh", _api_key="test")
    assert lifted["removed"] is True
    assert _loop_sites(loop_gate_warnings) == []


@pytest.mark.asyncio
async def test_unblackhole_handler_does_not_stall_the_loop_while_gate_is_held(
    blackhole_conn,
) -> None:
    """If the write hops back onto the loop, a held gate freezes /healthcheck."""
    await asyncio.to_thread(
        BlackholeStore(blackhole_conn).blackhole_entity, entity_ref="ent-bh"
    )

    holding = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with write_gate.with_db_write():
            holding.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=holder)
    thread.start()
    assert holding.wait(timeout=5)

    ticks = {"n": 0}

    async def heartbeat() -> None:
        while True:
            ticks["n"] += 1
            await asyncio.sleep(0.02)

    hb = asyncio.get_running_loop().create_task(heartbeat())
    worker = asyncio.get_running_loop().create_task(
        handle_signal_unblackhole_entity(
            {
                "id": "req-1",
                "type": "signal_unblackhole_entity",
                "payload": {"entity_id": "ent-bh"},
            }
        )
    )
    try:
        await asyncio.sleep(0.6)
        assert not worker.done(), "unblackhole should still be waiting on the write gate"
        assert ticks["n"] >= 10, "event loop stalled — write is back on the loop thread"
        release.set()
        result = await asyncio.wait_for(worker, timeout=10)
        assert result["status"] == "ok"
    finally:
        hb.cancel()
        release.set()
        thread.join(timeout=5)
