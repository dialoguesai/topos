"""Entities-job eligibility: every canonical group's rows must reach NER.

The 2026-07-14 OntoNotes backfill silently produced ZERO entities for
browser_visits (activity group: no `content` column, id under `event_id`) and
grow_* (journal group: id under `entry_id`) because eligibility only read
`message_id`/`content`. These tests pin the widened contract.
"""

from __future__ import annotations

from topos.enrichment.jobs.canonical.entities_job import (
    eligible_ner_records,
    record_key,
)


def test_chat_messages_still_eligible() -> None:
    rows = eligible_ner_records(
        [{"message_id": "m1", "content": "Lunch with Maya in Austin", "source_id": "imessage", "event_at": "2026-07-01T00:00:00Z"}]
    )
    assert len(rows) == 1
    assert rows[0]["id"] == "m1"
    assert rows[0]["text"] == "Lunch with Maya in Austin"
    assert rows[0]["event_at"] == "2026-07-01T00:00:00Z"


def test_activity_events_fall_back_to_title() -> None:
    # Browser visits: no content key at all; text lives in title/url.
    rows = eligible_ner_records(
        [
            {
                "event_id": "ev1",
                "record_id": "ev1",
                "title": "Dune review — Ars Technica",
                "url": "https://arstechnica.com/dune",
                "occurred_at": "2026-07-02T00:00:00Z",
                "source_id": "browser_visits",
            }
        ]
    )
    assert len(rows) == 1
    assert rows[0]["id"] == "ev1"
    assert "Dune review" in rows[0]["text"]
    assert rows[0]["event_at"] == "2026-07-02T00:00:00Z"


def test_journal_entries_use_entry_keys() -> None:
    rows = eligible_ner_records(
        [
            {
                "entry_id": "j1",
                "entry_at": "2026-07-03T00:00:00Z",
                "content": "Repotted the basil with Ken",
                "source_id": "grow_journal",
            }
        ]
    )
    assert len(rows) == 1
    assert rows[0]["id"] == "j1"
    assert rows[0]["event_at"] == "2026-07-03T00:00:00Z"


def test_junk_and_empty_rows_skipped() -> None:
    rows = eligible_ner_records(
        [
            {"message_id": "m1", "content": ""},  # empty
            {"content": "orphan text"},  # no id key
            {"message_id": "m2", "content": "bplist00 $classname junk"},  # binary artifact
        ]
    )
    # Junk content must not fall through to title/url either.
    assert [r["id"] for r in rows] == []


def test_record_key_priority_and_fallbacks() -> None:
    assert record_key({"message_id": "a", "event_id": "b"}) == "a"
    assert record_key({"event_id": "b"}) == "b"
    assert record_key({"entry_id": "c"}) == "c"
    assert record_key({}) == ""
