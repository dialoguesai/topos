"""OD-53, going forward: a source that declares its time zone lets the journal door say when a row happened.

protects: a journal row's timestamp is naive local text, so a grant can place it only by its stated
day (``permissions_v2/evidence_time.py``). The owner knows which zone their app writes in; declared
once on the source definition, the door can record each new row's event time with that zone's offset
(``journal_entries.event_time_json``, the ``topos-event-time/v1`` record messages already carry).
What must not happen: a payload naming its own time record, a declaration made later re-dating rows
already stored, a record outliving the time it was computed from, or a guess for a local time a clock
change repeats or skips.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict

import pytest

from topos.canonicalization.mappers.base import CanonicalRecord
from topos.features.temporal.records import EventTime, declared_zone_event_time
from topos.ingestion.canonical_pipeline import canonicalize_normalized_batch
from topos.permissions_v2.evidence_time import STATED_DAY, event_bounds, row_time_text
from topos.sources.definitions import DataSourceDefinition, definition_from_payload
from topos.sources.registry import REGISTRY
from topos.storage.canonical.canonical_store import SQLiteCanonicalStore
from topos.storage.db.migrations import read_user_version
from topos.storage.db.migrations.temporal_fields_v1 import apply_temporal_fields_v1_up

from test_ai_chat_writer_class import DATASET, conn  # noqa: F401
from test_canonical_writer_class import _rows
from test_journal_time_log_ui_stream_ingest import TIME_LOG_SOURCE_DEF

ZONE = "America/Chicago"
HOUR = 3600 * 1_000_000


class _CarryingMapper:
    def map_many(self, normalized):
        return [CanonicalRecord(record_id=normalized.record_id, payload=dict(normalized.payload))]


@pytest.fixture()
def journal_source(monkeypatch):
    """A journal-lane source, installed with or without a declared zone."""
    from topos.sources.runtime_install import install_source_definition

    monkeypatch.setattr("topos.canonicalization.declared_field_map.build_canonical_mapper",
                        lambda source_def, **_kwargs: _CarryingMapper())
    handles = []

    def install(time_zone=None):
        payload = {**TIME_LOG_SOURCE_DEF, **({"time_zone": time_zone} if time_zone else {})}
        handles.append(install_source_definition(payload))
        return REGISTRY["time_log"]

    yield install
    for handle in reversed(handles):
        handle.uninstall()


def _entry(**overrides: Any) -> Dict[str, Any]:
    entry = {"entry_id": "e1", "content": "Walked the long way home.", "entry_at": "2026-09-10T08:30:00"}
    entry.update(overrides)
    return entry


def _write(conn, source_def, payload, *, writer_class="owner_app"):
    result = canonicalize_normalized_batch(conn, source_def, [payload], dataset_id=DATASET, sync_batch_id="b",
                                           writer_class=writer_class)
    assert result.errors == []
    (row,) = _rows(conn, "journal_entries")
    return row


def _stated(row) -> str | None:
    return None if row["event_time_json"] is None else EventTime.from_json(row["event_time_json"]).event.text


# --- the declaration --------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["UTC", "Europe/Lisbon", "America/Argentina/Buenos_Aires", "Etc/GMT+5"])
def test_a_source_definition_can_declare_its_zone(name):
    definition = definition_from_payload({**TIME_LOG_SOURCE_DEF, "time_zone": name})
    assert definition.time_zone == name and definition.to_dict()["time_zone"] == name
    assert definition_from_payload(definition.to_dict()).time_zone == name


def test_an_undeclared_zone_is_absent_not_a_default():
    definition = definition_from_payload(dict(TIME_LOG_SOURCE_DEF))
    assert definition.time_zone is None and "time_zone" not in definition.to_dict()


@pytest.mark.parametrize("name", ["", " UTC", "UTC ", "-05:00", "../../etc/passwd", "UTC;x", 5, "a/b/c/d"])
def test_a_malformed_zone_name_is_refused_at_install(name):
    with pytest.raises(ValueError):
        DataSourceDefinition(source_id="s", display_name="S", source_type="ui_stream", schema_id="x", parser_id="x",
                             time_zone=name)


def test_the_always_run_step_adds_the_column_without_moving_the_version(tmp_path):
    db = sqlite3.connect(str(tmp_path / "legacy.db"))
    db.execute("CREATE TABLE journal_entries (entry_id TEXT PRIMARY KEY, entry_at TEXT, source_id TEXT NOT NULL)")
    db.execute("INSERT INTO journal_entries VALUES ('old', '2026-06-01T09:00:00', 'time_log')")
    before = read_user_version(db)
    for _ in range(2):
        apply_temporal_fields_v1_up(db)
    assert "event_time_json" in {row[1] for row in db.execute("PRAGMA table_info(journal_entries)")}
    assert read_user_version(db) == before
    assert db.execute("SELECT event_time_json FROM journal_entries").fetchall() == [(None,)]  # no backfill


# --- the local reading in the declared zone ---------------------------------------------------------

def test_a_local_reading_gets_the_zones_offset_at_that_moment():
    assert EventTime.from_json(declared_zone_event_time("2026-09-10T08:30:00", ZONE)).event.text == "2026-09-10T08:30:00-05:00"
    assert EventTime.from_json(declared_zone_event_time("2026-01-10T08:30:00", ZONE)).event.text == "2026-01-10T08:30:00-06:00"
    assert EventTime.from_json(declared_zone_event_time("2026-09-10T08:30:00", "UTC")).event.text == "2026-09-10T08:30:00Z"
    assert EventTime.from_json(declared_zone_event_time("2026-09-10T08:30:00.25", "Asia/Kolkata")).event.text == (
        "2026-09-10T08:30:00.25+05:30")


@pytest.mark.parametrize("text, zone", [
    ("2026-11-01T01:30:00", ZONE),          # the hour the clocks repeat: two instants, so none is stated
    ("2026-03-08T02:30:00", ZONE),          # the hour the clocks skip: no such local time
    ("2026-09-10T08:30:00", "Nowhere/Land"),  # a name this machine's zone database does not know
    ("2026-09-10", ZONE),                   # a day is not a reading of a clock
    ("2026-09-10T08:30:00Z", ZONE), ("2026-09-10T08:30:00-04:00", ZONE),  # already says its own basis
    ("not a time", ZONE), (None, ZONE), ("2026-09-10T08:30:00", None), ("2026-09-10T08:30:00", 5)])
def test_no_record_is_written_when_the_instant_would_be_a_guess(text, zone):
    assert declared_zone_event_time(text, zone) is None


# --- the door ---------------------------------------------------------------------------------------

def test_a_door_write_through_a_declared_source_records_when_the_row_happened(conn, journal_source):
    row = _write(conn, journal_source(ZONE), _entry())
    assert row["entry_at"] == "2026-09-10T08:30:00"  # the owner-facing time is left as written
    assert _stated(row) == "2026-09-10T08:30:00-05:00"
    # ... and a grant can place it exactly, where an undeclared row is only its day.
    written = 1789029000 * 1_000_000  # 2026-09-10T08:30:00Z
    assert event_bounds(row_time_text(row, column="entry_at"), semantics=STATED_DAY) == (
        written + 5 * HOUR, written + 5 * HOUR)


def test_an_undeclared_source_records_nothing(conn, journal_source):
    row = _write(conn, journal_source(), _entry())
    assert row["event_time_json"] is None
    assert row_time_text(row, column="entry_at") == "2026-09-10T08:30:00"


def test_a_payload_cannot_carry_its_own_time_record_or_zone(conn, journal_source):
    forged = _entry(event_time_json=declared_zone_event_time("2026-09-10T08:30:00", "Asia/Tokyo"),
                    declared_time_zone="Asia/Tokyo")
    assert _stated(_write(conn, journal_source(ZONE), forged)) == "2026-09-10T08:30:00-05:00"
    conn.execute("DELETE FROM journal_entries")
    assert _write(conn, journal_source(), dict(forged))["event_time_json"] is None
    # Nor through a path with no door, where the pipeline names no zone of its own.
    conn.execute("DELETE FROM journal_entries")
    assert _write(conn, journal_source(ZONE), dict(forged), writer_class=None)["event_time_json"] is None


def test_a_write_with_no_door_never_dates_a_row(conn, journal_source):
    """A replay or a reprocess after the owner declares a zone must not re-date rows written before it."""
    row = _write(conn, journal_source(ZONE), _entry(), writer_class=None)
    assert row["event_time_json"] is None


def test_a_replay_keeps_the_record_the_door_wrote(conn, journal_source):
    source = journal_source(ZONE)
    _write(conn, source, _entry())
    row = _write(conn, source, _entry(content="Walked the long way home, twice."), writer_class=None)
    assert _stated(row) == "2026-09-10T08:30:00-05:00" and row["content"].endswith("twice.")


def test_a_resend_under_a_later_declaration_keeps_the_first_record(conn, journal_source):
    _write(conn, journal_source(ZONE), _entry())
    row = _write(conn, journal_source("Europe/Lisbon"), _entry())
    assert _stated(row) == "2026-09-10T08:30:00-05:00"


def test_a_record_never_outlives_the_time_it_was_computed_from(conn, journal_source):
    declared = journal_source(ZONE)
    _write(conn, declared, _entry())
    moved = _write(conn, declared, _entry(entry_at="2026-09-11T21:00:00"))
    assert _stated(moved) == "2026-09-11T21:00:00-05:00"          # a door write restates it
    replayed = _write(conn, declared, _entry(entry_at="2026-09-12T07:00:00"), writer_class=None)
    assert replayed["entry_at"] == "2026-09-12T07:00:00" and replayed["event_time_json"] is None  # no door: cleared


def test_a_repeated_local_hour_is_left_as_its_stated_day(conn, journal_source):
    row = _write(conn, journal_source(ZONE), _entry(entry_at="2026-11-01T01:30:00"))
    assert row["event_time_json"] is None


def test_the_store_ignores_a_zone_on_a_database_without_the_column(tmp_path):
    db = sqlite3.connect(str(tmp_path / "old.db"))
    db.execute("""CREATE TABLE journal_entries (entry_id TEXT PRIMARY KEY, entry_at TEXT, starts_at TEXT, ends_at TEXT,
        mood_tag TEXT, category TEXT, content TEXT, duration TEXT, people TEXT, place_name TEXT, source_id TEXT NOT NULL,
        source_record_id TEXT, ingested_at TEXT, sync_batch_id TEXT, metadata_json TEXT)""")
    SQLiteCanonicalStore(db).upsert("journal_entries", {**_entry(), "source_id": "time_log", "declared_time_zone": ZONE})
    assert db.execute("SELECT entry_at FROM journal_entries").fetchall() == [("2026-09-10T08:30:00",)]


# --- reading the record back ------------------------------------------------------------------------

def test_a_record_that_no_longer_describes_the_row_makes_its_time_unknown():
    good = declared_zone_event_time("2026-09-10T08:30:00", ZONE)
    assert row_time_text({"entry_at": "2026-09-10T08:30:00", "event_time_json": good}, column="entry_at") == (
        "2026-09-10T08:30:00-05:00")
    for row in ({"entry_at": "2026-09-11T08:30:00", "event_time_json": good},      # the time moved under it
                {"entry_at": "2026-09-10", "event_time_json": good},               # the column is not a reading
                {"entry_at": None, "event_time_json": good},
                # a record for a finer reading than the row's own text: not this row's time
                {"entry_at": "2026-09-10T08:30:00",
                 "event_time_json": declared_zone_event_time("2026-09-10T08:30:00.5", ZONE)},
                {"entry_at": "2026-09-10T08:30:00",
                 "event_time_json": declared_zone_event_time("2026-09-10T08:30:00.5", "UTC")},
                {"entry_at": "2026-09-10T08:30:00", "event_time_json": "{}"},      # damage is not absence
                {"entry_at": "2026-09-10T08:30:00", "event_time_json": "not json"},
                {"entry_at": "2026-09-10T08:30:00", "event_time_json": json.dumps({"version": "topos-event-time/v1"})}):
        assert row_time_text(row, column="entry_at") is None
        assert event_bounds(row_time_text(row, column="entry_at"), semantics=STATED_DAY) is None


def test_a_record_of_a_stated_day_is_not_an_instant():
    from topos.features.temporal.records import event_time
    day_record = event_time("2026-09-10", provenance="unverified_producer").to_json()
    assert row_time_text({"entry_at": "2026-09-10", "event_time_json": day_record}, column="entry_at") is None
