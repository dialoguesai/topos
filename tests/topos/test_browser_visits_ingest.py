from __future__ import annotations

import sqlite3

import pytest

from topos.ingestion.ingest_helpers import ingest_ui_payload
from topos.storage.raw.browser_flat_tables import (
    RAW_BROWSER_VISITS_TABLE,
    backfill_browser_visits_from_raw_retention,
)
from topos.storage.raw.raw_tables_manager import RawTablesManager


@pytest.mark.asyncio
async def test_browser_visits_writes_flat_table(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "browser_visits.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)

    result = await ingest_ui_payload(
        dataset_id="user-1:default:device1",
        schema_id="browser.visits.v1",
        payload={
            "url": "https://example.com/docs",
            "visited_at": "2026-05-31T20:38:56.765Z",
            "title": "Example Docs",
            "hostname": "example.com",
            "device_name": "My Laptop",
        },
        source_id="browser_visits",
    )

    assert result["status"] == "ok"
    row = conn.execute(
        "SELECT record_id, dataset_id, url, title, hostname FROM browser_visits"
    ).fetchone()
    assert row is not None
    assert row["dataset_id"] == "user-1:default:device1"
    assert row["url"] == "https://example.com/docs"
    assert row["title"] == "Example Docs"
    assert row["hostname"] == "example.com"
    # Since cb9172f, browser_* ui_stream ingest retains raw payloads in the
    # generic raw_<source>_ui_stream table (payload_json), not the legacy flat
    # RAW_BROWSER_VISITS_TABLE (which backfill still reads for old databases).
    raw_count = conn.execute("SELECT COUNT(*) FROM raw_browservisits_ui_stream").fetchone()[0]
    assert raw_count == 1
    conn.close()


def test_backfill_browser_visits_from_raw_retention(tmp_path) -> None:
    db_path = tmp_path / "backfill.db"
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    raw_manager = RawTablesManager(conn)
    raw_manager.write_raw_record(
        source_id="browser_visits",
        source_record_id="visit-1",
        payload={
            "record_id": "visit-1",
            "dataset_id": "user-1:default",
            "url": "https://mail.google.com/mail/u/0/",
            "visited_at": "2026-05-31T20:37:25.114Z",
            "title": "Gmail",
            "hostname": "mail.google.com",
        },
        source_type="chat_messages",
    )

    copied = backfill_browser_visits_from_raw_retention(conn)
    assert copied == 1
    row = conn.execute("SELECT url, hostname FROM browser_visits").fetchone()
    assert row is not None
    assert row["url"] == "https://mail.google.com/mail/u/0/"
    assert row["hostname"] == "mail.google.com"
    assert backfill_browser_visits_from_raw_retention(conn) == 0
    conn.close()
