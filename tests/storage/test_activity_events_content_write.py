"""activity_events writes must PERSIST content + hostname.

The activity_events_content_v1 migration added both columns and two producers
fill them — the P2.1 browser mapper (highlight span, hostname) and §5a declared
field maps (github commit messages) — but the store's INSERT never listed the
columns, so every value they computed was dropped at the write (0 of 4,444 rows
populated on the first live node checked). These pin the write and the heal:
re-upserting an existing row fills a NULL content/hostname instead of leaving
the pre-fix row dark forever.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.canonicalization.mappers.browser_activity_mapper import BrowserActivityCanonicalMapper
from topos.ingestion.parsers.base import NormalizedRecord
from topos.storage.canonical.canonical_store import SQLiteCanonicalStore
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    apply_all_migrations(connection)
    yield connection
    connection.close()


def _row(conn, event_id: str):
    return conn.execute(
        "SELECT content, hostname, title, metadata_json FROM activity_events WHERE event_id=?",
        (event_id,),
    ).fetchone()


def test_content_and_hostname_are_written(conn) -> None:
    SQLiteCanonicalStore(conn).upsert(
        "activity_events",
        {
            "event_id": "github:1",
            "activity_type": "push",
            "title": "dialoguesai/topos: pushed 1 commit",
            "occurred_at": "2026-07-01T12:34:56Z",
            "source_id": "github_activity",
            "content": "fix: tighten retry loop",
            "hostname": "github.com",
        },
    )
    row = _row(conn, "github:1")
    assert row["content"] == "fix: tighten retry loop"
    assert row["hostname"] == "github.com"


def test_reupsert_heals_a_content_free_row(conn) -> None:
    store = SQLiteCanonicalStore(conn)
    store.upsert(
        "activity_events",
        {
            "event_id": "github:2",
            "activity_type": "push",
            "title": "dialoguesai/topos: pushed 1 commit",
            "source_id": "github_activity",
        },
    )
    assert _row(conn, "github:2")["content"] is None

    store.upsert(
        "activity_events",
        {
            "event_id": "github:2",
            "activity_type": "push",
            "title": "dialoguesai/topos: pushed 1 commit",
            "source_id": "github_activity",
            "content": "fix: tighten retry loop",
            "metadata_json": {"repo": "dialoguesai/topos"},
        },
    )
    row = _row(conn, "github:2")
    assert row["content"] == "fix: tighten retry loop"
    assert '"repo": "dialoguesai/topos"' in row["metadata_json"]


def test_blank_values_never_overwrite_stored_content(conn) -> None:
    """A later batch that carries no content must not blank a stored value."""
    store = SQLiteCanonicalStore(conn)
    store.upsert(
        "activity_events",
        {
            "event_id": "github:3",
            "activity_type": "push",
            "source_id": "github_activity",
            "content": "docs: add sync notes",
        },
    )
    store.upsert(
        "activity_events",
        {
            "event_id": "github:3",
            "activity_type": "push",
            "source_id": "github_activity",
            "content": "  ",
        },
    )
    assert _row(conn, "github:3")["content"] == "docs: add sync notes"


def test_browser_highlight_span_reaches_the_column(conn) -> None:
    """P2.1's one expression-grade browser signal, end to end through the store."""
    mapped = BrowserActivityCanonicalMapper().map(
        NormalizedRecord(
            record_id="visit-9",
            payload={
                "id": "visit-9",
                "event_type": "highlight",
                "url": "https://example.com/post",
                "title": "A post",
                "content": '{"selectedText": "the part I actually cared about"}',
                "visited_at": "2026-07-01T09:00:00Z",
            },
        )
    )
    SQLiteCanonicalStore(conn).upsert("activity_events", {**mapped.payload, "source_id": "browser_events"})
    row = _row(conn, "browser:visit-9")
    assert row["content"] == "the part I actually cared about"
    assert row["hostname"] == "example.com"
