from __future__ import annotations

import json
import sqlite3

import pytest

from topos.data_explorer_table_prefs import (
    build_table_prefs_config_key,
    delete_table_prefs,
    get_table_prefs,
    normalize_table_prefs_payload,
    put_table_prefs,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        """
        CREATE TABLE engine_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    connection.commit()
    return connection


def test_put_and_get_table_prefs_round_trip(conn: sqlite3.Connection):
    sample = {
        "columnWidths": {"id": 120, "content": 240},
        "sort": {"columnId": "id", "direction": "asc"},
    }
    saved = put_table_prefs(conn, user_id="user-1", table_name="messages", prefs=sample)
    assert saved["columnWidths"]["id"] == 120
    loaded = get_table_prefs(conn, user_id="user-1", table_name="messages")
    assert loaded is not None
    assert loaded["sort"]["columnId"] == "id"


def test_delete_table_prefs(conn: sqlite3.Connection):
    put_table_prefs(conn, user_id="user-1", table_name="messages", prefs={"columnWidths": {"id": 1}})
    assert delete_table_prefs(conn, user_id="user-1", table_name="messages") is True
    assert get_table_prefs(conn, user_id="user-1", table_name="messages") is None


def test_normalize_table_prefs_payload_rejects_oversized():
    huge_order = [f"column_{i}" for i in range(5000)]
    with pytest.raises(ValueError, match="PREFS_TOO_LARGE"):
        normalize_table_prefs_payload({"columnWidths": {"id": 1}, "columnOrder": huge_order})


def test_build_table_prefs_config_key():
    assert build_table_prefs_config_key("user-1", "messages") == "data_explorer_table_prefs:v1:user-1:messages"


def test_put_table_prefs_persists_json(conn: sqlite3.Connection):
    put_table_prefs(conn, user_id="user-1", table_name="events", prefs={"columnWidths": {"payload": 300}})
    key = build_table_prefs_config_key("user-1", "events")
    row = conn.execute("SELECT value FROM engine_config WHERE key = ?", (key,)).fetchone()
    assert row is not None
    parsed = json.loads(row["value"])
    assert parsed["version"] == 1
    assert parsed["columnWidths"]["payload"] == 300
