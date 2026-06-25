"""delete_database_table clears canonical schema tables instead of dropping them."""

from __future__ import annotations

import importlib
import sqlite3

import pytest

pytestmark = pytest.mark.usefixtures("engine_runtime_isolation")


def _set_local_db(monkeypatch: pytest.MonkeyPatch, db_path: str) -> None:
    config_settings = importlib.import_module("topos.config.settings").settings
    handlers_settings = importlib.import_module("topos.core.handlers").settings
    state_settings = importlib.import_module("topos.core.state").settings
    for target in (config_settings, handlers_settings, state_settings):
        monkeypatch.setattr(target, "engine_pool_mode", "off", raising=False)
        monkeypatch.setattr(target, "database_mode", "local", raising=False)
        monkeypatch.setattr(target, "database_path", str(db_path), raising=False)


async def _handle(message: dict) -> dict:
    handlers = importlib.import_module("topos.core.handlers")
    return await handlers.handle_control_plane_request(message)


@pytest.mark.asyncio
async def test_delete_database_table_clears_canonical_table_rows(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "clear_canonical.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE journal_entries (
            entry_id TEXT PRIMARY KEY,
            content TEXT,
            source_id TEXT
        )
        """
    )
    conn.executemany(
        "INSERT INTO journal_entries (entry_id, content, source_id) VALUES (?, ?, ?)",
        [("j-1", "one", "demo_journal_file"), ("j-2", "two", "demo_journal_file")],
    )
    conn.commit()
    conn.close()

    _set_local_db(monkeypatch, str(db_path))
    result = await _handle(
        {
            "id": "clear-journal",
            "type": "delete_database_table",
            "payload": {"table_name": "journal_entries"},
        }
    )
    assert result["status"] == "ok"
    payload = result["payload"]
    assert payload["action"] == "cleared"
    assert payload["rows_deleted"] == 2

    verify = sqlite3.connect(db_path)
    try:
        assert (
            verify.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='journal_entries'").fetchone()[0]
            == 1
        )
        assert verify.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_delete_database_table_clears_non_canonical_table_when_action_clear(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "clear_raw.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE time_log_sessions (record_id TEXT PRIMARY KEY, goal TEXT)")
    conn.execute("INSERT INTO time_log_sessions (record_id, goal) VALUES ('tl-1', 'Ship')")
    conn.commit()
    conn.close()

    _set_local_db(monkeypatch, str(db_path))
    result = await _handle(
        {
            "id": "clear-sessions",
            "type": "delete_database_table",
            "payload": {"table_name": "time_log_sessions", "action": "clear"},
        }
    )
    assert result["status"] == "ok"
    payload = result["payload"]
    assert payload["action"] == "cleared"
    assert payload["rows_deleted"] == 1

    verify = sqlite3.connect(db_path)
    try:
        assert (
            verify.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='time_log_sessions'").fetchone()[0]
            == 1
        )
        assert verify.execute("SELECT COUNT(*) FROM time_log_sessions").fetchone()[0] == 0
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_delete_database_table_rejects_drop_for_canonical_table(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "drop_canonical.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE journal_entries (entry_id TEXT PRIMARY KEY, content TEXT)")
    conn.execute("INSERT INTO journal_entries (entry_id, content) VALUES ('j-1', 'one')")
    conn.commit()
    conn.close()

    _set_local_db(monkeypatch, str(db_path))
    result = await _handle(
        {
            "id": "drop-journal",
            "type": "delete_database_table",
            "payload": {"table_name": "journal_entries", "action": "drop"},
        }
    )
    assert result["status"] == "error"
    assert "cannot be dropped" in str(result.get("error", "")).lower()

    verify = sqlite3.connect(db_path)
    try:
        assert verify.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 1
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_delete_database_table_drops_non_canonical_table(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "drop_raw.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE time_log_sessions (record_id TEXT PRIMARY KEY, goal TEXT)")
    conn.execute("INSERT INTO time_log_sessions (record_id, goal) VALUES ('tl-1', 'Ship')")
    conn.commit()
    conn.close()

    _set_local_db(monkeypatch, str(db_path))
    result = await _handle(
        {
            "id": "drop-sessions",
            "type": "delete_database_table",
            "payload": {"table_name": "time_log_sessions"},
        }
    )
    assert result["status"] == "ok"
    payload = result["payload"]
    assert payload["action"] == "dropped"
    assert payload["dropped_type"] == "table"

    verify = sqlite3.connect(db_path)
    try:
        assert (
            verify.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='time_log_sessions'").fetchone()[0]
            == 0
        )
    finally:
        verify.close()
