"""The query path's two source reads take no write gate and run on the loop without waiting.

``list_installed_source_ids``, ``get_data_health_version`` and packet resolution's
``active_elevation`` run inside ``_execute_turn`` on the app event loop, once per query. Until
21 Oct 2026 the first called ``ensure_install_schema()`` (CREATE TABLE, CREATE INDEX and a
scope-key migration under the write gate), the second ran ``CREATE TABLE IF NOT EXISTS`` ungated,
and the third created the MCP client tables under the gate. With the gate held 60 to 109 s by the
review pass, every query froze the loop at the first one, 111 times before noon. Pinned here: none
of the three enters a gate section, each answers while another thread holds the gate, and a
database without the table means no installs, no generations and no elevation.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime, timezone

import pytest

from topos.mcp_clients import active_elevation, decide_elevation, mint_client_token, request_elevation
from topos.query.source_generation import (
    bump_source_generation,
    get_data_health_version,
    list_installed_source_ids,
)
from topos.sources import install_service
from topos.storage.db.write_gate import db_write_lock, set_section_observer


@pytest.fixture
def conn():
    handle = sqlite3.connect(":memory:", check_same_thread=False)
    try:
        yield handle
    finally:
        handle.close()


@pytest.fixture
def gate_sections():
    """Every gate section entered while the fixture is live: (site, caller)."""
    seen: list = []
    set_section_observer(lambda site, caller, waited, held, nested: seen.append((site, caller)))
    try:
        yield seen
    finally:
        set_section_observer(None)


def _install(conn, source_id: str, *, status: str = "installed", active: int = 1) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        f"INSERT INTO {install_service.INSTALL_TABLE} (install_id, scope_key, source_id, status, is_active, "
        "source_definition_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, '{}', ?, ?)",
        (f"inst-{source_id}-{status}-{active}", "u1|t1|d1|dev1", source_id, status, active, now, now),
    )
    conn.commit()


def test_no_install_table_means_no_installs_and_no_gate(conn, gate_sections):
    assert list_installed_source_ids(conn) == []
    assert gate_sections == []
    assert list_installed_source_ids(None) == []


def test_installed_ids_are_read_without_a_gate_section(conn, gate_sections, monkeypatch):
    monkeypatch.setattr(install_service, "get_db_connection", lambda: conn)
    install_service.ensure_install_schema()  # the writer creates the schema, under the gate
    _install(conn, "fitness")
    _install(conn, "calendar", status="ready")
    _install(conn, "stale", status="installed", active=0)
    _install(conn, "failed", status="failed")
    gate_sections.clear()
    assert sorted(list_installed_source_ids(conn)) == ["calendar", "fitness"]
    assert gate_sections == []


def _while_gate_is_held(fn):
    """``fn()`` while another thread holds the write gate: its result and how long it took."""
    held, release = threading.Event(), threading.Event()

    def hold():
        with db_write_lock():
            held.set()
            release.wait(10.0)

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    assert held.wait(5.0)
    try:
        before = time.perf_counter()
        result = fn()
        return result, time.perf_counter() - before
    finally:
        release.set()
        holder.join(5.0)


def test_installed_ids_answer_while_another_thread_holds_the_gate(conn, monkeypatch):
    monkeypatch.setattr(install_service, "get_db_connection", lambda: conn)
    install_service.ensure_install_schema()
    _install(conn, "fitness")
    ids, elapsed = _while_gate_is_held(lambda: list_installed_source_ids(conn))
    assert ids == ["fitness"]
    assert elapsed < 0.2, f"the read waited {elapsed:.2f}s on the write gate"


def test_no_client_tables_means_no_elevation_and_no_gate(conn, gate_sections, caplog):
    assert active_elevation(conn, client_id="partner-x", scope_id="health:read") is None
    assert gate_sections == []
    assert not [r for r in caplog.records if "active_elevation lookup failed" in r.getMessage()]


def test_an_approved_elevation_is_read_without_a_gate_section(conn, gate_sections):
    mint_client_token(conn, client_id="partner-x")  # the writers create the schema, under the gate
    row = request_elevation(conn, client_id="partner-x", scope_id="health:read")
    decide_elevation(conn, request_id=row["id"], approve=True)
    gate_sections.clear()
    grant, elapsed = _while_gate_is_held(
        lambda: active_elevation(conn, client_id="partner-x", scope_id="health:read")
    )
    assert grant is not None and grant["status"] == "approved"
    assert gate_sections == []
    assert elapsed < 0.2, f"the read waited {elapsed:.2f}s on the write gate"
    assert active_elevation(conn, client_id="partner-x", scope_id="sleep:read") is None


def test_data_health_version_reads_without_creating_the_table(conn, gate_sections):
    missing = get_data_health_version("health:read", ["fitness", "sleep"], conn)
    assert missing == "fitness:0|sleep:0"
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "scope_source_generation" not in tables
    assert gate_sections == []

    bump_source_generation(conn, "fitness", scope_ids=["health:read"])  # the writer creates it
    gate_sections.clear()
    bumped = get_data_health_version("health:read", ["fitness", "sleep"], conn)
    assert bumped == "fitness:1|sleep:0"
    assert gate_sections == []


def test_data_health_version_still_raises_on_a_sick_database():
    sick = sqlite3.connect(":memory:")
    sick.execute("CREATE TABLE scope_source_generation (scope_id TEXT)")  # wrong shape, not missing
    with pytest.raises(sqlite3.OperationalError):
        get_data_health_version("health:read", ["fitness"], sick)
