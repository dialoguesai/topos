"""Owner permission state is never served or cleared through the explorer surfaces.

The legacy inspection handlers answer a non-owner principal whenever no black
hole is active, so the owner's protection clock, attestations and ingest
provenance would otherwise disclose which records are Off-limits and which facts
were excluded, keyed by subject and predicate.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from contextlib import contextmanager

from topos.core.handlers import handle_control_plane_request
from topos.data_explorer_tables import is_permission_state_table
from topos.principal import OWNER_APP, Principal, reset_principal, set_principal


@contextmanager
def owner():
    token = set_principal(Principal(cls=OWNER_APP, channel="uds", acting_user="owner-1"))
    try:
        yield
    finally:
        reset_principal(token)

STATE_TABLES = ("permissions_v2_protection_state", "permissions_v2_protection_events", "ingest_provenance_records")


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "permission_state.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE browser_visits (id TEXT PRIMARY KEY, visited_at TEXT, url TEXT)")
        conn.execute("INSERT INTO browser_visits VALUES ('a','2024-01-01T00:00:00Z','https://a.example')")
        conn.execute("CREATE TABLE permissions_v2_protection_state (singleton INTEGER PRIMARY KEY, clock_id TEXT, generation INTEGER)")
        conn.execute("INSERT INTO permissions_v2_protection_state VALUES (1,'"+"a"*64+"',7)")
        conn.execute("CREATE TABLE permissions_v2_protection_events (sequence INTEGER PRIMARY KEY, generation INTEGER, source TEXT, artifact_key TEXT)")
        conn.execute("INSERT INTO permissions_v2_protection_events VALUES (1,1,'intelligence_exclusions','fact|self:prefers:secret label')")
        conn.execute("CREATE TABLE ingest_provenance_records (message_id TEXT PRIMARY KEY, attestation TEXT)")
        conn.execute("INSERT INTO ingest_provenance_records VALUES ('m1','owner attested')")
        conn.commit()
    return path


@pytest.fixture
def explorer(tmp_path, monkeypatch):
    path = _database(tmp_path)
    for module in ("topos.config.settings", "topos.core.handlers", "topos.core.state"):
        target = __import__(module, fromlist=["settings"]).settings
        monkeypatch.setattr(target, "engine_pool_mode", "off", raising=False)
        monkeypatch.setattr(target, "database_mode", "local", raising=False)
        monkeypatch.setattr(target, "database_path", str(path), raising=False)
    return path


def test_prefixes_cover_permission_state_and_nothing_else():
    for name in STATE_TABLES:
        assert is_permission_state_table(name)
    for name in ("conversation_messages", "signal_objects", "entities", "owner_only_records", "", "permissions_v2"):
        assert not is_permission_state_table(name)


@pytest.mark.asyncio
@pytest.mark.parametrize("table", STATE_TABLES)
@pytest.mark.parametrize("message_type", ["get_table_rows", "get_table_count", "get_table_schema"])
async def test_permission_state_rows_counts_and_columns_are_refused(explorer, message_type, table):
    result = await handle_control_plane_request({"id": "probe", "type": message_type, "payload": {"table_name": table}})
    assert result["status"] == "error", result
    body = str(result)
    assert "prefers" not in body and "owner attested" not in body and "generation" not in body


@pytest.mark.asyncio
async def test_permission_state_tables_are_not_listed(explorer):
    result = await handle_control_plane_request({"id": "probe", "type": "list_database_tables", "payload": {}})
    assert result["status"] == "ok", result

    def names(value):
        if isinstance(value, dict):
            for key in ("name", "table_name"):
                if isinstance(value.get(key), str):
                    yield value[key]
            for nested in value.values():
                yield from names(nested)
        elif isinstance(value, list):
            for item in value:
                yield from names(item)
        elif isinstance(value, str):
            yield value

    listed = set(names(result["payload"]))
    assert "browser_visits" in listed, sorted(listed)
    assert not any(is_permission_state_table(name) for name in listed), sorted(listed)


@pytest.mark.asyncio
@pytest.mark.parametrize("table", STATE_TABLES)
@pytest.mark.parametrize("action", ["clear", "drop"])
async def test_permission_state_tables_cannot_be_cleared_or_dropped(explorer, table, action):
    # Even the owner clears these only through the permissions lanes, never here:
    # dropping the clock's event log silently revives stale reviews (audit F07).
    with owner():
        result = await handle_control_plane_request(
            {"id": "probe", "type": "delete_database_table", "payload": {"table_name": table, "action": action}})
    assert result["status"] == "error" and "protected from deletion" in result["error"]
    with sqlite3.connect(explorer) as conn:
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("table", STATE_TABLES)
async def test_permission_state_rows_cannot_be_deleted(explorer, table):
    with owner():
        result = await handle_control_plane_request(
            {"id": "probe", "type": "delete_database_rows", "payload": {"table_name": table, "row_ids": ["1"]}})
    assert result["status"] == "error" and "hidden" in result["error"].lower()
