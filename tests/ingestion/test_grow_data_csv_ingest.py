"""Verify Grow CSV ingestion via grow_data_file (file) and grow_journal (ui_stream)."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

from topos.core import state as core_state
from topos.ingestion.ingest_helpers import _ingest_ui_payload_direct
from topos.ingestion.manager import IngestionManager
from topos.ingestion.triggers.file_trigger import FileTrigger
from topos.sources.registry import REGISTRY
from topos.sources.runtime_install import install_source_definition
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.raw.file_store import RawFileStore

GROW_CSV = Path(__file__).resolve().parents[3] / "Grow_Data (12).csv"

# The Grow export lives in the owner's workspace root, not the repo — these
# tests validate real-export ingestion locally and must skip elsewhere (CI).
pytestmark = pytest.mark.skipif(
    not GROW_CSV.is_file(), reason=f"Grow export not present: {GROW_CSV}"
)

SESSION_COLUMNS = [
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
]

GROW_DATA_FILE_DEF = {
    "source_id": "grow_data_file",
    "display_name": "Grow Data File",
    "source_type": "file",
    "schema_id": "journal.time_log.v1",
    "parser_id": "journal.time_log.v1",
    "canonical_group_id": "journal",
    "ingestion_trigger": "automatic",
    "enrichment_trigger": "manual",
    "default_scope_id": "health",
    "allowed_scope_ids": ["health:read"],
    "pipeline_include_data_table": True,
    "file_ingest_shape": {"format": "csv", "has_header": True},
    "tables": [
        {
            "table_id": "grow_data_sessions",
            "display_name": "Grow Data Sessions",
            "columns": SESSION_COLUMNS,
        }
    ],
}

GROW_JOURNAL_DEF = {
    "source_id": "grow_journal",
    "display_name": "Grow Journal",
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
            "table_id": "grow_journal_sessions",
            "display_name": "Grow Journal Sessions",
            "columns": SESSION_COLUMNS,
        }
    ],
}


def _load_grow_rows() -> list[dict[str, str]]:
    assert GROW_CSV.is_file(), f"Missing Grow export: {GROW_CSV}"
    with GROW_CSV.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture
def migrated_conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "grow_ingest.db"))
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)
    yield conn
    conn.close()


@pytest.fixture
def stub_post_canonical(monkeypatch):
    async def fake_signal_derivation(self, *args, **kwargs):
        return {"jobs_run": 0, "records_created": {}, "errors": [], "deferred_jobs": []}

    async def fake_run_canonical(self, *args, **kwargs):
        return {"jobs_run": 0, "records_created": {}, "errors": []}

    async def fake_privacy(conn, messages, **kwargs):
        return {"records_updated": len(messages), "nsfw_tagged": 0}

    monkeypatch.setattr(
        "topos.enrichment.orchestrator.SignalDerivationOrchestrator.run_signal_derivation",
        fake_signal_derivation,
    )
    monkeypatch.setattr(
        "topos.enrichment.orchestrator.EnrichmentOrchestrator.run_canonical",
        fake_run_canonical,
    )
    monkeypatch.setattr(
        "topos.disclosure.privacy_layer.run_privacy_disclosure_layer",
        fake_privacy,
    )


@pytest.fixture
def installed_grow_data_file():
    handle = install_source_definition(GROW_DATA_FILE_DEF)
    try:
        yield handle
    finally:
        handle.uninstall()
        assert "grow_data_file" not in REGISTRY


@pytest.fixture
def installed_grow_journal():
    handle = install_source_definition(GROW_JOURNAL_DEF)
    try:
        yield handle
    finally:
        handle.uninstall()
        assert "grow_journal" not in REGISTRY


@pytest.mark.asyncio
async def test_grow_data_file_ingests_full_csv(
    migrated_conn,
    tmp_path,
    monkeypatch,
    stub_post_canonical,
    installed_grow_data_file,
) -> None:
    rows = _load_grow_rows()
    monkeypatch.setattr(core_state, "get_db_connection", lambda: migrated_conn)

    file_store = RawFileStore(base_path=tmp_path)
    trigger = FileTrigger(file_store=file_store)
    job = trigger.create_job_from_bytes(
        job_id="grow-data-file-job",
        dataset_id="user:default:device",
        schema_id="journal.time_log.v1",
        payload=GROW_CSV.read_bytes(),
        file_format="csv",
    )

    manager = IngestionManager(file_store=file_store)
    result = await manager.process_job(job, source_id="grow_data_file")

    assert result["records_processed"] == len(rows)
    assert result["errors_count"] == 0

    session_count = migrated_conn.execute(
        "SELECT COUNT(*) FROM grow_data_sessions WHERE source_id='grow_data_file'"
    ).fetchone()[0]
    journal_count = migrated_conn.execute(
        "SELECT COUNT(*) FROM journal_entries WHERE source_id='grow_data_file'"
    ).fetchone()[0]
    assert session_count == len(rows)
    assert journal_count == len(rows)

    first = migrated_conn.execute(
        """
        SELECT entry_id, starts_at, ends_at, category, place_name, content
        FROM journal_entries
        WHERE source_id='grow_data_file' AND entry_id='tl-1'
        """
    ).fetchone()
    assert first is not None
    assert first["starts_at"] == "2026-05-01T08:00:00"
    assert first["ends_at"] == "2026-05-01T08:55:00"
    assert first["category"] == "Job Applications"
    assert first["place_name"] == "Timbercreek Apt"
    assert "Goal: Update resume" in first["content"]
    assert "better-half.ai" in first["content"]


@pytest.mark.asyncio
async def test_grow_journal_ui_stream_ingests_csv_rows(
    migrated_conn,
    monkeypatch,
    stub_post_canonical,
    installed_grow_journal,
) -> None:
    rows = _load_grow_rows()
    monkeypatch.setattr(core_state, "get_db_connection", lambda: migrated_conn)

    for row in rows:
        result = await _ingest_ui_payload_direct(
            dataset_id="user:default:device",
            schema_id="journal.time_log.v1",
            payload=dict(row),
            job_id=f"grow-ui-{row['num']}",
            source_id="grow_journal",
        )
        assert result["status"] == "ok", result.get("error")
        assert result["errors_count"] == 0

    session_count = migrated_conn.execute(
        "SELECT COUNT(*) FROM grow_journal_sessions WHERE source_id='grow_journal'"
    ).fetchone()[0]
    journal_count = migrated_conn.execute(
        "SELECT COUNT(*) FROM journal_entries WHERE source_id='grow_journal'"
    ).fetchone()[0]
    assert session_count == len(rows)
    assert journal_count == len(rows)

    multiline = migrated_conn.execute(
        """
        SELECT entry_id, content
        FROM journal_entries
        WHERE source_id='grow_journal' AND entry_id='tl-4'
        """
    ).fetchone()
    assert multiline is not None
    assert "Nicholas and Thomas" in multiline["content"]
