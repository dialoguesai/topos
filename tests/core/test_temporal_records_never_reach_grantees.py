"""No grantee read carries the separated temporal records, whatever the grant's filters.

``event_time_json`` holds a native time at full precision. A grant that turns
``event_at`` into a date, or blocks ``event_at`` outright, does not reach into
that JSON, and no grantee capability evaluates it yet. So every grantee row
reader strips both record columns for every grant. Tested through the actual
SQL readers, with and without a saved filter manifest.
"""
import json

import pytest

from tests.core.test_permission_read_authority import (DISCLOSED, RESOURCE, call, client, conn, make_messages,
    seed)
from topos.core.handlers import uma
from topos.features.temporal.records import event_time

EXACT = "2026-09-15T21:11:24.123456+00:00"
CANARY = "temporal-record-canary"

GRANTS = {
    "no filters": None,
    "timestamp to date": {"filter_manifest": {"filters": [{"filter_id": "timestamp_to_date", "params": {}}]}},
    "event_at blocked": {"filter_manifest": {"filters": [{"filter_id": "column_blocklist", "params": {"fields": ["event_at", "ts"]}}]}},
}


def seed_with_records(conn):
    make_messages(conn)
    conn.execute("ALTER TABLE conversation_messages ADD COLUMN event_time_json TEXT")
    conn.execute("ALTER TABLE conversation_messages ADD COLUMN temporal_json TEXT")
    seed(conn, "conversation_messages", "approved", EXACT)
    # The shared harness table also has `ts` and `content_rendered`; the real
    # conversation_messages table has neither, so they are cleared to keep the
    # full-precision check about what a real row would release.
    conn.execute("UPDATE conversation_messages SET ts=NULL, content_rendered=NULL, event_time_json=?, temporal_json=? "
                 "WHERE message_id='approved'",
                 (event_time(EXACT, provenance="native_source_clock").to_json(), json.dumps({"canary": CANARY})))


def assert_no_record(rows, text):
    assert rows, text
    assert all("event_time_json" not in row and "temporal_json" not in row for row in rows)
    assert CANARY not in text and "native_source_clock" not in text


@pytest.mark.parametrize("filters", GRANTS.values(), ids=GRANTS.keys())
def test_the_generic_rows_reader_never_returns_them(conn, filters):
    seed_with_records(conn)
    result = call(uma.handle_uma_get_rows, filters=filters, table_name="conversation_messages",
                  allowed_tables=["conversation_messages"])
    assert result["status"] == "ok", result
    rows = result["payload"]["rows"]
    assert rows[0]["content"] == DISCLOSED
    assert_no_record(rows, json.dumps(result["payload"]))
    if filters is GRANTS["timestamp to date"]:
        assert rows[0]["event_at"] == "2026-09-15"
        assert "21:11:24" not in json.dumps(result["payload"])


@pytest.mark.parametrize("filters", GRANTS.values(), ids=GRANTS.keys())
def test_the_messages_reader_never_returns_them(conn, filters):
    seed_with_records(conn)
    result = call(uma.handle_uma_get_messages, filters=filters)
    assert result["status"] == "ok", result
    assert_no_record(result["payload"]["messages"], json.dumps(result["payload"]))


@pytest.mark.parametrize("filters", GRANTS.values(), ids=GRANTS.keys())
def test_the_http_messages_route_never_returns_them(conn, monkeypatch, filters):
    seed_with_records(conn)
    response = client(monkeypatch, ["messages:read"], filters).get(f"/v1/uma/resources/{RESOURCE}/data/messages")
    assert response.status_code == 200, response.text
    assert_no_record(response.json()["messages"], response.text)
    if filters is GRANTS["timestamp to date"]:
        assert "21:11:24" not in response.text
