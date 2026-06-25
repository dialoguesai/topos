"""Canonical ingest tests for signal-dimension demo harness."""

from __future__ import annotations

import sqlite3

import pytest

from topos.ingestion.canonical_pipeline import canonicalize_normalized_batch
from topos.ingestion.parsers.demo_file_parsers import (
    DemoCalendarParser,
    DemoContactsParser,
    DemoFinancialParser,
    JournalTimeLogFileParser,
    DemoJournalParser,
    DemoMessengerParser,
    DemoPlacesParser,
    DemoProfileParser,
)
from topos.ingestion.sources.base import RawRecord
from topos.sources.registry import (
    DEMO_CALENDAR_FILE,
    DEMO_CONTACTS_FILE,
    DEMO_FINANCIAL_FILE,
    DEMO_JOURNAL_FILE,
    DEMO_MESSENGER_FILE,
    DEMO_PLACES_FILE,
    DEMO_RESUME_FILE,
)
from topos.sources.definitions import DataSourceDefinition
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture
def migrated_conn(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "harness.db"))
    conn.row_factory = sqlite3.Row
    apply_all_migrations(conn)
    yield conn
    conn.close()


def test_demo_parser_accepts_schema_id_kwarg() -> None:
    parser = DemoCalendarParser(dataset_id="user:default:device", _schema_id="demo.calendar.v1")
    assert parser.schema_id() == "demo.calendar.v1"


def test_demo_calendar_canonicalize(migrated_conn) -> None:
    parser = DemoCalendarParser(dataset_id="user:default:device")
    raw = {
        "event_id": "cal-test-1",
        "title": "Investor sync",
        "starts_at": "2026-03-13T10:00:00Z",
        "ends_at": "2026-03-13T11:00:00Z",
        "location": "Zoom",
        "attendees": "Jordan Lee",
        "is_busy": "true",
    }
    norm = parser.parse(RawRecord(record_id="cal-test-1", payload=raw))
    result = canonicalize_normalized_batch(
        migrated_conn,
        DEMO_CALENDAR_FILE,
        [norm],
        dataset_id="user:default:device",
        sync_batch_id="batch-cal",
    )
    assert result.events_created == 1
    row = migrated_conn.execute(
        "SELECT title FROM calendar_events WHERE event_id=?",
        ("cal-test-1",),
    ).fetchone()
    assert row["title"] == "Investor sync"


def test_demo_journal_and_profile_canonicalize(migrated_conn) -> None:
    journal_parser = DemoJournalParser(dataset_id="user:default:device")
    journal_norm = journal_parser.parse(
        RawRecord(
            record_id="j1",
            payload={
                "entry_id": "j1",
                "content": "Rested well",
                "entry_at": "2026-03-11T07:00:00Z",
                "mood_tag": "calm",
                "category": "wellbeing",
            },
        )
    )
    canonicalize_normalized_batch(
        migrated_conn,
        DEMO_JOURNAL_FILE,
        [journal_norm],
        dataset_id="user:default:device",
        sync_batch_id="batch-j",
    )
    assert migrated_conn.execute("SELECT 1 FROM journal_entries WHERE entry_id='j1'").fetchone()

    profile_parser = DemoProfileParser(dataset_id="user:default:device")
    profile_norm = profile_parser.parse(
        RawRecord(
            record_id="p1",
            payload={"record_id": "p1", "record_type": "skill", "title": "Python", "description": "Backend"},
        )
    )
    canonicalize_normalized_batch(
        migrated_conn,
        DEMO_RESUME_FILE,
        [profile_norm],
        dataset_id="user:default:device",
        sync_batch_id="batch-p",
    )
    assert migrated_conn.execute("SELECT title FROM profile_records WHERE record_id='p1'").fetchone()["title"] == "Python"


def test_demo_messenger_and_financial_and_places(migrated_conn) -> None:
    messenger_parser = DemoMessengerParser(dataset_id="user:default:device")
    msg_norm = messenger_parser.parse(
        RawRecord(
            record_id="m1",
            payload={
                "message_id": "m1",
                "conversation_id": "conv-1",
                "sender_id": "sara",
                "sender_name": "Sara Chen",
                "is_from_self": "false",
                "event_at": "2026-03-10T09:00:00Z",
                "content": "Intro to Marcus?",
            },
        )
    )
    canonicalize_normalized_batch(
        migrated_conn,
        DEMO_MESSENGER_FILE,
        [msg_norm],
        dataset_id="user:default:device",
        sync_batch_id="batch-m",
    )
    assert migrated_conn.execute("SELECT content FROM conversation_messages WHERE message_id='m1'").fetchone()

    fin_parser = DemoFinancialParser(dataset_id="user:default:device")
    fin_norm = fin_parser.parse(
        RawRecord(
            record_id="f1",
            payload={
                "transaction_id": "f1",
                "account_type": "income",
                "amount": "12000",
                "posted_at": "2026-03-01T08:00:00Z",
                "category": "salary",
                "description": "Payroll",
            },
        )
    )
    canonicalize_normalized_batch(
        migrated_conn,
        DEMO_FINANCIAL_FILE,
        [fin_norm],
        dataset_id="user:default:device",
        sync_batch_id="batch-f",
    )
    row = migrated_conn.execute("SELECT amount FROM financial_transactions WHERE transaction_id='f1'").fetchone()
    assert row["amount"] == 12000.0

    places_parser = DemoPlacesParser(dataset_id="user:default:device")
    place_norm = places_parser.parse(
        RawRecord(
            record_id="loc-1",
            payload={
                "event_id": "loc-1",
                "place_name": "Austin Office",
                "city": "Austin",
                "region": "Texas",
                "country": "US",
                "event_at": "2026-03-14T14:00:00Z",
                "event_type": "office_visit",
            },
        )
    )
    canonicalize_normalized_batch(
        migrated_conn,
        DEMO_PLACES_FILE,
        [place_norm],
        dataset_id="user:default:device",
        sync_batch_id="batch-l",
    )
    assert migrated_conn.execute("SELECT city FROM location_events WHERE event_id='loc-1'").fetchone()["city"] == "Austin"


