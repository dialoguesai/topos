"""ui_stream install + ingest for journal.time_log.v1 via registry path only."""

from __future__ import annotations

import sqlite3

import pytest

from topos.core import state as core_state
from topos.core.handlers import handle_control_plane_request
from topos.ingestion.ingest_helpers import _ingest_ui_payload_direct, _ui_stream_passes_payload_through
from topos.sources.registry import REGISTRY
from topos.sources.runtime_install import install_source_definition
from topos.storage.db.migrations import apply_all_migrations

TIME_LOG_SOURCE_DEF = {
    "source_id": "time_log",
    "display_name": "Time Log",
    "source_type": "ui_stream",
    "schema_id": "journal.time_log.v1",
    "parser_id": "journal.time_log.v1",
    "canonical_group_id": "journal",
    "ingestion_trigger": "automatic",
    "enrichment_trigger": "manual",
    "default_scope_id": "health",
    "allowed_scope_ids": ["health:read"],
    "pipeline_include_data_table": True,
    "tables": [
        {
            "table_id": "time_log_sessions",
            "display_name": "Time Log Sessions",
            "columns": [
                {"name": "record_id", "type": "text", "primary_key": True},
                {"name": "entry_at", "type": "text"},
                {"name": "starts_at", "type": "text"},
                {"name": "ends_at", "type": "text"},
                {"name": "duration", "type": "text"},
                {"name": "project", "type": "text"},
                {"name": "goal", "type": "text"},
                {"name": "accomplished", "type": "text"},
                {"name": "completed", "type": "integer"},
                {"name": "location", "type": "text"},
                {"name": "group", "type": "text"},
                {"name": "source_id", "type": "text"},
            ],
        }
    ],
}


@pytest.fixture
def migrated_conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "time_log.db"))
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def installed_time_log():
    handle = install_source_definition(TIME_LOG_SOURCE_DEF)
    try:
        yield handle
    finally:
        handle.uninstall()
        assert "time_log" not in REGISTRY


@pytest.mark.asyncio
async def test_ui_stream_ingest_writes_raw_table_and_journal_entry(
    migrated_conn, monkeypatch, installed_time_log
) -> None:
    monkeypatch.setattr(core_state, "get_db_connection", lambda: migrated_conn)

    payload = {
        "startDate": "2026-06-23",
        "startTime": "09:00 AM",
        "endDate": "2026-06-23",
        "endTime": "10:00 AM",
        "duration": 60,
        "project": "Topos",
        "goal": "Registry-only ingest",
        "accomplished": "Submitted first ui_stream row.",
        "completed": True,
        "location": "Home",
        "group": "Solo",
    }

    result = await _ingest_ui_payload_direct(
        dataset_id="user:default:device",
        schema_id="journal.time_log.v1",
        payload=payload,
        job_id="job-time-log-1",
        source_id="time_log",
    )

    assert result["status"] == "ok"
    assert result["records_processed"] == 1
    assert result["errors_count"] == 0

    journal = migrated_conn.execute(
        "SELECT entry_id, starts_at, ends_at, category, content, place_name FROM journal_entries"
    ).fetchone()
    assert journal is not None
    assert journal["starts_at"] == "2026-06-23T09:00:00"
    assert journal["ends_at"] == "2026-06-23T10:00:00"
    assert journal["category"] == "Topos"
    assert "Goal: Registry-only ingest" in journal["content"]
    assert journal["place_name"] == "Home"

    session = migrated_conn.execute(
        'SELECT record_id, goal, project, source_id FROM time_log_sessions'
    ).fetchone()
    assert session is not None
    assert session["goal"] == "Registry-only ingest"
    assert session["project"] == "Topos"
    assert session["source_id"] == "time_log"


def test_ui_stream_passes_payload_through_for_journal_time_log(installed_time_log) -> None:
    source = REGISTRY["time_log"]
    assert _ui_stream_passes_payload_through(source, "time_log") is True


@pytest.mark.asyncio
async def test_app_ingest_fails_when_source_not_installed() -> None:
    result = await handle_control_plane_request(
        {
            "id": "req-app-ingest-missing-source",
            "type": "app_ingest",
            "payload": {
                "user_id": "user-1",
                "dataset_id": "user-1:default:device1",
                "source_id": "my_uninstalled_stream",
                "schema_id": "journal.time_log.v1",
                "records": [{"startDate": "2026-06-23", "goal": "Should fail"}],
            },
        }
    )

    assert result["status"] == "error"
    assert "not installed" in result.get("error", "").lower()
