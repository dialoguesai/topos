"""``db_ok`` — healthcheck must say whether the database is actually readable.

``/healthcheck`` returned ``{"status": "ok"}`` without touching SQLite, so a node
whose database handle was dead was indistinguishable from a working one: the app
drew a connected Topos over an empty graph for nearly two hours on 2026-08-17 and
nothing in the protocol could have told it otherwise.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

import topos.core.handlers as hub
from topos.core.db_health import probe_db_health
from topos.core.handlers.device import handle_healthcheck


class _PoisonedConnection:
    """Statement-cache corruption: every execute raises ``KeyError((sql,))``."""

    def execute(self, sql, *args, **kwargs):
        raise KeyError(("\n        SELECT id FROM routines\n        WHERE id = ?\n        ",))


@pytest.mark.asyncio
async def test_probe_reports_ok_on_a_working_database(monkeypatch, tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"), check_same_thread=False)
    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)

    db_ok, db_error = await probe_db_health()
    assert db_ok is True
    assert db_error is None


@pytest.mark.asyncio
async def test_probe_reports_unknown_when_no_database_is_configured(monkeypatch):
    monkeypatch.setattr(hub, "get_db_connection", lambda: None)

    db_ok, db_error = await probe_db_health()
    # None, not False: "no database here" must not accuse a healthy node.
    assert db_ok is None
    assert db_error is None


@pytest.mark.asyncio
async def test_probe_reports_a_corrupted_connection_without_quoting_its_sql(monkeypatch):
    monkeypatch.setattr(hub, "get_db_connection", lambda: _PoisonedConnection())

    db_ok, db_error = await probe_db_health()
    assert db_ok is False
    # The KeyError's payload is an unrelated SELECT. Surfacing it verbatim is how
    # this failure got read as a routines bug for two hours.
    assert "routines" not in (db_error or "")
    assert "statement cache" in (db_error or "")


@pytest.mark.asyncio
async def test_probe_runs_off_the_event_loop(monkeypatch):
    """A probe on the loop would stall every coroutine — including the keepalive."""
    seen: dict[str, int] = {}

    class _RecordingConn:
        def execute(self, sql, *args, **kwargs):
            seen["thread"] = threading.get_ident()

            class _Cur:
                def fetchone(self_inner):
                    return (1,)

            return _Cur()

    monkeypatch.setattr(hub, "get_db_connection", lambda: _RecordingConn())

    await probe_db_health()
    assert seen["thread"] != threading.get_ident()


@pytest.mark.asyncio
async def test_ws_healthcheck_carries_the_database_verdict(monkeypatch):
    """The control plane forwards its /healthcheck route to THIS handler."""
    monkeypatch.setattr(hub, "get_db_connection", lambda: _PoisonedConnection())

    reply = await handle_healthcheck({"id": "req-1", "type": "healthcheck"})
    assert reply["id"] == "req-1"
    # Still ok: reaching this handler proves the event loop is alive, and the
    # reconnect logic keys on that. The database is a separate axis.
    assert reply["status"] == "ok"
    assert reply["payload"]["status"] == "ok"
    assert reply["payload"]["db_ok"] is False
    assert reply["payload"]["db_error"]


@pytest.mark.asyncio
async def test_ws_healthcheck_stays_quiet_when_the_database_is_fine(monkeypatch, tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"), check_same_thread=False)
    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)

    reply = await handle_healthcheck({"id": "req-2", "type": "healthcheck"})
    assert reply["payload"]["db_ok"] is True
    assert "db_error" not in reply["payload"]


@pytest.mark.asyncio
async def test_a_slow_probe_reports_unknown_rather_than_accusing_the_node(monkeypatch):
    """A saturated thread pool must not be reported as a broken database.

    `healthcheck` is on the control plane's fast-inbound path so it answers while
    the node is busy, and this probe queues on the same pool as enrichment work.
    Reporting `False` here would put a red "data unavailable" dot on a node that
    is merely loaded — the same class of lie the field exists to remove.
    """
    import topos.core.db_health as db_health

    class _SlowConn:
        def execute(self, sql, *args, **kwargs):
            import time

            time.sleep(0.5)
            raise AssertionError("probe should have been abandoned before this")

    monkeypatch.setattr(hub, "get_db_connection", lambda: _SlowConn())
    monkeypatch.setattr(db_health, "_PROBE_TIMEOUT_S", 0.05)

    db_ok, db_error = await db_health.probe_db_health()
    assert db_ok is None
    assert db_error is None
