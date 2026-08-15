"""File ingest runs the same post-canonical privacy hook as ui_stream ingest."""

from __future__ import annotations

import sqlite3

import pytest

from topos.core import state as core_state
from topos.ingestion.manager import IngestionManager
from topos.ingestion.triggers.file_trigger import FileTrigger
from topos.sources.registry import REGISTRY
from topos.sources.runtime_install import install_source_definition
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.raw.file_store import RawFileStore

TIME_LOG_FILE_DEF = {
    "source_id": "time_log",
    "display_name": "Time Log",
    "source_type": "file",
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
            "display_name": "Sessions",
            "columns": [
                {"name": "record_id", "type": "text", "primary_key": True},
                {"name": "goal", "type": "text"},
                {"name": "source_id", "type": "text"},
            ],
        }
    ],
}


@pytest.fixture
def migrated_conn(tmp_path, pin_db_path):
    # The ingest DB stretch runs on a worker thread (asyncio.to_thread),
    # so the injected connection must allow cross-thread use.
    db_file = tmp_path / "file_ingest_privacy.db"
    # Settings must name the SAME file the injected connection uses;
    # AdapterFactory resolves by path, not by the patched accessor.
    pin_db_path(db_file)
    conn = sqlite3.connect(str(db_file), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def installed_time_log_file():
    handle = install_source_definition(TIME_LOG_FILE_DEF)
    try:
        yield handle
    finally:
        handle.uninstall()
        assert "time_log" not in REGISTRY


@pytest.mark.asyncio
async def test_time_log_file_ingest_runs_privacy_hook(
    migrated_conn, tmp_path, monkeypatch, installed_time_log_file
) -> None:
    privacy_calls: list[list[dict]] = []

    async def capture_privacy(conn, messages, **kwargs):
        privacy_calls.append(list(messages))
        return {"records_updated": len(messages), "nsfw_tagged": 0}

    async def fake_signal_derivation(self, *args, **kwargs):
        return {"jobs_run": 0, "records_created": {}, "errors": [], "deferred_jobs": []}

    async def fake_run_canonical(self, *args, **kwargs):
        return {"jobs_run": 0, "records_created": {}, "errors": []}

    monkeypatch.setattr(core_state, "get_db_connection", lambda: migrated_conn)
    monkeypatch.setattr(
        "topos.disclosure.privacy_layer.run_privacy_disclosure_layer",
        capture_privacy,
    )
    monkeypatch.setattr(
        "topos.enrichment.orchestrator.SignalDerivationOrchestrator.run_signal_derivation",
        fake_signal_derivation,
    )
    monkeypatch.setattr(
        "topos.enrichment.orchestrator.EnrichmentOrchestrator.run_canonical",
        fake_run_canonical,
    )

    csv_body = (
        "num,startDate,startTime,endDate,endTime,duration,project,goal,accomplished,completed,location,group\n"
        "99,2026-05-01,8:00 AM,2026-05-01,8:55 AM,55,Topos,Ship time-log source,Parser wired.,TRUE,Home,Solo\n"
    ).encode("utf-8")

    file_store = RawFileStore(base_path=tmp_path)
    trigger = FileTrigger(file_store=file_store)
    job = trigger.create_job_from_bytes(
        job_id="time-log-file-privacy",
        dataset_id="user:default:device",
        schema_id="journal.time_log.v1",
        payload=csv_body,
        file_format="csv",
    )

    manager = IngestionManager(file_store=file_store)
    result = await manager.process_job(job, source_id="time_log")

    assert result["records_processed"] == 1
    assert privacy_calls, "expected file ingest to invoke privacy disclosure layer"
    assert privacy_calls[0][0].get("message_id") == "tl-99"
    assert "Goal: Ship time-log source" in str(privacy_calls[0][0].get("content") or "")

    row = migrated_conn.execute(
        "SELECT entry_id, content FROM journal_entries WHERE entry_id='tl-99'"
    ).fetchone()
    assert row is not None


@pytest.mark.asyncio
async def test_manager_delegates_post_canonical_pipeline(
    migrated_conn, tmp_path, monkeypatch, installed_time_log_file
) -> None:
    pipeline_calls: list[dict] = []

    async def capture_post_canonical(**kwargs):
        pipeline_calls.append(kwargs)
        return {
            "privacy_disclosure_layer": {"records_updated": 1},
            "canonical_enrichment": {"jobs_run": 0, "records_created": {}, "errors": []},
            "signal_derivation": {"jobs_run": 0, "records_created": {}, "errors": []},
        }

    monkeypatch.setattr(core_state, "get_db_connection", lambda: migrated_conn)
    monkeypatch.setattr(
        "topos.ingestion.canonical_pipeline.run_post_canonical_pipeline",
        capture_post_canonical,
    )

    csv_body = (
        "num,startDate,startTime,endDate,endTime,duration,project,goal,accomplished,completed,location,group\n"
        "1,2026-05-01,8:00 AM,2026-05-01,8:55 AM,55,Topos,Goal text,Done.,TRUE,Home,Solo\n"
    ).encode("utf-8")

    file_store = RawFileStore(base_path=tmp_path)
    trigger = FileTrigger(file_store=file_store)
    job = trigger.create_job_from_bytes(
        job_id="time-log-file-post-canonical",
        dataset_id="user:default:device",
        schema_id="journal.time_log.v1",
        payload=csv_body,
        file_format="csv",
    )

    manager = IngestionManager(file_store=file_store)
    result = await manager.process_job(job, source_id="time_log")

    assert result["records_processed"] == 1
    assert pipeline_calls
    assert pipeline_calls[0]["source_def"].source_id == "time_log"
    assert pipeline_calls[0]["canonical_records"]
