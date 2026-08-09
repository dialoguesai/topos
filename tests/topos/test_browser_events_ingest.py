from __future__ import annotations

import json
import sqlite3

import pytest

from topos.ingestion.ingest_helpers import ingest_ui_payload
from topos.ingestion.log_preview import field_preview


def test_field_preview_handles_non_string_content() -> None:
    assert field_preview({"x": 1, "y": 2}) == json.dumps({"x": 1, "y": 2})
    assert field_preview(None) == ""
    assert field_preview("hello world") == "hello world"
    assert field_preview("x" * 200, max_len=10) == "x" * 10


@pytest.mark.asyncio
async def test_browser_events_dict_content_writes_flat_table(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "browser_events.db"
    # The direct-ingest DB stretch runs on a worker thread (asyncio.to_thread),
    # so the injected connection must allow cross-thread use.
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)

    result = await ingest_ui_payload(
        dataset_id="user-1:default:device1",
        schema_id="browser.events.v1",
        payload={
            "event_type": "click",
            "url": "https://example.com/page",
            "visited_at": "2026-05-29T16:40:00.000Z",
            "title": "Example",
            "content": {"x": 10, "y": 20, "element": "button.primary"},
        },
        source_id="browser_events",
    )

    assert result["status"] == "ok"
    row = conn.execute(
        "SELECT record_id, dataset_id, event_type, url, content FROM browser_events"
    ).fetchone()
    assert row is not None
    assert row["dataset_id"] == "user-1:default:device1"
    assert row["event_type"] == "click"
    assert row["url"] == "https://example.com/page"
    assert json.loads(row["content"]) == {"x": 10, "y": 20, "element": "button.primary"}
    conn.close()
