from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from topos.core.handlers import _table_row_order_clause, handle_control_plane_request


def test_table_row_order_clause_prefers_temporal_columns():
    clause = _table_row_order_clause({"visited_at", "url", "id"}, table_name="browser_visits")
    assert clause == '"visited_at" DESC'


def test_table_row_order_clause_falls_back_to_rowid():
    clause = _table_row_order_clause({"id", "content"}, table_name="notes")
    assert clause == "rowid DESC"


@pytest.mark.asyncio
async def test_get_table_rows_orders_newest_first_by_time_column(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "ordered_rows.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE browser_visits (
                id TEXT PRIMARY KEY,
                visited_at TEXT,
                url TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO browser_visits (id, visited_at, url) VALUES (?, ?, ?)",
            [
                ("old", "2024-01-01T00:00:00Z", "https://old.example"),
                ("mid", "2024-06-01T00:00:00Z", "https://mid.example"),
                ("new", "2025-01-01T00:00:00Z", "https://new.example"),
            ],
        )
        conn.commit()
    finally:
        conn.close()

    config_settings = __import__("topos.config.settings", fromlist=["settings"]).settings
    handlers_settings = __import__("topos.core.handlers", fromlist=["settings"]).settings
    state_settings = __import__("topos.core.state", fromlist=["settings"]).settings
    for target in (config_settings, handlers_settings, state_settings):
        monkeypatch.setattr(target, "engine_pool_mode", "off", raising=False)
        monkeypatch.setattr(target, "database_mode", "local", raising=False)
        monkeypatch.setattr(target, "database_path", str(db_path), raising=False)

    result = await handle_control_plane_request(
        {
            "id": "ordered-rows",
            "type": "get_table_rows",
            "payload": {"table_name": "browser_visits", "limit": 2, "offset": 0},
        }
    )

    assert result["status"] == "ok"
    ids = [row["id"] for row in result["payload"]["rows"]]
    assert ids == ["new", "mid"]
