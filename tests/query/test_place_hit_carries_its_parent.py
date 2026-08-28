"""A place hit must bring the entry it came from — if the grant allows it.

``location_events`` is a fan-out CHILD of a journal entry and its whole document
is the place name, so a vector or keyword hit on it could only ever answer with
that name. "Who did I eat with at X?" came back with the string "X". The parent
carries the narrative, ``source_record_id`` has pointed at it since ingest, and
no reader had ever followed it — the same missing join this whole workstream
started from, at the query end instead of the graph end.

The manifest gate is the design, not a caveat. Pulling journal prose into a
LOCATION-scoped grant is the exact inverse of the defect the register opened
with (a journal-only grant admitting location evidence), and worse, because the
journal is the richer surface. So the parent is read only when
``journal_entries`` is itself in the manifest, and it goes through the same
scope redaction as any journal row.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.query.manifest import ScopeResolutionManifest


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "q.db"))
    c.row_factory = sqlite3.Row
    apply_all_migrations(c)
    c.execute(
        "INSERT INTO journal_entries (entry_id, content, source_id, entry_at)"
        " VALUES ('tl-1','Dinner with Robin, talked about the move','grow_journal','2026-07-01T19:00:00Z')"
    )
    c.execute(
        "INSERT INTO location_events (event_id, place_name, event_at, source_id, source_record_id)"
        " VALUES ('tl-1-loc','The Lantern Cafe','2026-07-01T19:00:00Z','grow_journal','tl-1')"
    )
    c.commit()
    yield c
    c.close()


def _manifest(tables):
    return ScopeResolutionManifest(
        scope_id="owner:read", primary_dimensions=["places"], canonical_tables=list(tables)
    )


def _summary(conn, manifest):
    from topos.query.retrieval import _canonical_row_to_item

    row = dict(conn.execute("SELECT * FROM location_events WHERE event_id='tl-1-loc'").fetchone())
    row["record_id"] = row["event_id"]
    item = _canonical_row_to_item(
        "location_events", row, manifest=manifest, query_text="where did I eat",
        conn=conn, first_person=False, belief_intent=False, exposure_visible=True,
        role_cache={}, display_cache={}, highlight_cache={},
    )
    return (item or {}).get("summary_text") or (item or {}).get("text") or ""


def test_a_place_hit_carries_the_parent_narrative(conn):
    """The whole point: the answer stops being the place name alone."""
    text = _summary(conn, _manifest(["location_events", "journal_entries"]))

    assert "Lantern" in text, "the place itself must survive"
    assert "Robin" in text, "the parent entry's narrative must come with it"


def test_a_location_only_grant_gets_no_journal_prose(conn):
    """The inverse leak, and the reason this is gated.

    A grant for places must not become a grant for the diary because the two
    share a row id.
    """
    text = _summary(conn, _manifest(["location_events"]))

    assert "Lantern" in text
    assert "Robin" not in text, "journal prose leaked into a location-only grant"


def test_a_child_with_no_parent_is_unchanged(conn):
    conn.execute(
        "INSERT INTO location_events (event_id, place_name, event_at, source_id)"
        " VALUES ('orphan-loc','Somewhere','2026-07-02T10:00:00Z','grow_journal')"
    )
    conn.commit()
    from topos.query.retrieval import _canonical_row_to_item

    row = dict(conn.execute("SELECT * FROM location_events WHERE event_id='orphan-loc'").fetchone())
    row["record_id"] = row["event_id"]
    item = _canonical_row_to_item(
        "location_events", row,
        manifest=_manifest(["location_events", "journal_entries"]),
        query_text="where did I eat", conn=conn, first_person=False,
        belief_intent=False, exposure_visible=True,
        role_cache={}, display_cache={}, highlight_cache={},
    )

    assert "Somewhere" in ((item or {}).get("summary_text") or "")


def test_a_dangling_parent_id_is_survivable(conn):
    """The parent may have been deleted while the child lingers."""
    conn.execute("UPDATE location_events SET source_record_id='gone' WHERE event_id='tl-1-loc'")
    conn.commit()

    text = _summary(conn, _manifest(["location_events", "journal_entries"]))

    assert "Lantern" in text
