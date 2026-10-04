"""Grow CSV ingestion via grow_data_file (file) and grow_journal (ui_stream), on an invented export.

The export is made here: the columns a Grow export carries as far as the time-log parser reads them
(``topos/ingestion/journal_time_log_normalize.py``: the session number, start and end as a date and a 12- or 24-hour
time, duration, project, goal, accomplished, completed, location, group, the mood label, the entity and word-count
columns, the form flag and the creation time), with every value coined. No person, place, project or time here is the
owner's, and nothing outside the repository is read (BL-35: this test used to read the owner's own export from the
workspace root, and to compare against values copied from it).

protects: a whole export ingests through the file door and row by row through the UI stream, one session row and one
journal entry per export row; the times, the project, the place and the goal and accomplished text arrive whole,
including a field with line breaks, a blank line, commas and quotes (BL-12), an empty place and a "null" mood.
"""

from __future__ import annotations

import csv
import io
import sqlite3

import pytest

from topos.core import state as core_state
from topos.ingestion.ingest_helpers import _ingest_ui_payload_direct
from topos.ingestion.manager import IngestionManager
from topos.ingestion.triggers.file_trigger import FileTrigger
from topos.sources.registry import REGISTRY
from topos.sources.runtime_install import install_source_definition
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.raw.file_store import RawFileStore

COLUMNS = ["num", "startDate", "startTime", "endDate", "endTime", "duration", "project", "goal", "accomplished",
           "completed", "location", "group", "emotionLabel", "goalEntities", "accomplishedEntities", "goalWordCount",
           "accomplishedWordCount", "hasForm", "createdAt"]
#: Session 4's notes: line breaks, a blank line, a comma and quotes inside one quoted field.
MULTILINE = ("Planned the seedling swap with Ferrow and Quellin.\nThey bring the tomato starts, I bring the trays."
             "\n\nNext: water on Thursday, \"lightly\".")


def _row(num, day, start, end, minutes, project, goal, accomplished, *, completed="true", location="Workshop corner",
         group="Solo", mood="calm", made=None):
    return {"num": str(num), "startDate": day, "startTime": start, "endDate": day, "endTime": end,
            "duration": str(minutes), "project": project, "goal": goal, "accomplished": accomplished,
            "completed": completed, "location": location, "group": group, "emotionLabel": mood,
            "goalEntities": project.lower(), "accomplishedEntities": "", "goalWordCount": str(len(goal.split())),
            "accomplishedWordCount": str(len(accomplished.split())), "hasForm": "true",
            "createdAt": made or f"{day}T23:30:00Z"}


ROWS = [
    _row(1, "2026-04-06", "9:15 AM", "10:05 AM", 50, "Shed Rebuild", "Sand the shed door",
         "Sanded both panels; plans kept at example.org/shed-plans."),
    _row(2, "2026-04-06", "1:30 PM", "2:10 PM", 40, "Recipe Book", "Test the rye loaf",
         "Second proof was too short, crumb dense.", completed="false", mood="null"),
    _row(3, "2026-04-07", "07:45", "08:20", 35, "Bike Repair", "Swap the rear brake pads",
         "Pads swapped, cable trimmed, test ride fine.", location="", group="Solo"),
    _row(4, "2026-04-08", "6:00 PM", "7:15 PM", 75, "Allotment", "Plan the seedling swap", MULTILINE,
         location="Plot 12 bench", group="Allotment friends"),
    _row(5, "2026-04-09", "8:05 AM", "8:50 AM", 45, "Reading Club", "Finish chapter seven",
         "Finished it; two questions noted for the club.", mood="curious"),
    _row(6, "2026-04-10", "11:00 AM", "12:30 PM", 90, "Shed Rebuild", "Prime the door",
         "Primer on, a \"second\" coat tomorrow.", made="2026-04-10T12:31:00Z"),
]


def _export() -> bytes:
    """The invented export, written as a spreadsheet writes CSV (CRLF rows, quoted fields where needed)."""
    out = io.StringIO(newline="")
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(ROWS)
    return out.getvalue().encode("utf-8")


EXPORT = _export()

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
    """The export's rows as a CSV reader gives them, as the UI stream sends them one by one."""
    return list(csv.DictReader(io.StringIO(EXPORT.decode("utf-8"), newline="")))


@pytest.fixture
def migrated_conn(tmp_path):
    # The ingest DB stretch runs on a worker thread (asyncio.to_thread),
    # so the injected connection must allow cross-thread use.
    conn = sqlite3.connect(str(tmp_path / "grow_ingest.db"), check_same_thread=False)
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
        payload=EXPORT,
        file_format="csv",
    )

    manager = IngestionManager(file_store=file_store)
    result = await manager.process_job(job, source_id="grow_data_file")

    assert len(rows) == len(ROWS) == 6
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
    assert first["starts_at"] == "2026-04-06T09:15:00"
    assert first["ends_at"] == "2026-04-06T10:05:00"
    assert first["category"] == "Shed Rebuild"
    assert first["place_name"] == "Workshop corner"
    assert "Goal: Sand the shed door" in first["content"]
    assert "example.org/shed-plans" in first["content"]

    multiline = migrated_conn.execute(
        "SELECT content FROM journal_entries WHERE source_id='grow_data_file' AND entry_id='tl-4'"
    ).fetchone()
    assert multiline["content"] == f"Goal: Plan the seedling swap\n\nAccomplished: {MULTILINE}"
    no_place = migrated_conn.execute(
        "SELECT starts_at, place_name FROM journal_entries WHERE source_id='grow_data_file' AND entry_id='tl-3'"
    ).fetchone()
    assert (no_place["starts_at"], no_place["place_name"]) == ("2026-04-07T07:45:00", None)


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
    assert multiline["content"] == f"Goal: Plan the seedling swap\n\nAccomplished: {MULTILINE}"
    moods = dict(migrated_conn.execute(
        "SELECT entry_id, mood_tag FROM journal_entries WHERE source_id='grow_journal' AND entry_id IN ('tl-1','tl-2')"
    ).fetchall())
    assert moods == {"tl-1": "calm", "tl-2": None}               # the export's "null" mood is no mood