def test_demo_contacts_canonicalize(migrated_conn) -> None:
    parser = DemoContactsParser(dataset_id="user:default:device")
    rows = [
        {
            "contact_id": "contact-sara",
            "display_name": "Sara Chen",
            "identifier": "sara.chen@example.com",
            "identifier_type": "email",
        },
        {
            "contact_id": "contact-sara",
            "display_name": "Sara Chen",
            "identifier": "+14155550101",
            "identifier_type": "phone",
        },
    ]
    norms = [
        parser.parse(RawRecord(record_id=f"{r['contact_id']}:{r['identifier']}", payload=r))
        for r in rows
    ]
    result = canonicalize_normalized_batch(
        migrated_conn,
        DEMO_CONTACTS_FILE,
        norms,
        dataset_id="user:default:device",
        sync_batch_id="batch-c",
    )
    assert result.messages_created == 2
    assert migrated_conn.execute(
        "SELECT COUNT(*) AS n FROM contacts WHERE source_id=?",
        (DEMO_CONTACTS_FILE.source_id,),
    ).fetchone()["n"] == 1
    assert migrated_conn.execute(
        "SELECT COUNT(*) AS n FROM contact_identifiers WHERE contact_id='contact-sara'",
    ).fetchone()["n"] == 2
    signal = result.canonical_records[0]
    assert signal.get("message_id") or signal.get("contact_id")


def test_demo_journal_signal_record_has_message_id(migrated_conn) -> None:
    journal_parser = DemoJournalParser(dataset_id="user:default:device")
    journal_norm = journal_parser.parse(
        RawRecord(
            record_id="j-emo",
            payload={
                "entry_id": "j-emo",
                "content": "Feeling hopeful after the investor call",
                "entry_at": "2026-03-12T18:00:00Z",
                "mood_tag": "hopeful",
                "category": "wellbeing",
            },
        )
    )
    result = canonicalize_normalized_batch(
        migrated_conn,
        DEMO_JOURNAL_FILE,
        [journal_norm],
        dataset_id="user:default:device",
        sync_batch_id="batch-j-emo",
    )
    assert result.canonical_records
    rec = result.canonical_records[0]
    assert rec["message_id"] == "j-emo"
    assert rec["content"]


def test_journal_time_log_canonicalize(migrated_conn) -> None:
    time_log_source = DataSourceDefinition(
        source_id="time_log",
        display_name="Time Log",
        source_type="file",
        schema_id="journal.time_log.v1",
        parser_id="journal.time_log.v1",
        canonical_mapper_id="journal_time_log",
        canonical_group_id="journal",
    )
    parser = JournalTimeLogFileParser(dataset_id="user:default:device")
    raw = {
        "num": "99",
        "startDate": "2026-05-01",
        "startTime": "8:00 AM",
        "endDate": "2026-05-01",
        "endTime": "8:55 AM",
        "duration": "55",
        "project": "Topos",
        "goal": "Ship time-log source",
        "accomplished": "Parser and mapper wired.",
        "completed": "TRUE",
        "location": "Home",
        "group": "Solo",
    }
    norm = parser.parse(RawRecord(record_id="99", payload=raw))
    from topos.ingestion.manager import _persist_source_data_tables

    _persist_source_data_tables(
        db_conn=migrated_conn,
        source_def=replace_time_log_source_with_table(time_log_source),
        dataset_id="user:default:device",
        normalized_records=[norm],
    )
    result = canonicalize_normalized_batch(
        migrated_conn,
        time_log_source,
        [norm],
        dataset_id="user:default:device",
        sync_batch_id="batch-time-log",
    )
    assert result.messages_created == 1
    row = migrated_conn.execute(
        "SELECT entry_id, entry_at, starts_at, ends_at, category, content, duration, people, place_name FROM journal_entries WHERE entry_id='tl-99'"
    ).fetchone()
    assert row["category"] == "Topos"
    assert row["starts_at"] == "2026-05-01T08:00:00"
    assert row["ends_at"] == "2026-05-01T08:55:00"
    assert row["entry_at"]
    assert row["duration"] == "55"
    assert row["people"] == "Solo"
    assert row["place_name"] == "Home"
    assert "Goal: Ship time-log source" in row["content"]
    assert "Accomplished: Parser and mapper wired." in row["content"]
    assert result.canonical_records[0]["message_id"] == "tl-99"

    session = migrated_conn.execute(
        "SELECT record_id, goal, project, source_id FROM time_log_sessions WHERE record_id='tl-99'"
    ).fetchone()
    assert session is not None
    assert session["goal"] == "Ship time-log source"
    assert session["project"] == "Topos"
    assert session["source_id"] == "time_log"


def replace_time_log_source_with_table(source: DataSourceDefinition) -> DataSourceDefinition:
    from dataclasses import replace

    return replace(
        source,
        pipeline_include_data_table=True,
        tables=[
            {
                "table_id": "time_log_sessions",
                "display_name": "Sessions",
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
    )
