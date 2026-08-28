"""One instant must occupy one timeline row, whatever its spelling.

``timeline``'s primary key is ``(event_at, record_id)`` — the raw STRING. The
projection's "has this changed?" check is ``parse_ts(existing) != event_at`` —
the parsed INSTANT. The two disagree on exactly one case, and it is a case that
occurs: ``2026-06-28T18:00:00`` and ``2026-06-28T18:00:00+00:00`` parse equal and
key apart.

So the projection reported "nothing to do" and the insert landed a second row.
Worse, that verdict is stable: every future projection re-derives the same
answer, because the check that would delete the twin is the one declaring it
fine. Measured on the owner's node 2026-08-27: 196 records hold 400 rows, 204 more
than one apiece. **195 of those are rendering twins** — every naive row written
inside one backfill window on 2026-07-10, unreachable by re-projection ever
since. A timeline is a count of when things happened; those records were counted
twice in every window they fall in.

The remaining 9 are not duplicates at all, and they are why the repair groups by
PARSED INSTANT and never by record alone: one ``time_log`` record carries 10
genuinely distinct events under a shared ``record_id``. A dedup keyed on the
record would have reported "removed 9 duplicates" and destroyed 9 real events.
That test is the important one in this file.

Verified on a 526MB snapshot of the live database before any live write: 195
removed, 0 naive rows left, the 10 ``time_log`` events byte-identical, and a
second pass a clean no-op.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.timeline_projection import (
    normalize_timeline_renderings,
    project_timeline_rows,
)


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "tl.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _rows(conn, record_id=None):
    sql = "SELECT event_at, record_id, entity_ids_json, signal_dimension FROM timeline"
    args = ()
    if record_id:
        sql += " WHERE record_id=?"
        args = (record_id,)
    return conn.execute(sql + " ORDER BY event_at", args).fetchall()


def _insert(conn, event_at, record_id, *, source="grow_data_file",
            table="journal_entries", entities="[]", dimension=None):
    conn.execute(
        "INSERT INTO timeline (event_at, record_id, source_id, canonical_table,"
        " entity_ids_json, signal_dimension) VALUES (?,?,?,?,?,?)",
        (event_at, record_id, source, table, entities, dimension),
    )
    conn.commit()


def _project(conn, **kw):
    row = {
        "entry_id": kw.get("record_id", "tl-1"),
        "event_at": kw.get("event_at", "2026-06-28T18:00:00+00:00"),
        "source_id": "grow_data_file",
        "canonical_table": "journal_entries",
        "_table": "journal_entries",
    }
    row.update(kw.get("extra") or {})
    return project_timeline_rows(conn, [row], commit=True)


# ------------------------------------------------- the projection stops making them


def test_projecting_over_a_naive_twin_leaves_one_row(conn):
    """The original defect, end to end."""
    _insert(conn, "2026-06-28T18:00:00", "tl-1")

    _project(conn)

    assert len(_rows(conn)) == 1, "the naive row was shadowed by an offset twin"


def test_the_surviving_row_uses_the_canonical_rendering(conn):
    _insert(conn, "2026-06-28T18:00:00", "tl-1")

    _project(conn)

    assert _rows(conn)[0][0] == "2026-06-28T18:00:00+00:00"


def test_the_normalization_is_counted_separately_from_a_real_move(conn):
    """A rewrite because the clock changed is not the same event as a rewrite
    because the spelling did, and an operator reading the result needs both."""
    _insert(conn, "2026-06-28T18:00:00", "tl-1")

    result = _project(conn)

    assert result.rendering_normalized == 1
    assert result.timestamp_mismatch == 0


def test_a_genuine_timestamp_move_is_still_a_move(conn):
    """Control: the pre-existing behaviour must be untouched."""
    _insert(conn, "2026-06-28T18:00:00+00:00", "tl-1")

    result = _project(conn, event_at="2026-07-02T09:00:00+00:00")

    assert result.timestamp_mismatch == 1
    assert result.rendering_normalized == 0
    assert len(_rows(conn)) == 1


def test_an_already_canonical_row_is_not_rewritten(conn):
    """No-op must stay a no-op, or every projection churns the whole table."""
    _insert(conn, "2026-06-28T18:00:00+00:00", "tl-1")

    result = _project(conn)

    assert result.rendering_normalized == 0
    assert result.timestamp_mismatch == 0


def test_projection_is_idempotent_across_repeated_runs(conn):
    for _ in range(3):
        _project(conn)

    assert len(_rows(conn)) == 1


# --------------------------------------------------------------- the repair


def test_the_repair_collapses_an_existing_twin(conn):
    _insert(conn, "2026-06-28T18:00:00", "tl-1")
    _insert(conn, "2026-06-28T18:00:00+00:00", "tl-1")

    stats = normalize_timeline_renderings(conn, dry_run=False)

    assert stats["rows_removed"] == 1
    assert [r[0] for r in _rows(conn)] == ["2026-06-28T18:00:00+00:00"]


def test_the_repair_dry_runs_by_default(conn):
    _insert(conn, "2026-06-28T18:00:00", "tl-1")
    _insert(conn, "2026-06-28T18:00:00+00:00", "tl-1")

    stats = normalize_timeline_renderings(conn)

    assert stats["rows_removed"] == 1
    assert len(_rows(conn)) == 2, "a dry run must not write"


def test_the_repair_keeps_metadata_from_whichever_twin_carried_it(conn):
    """A later enrichment pass attached entities to one spelling. That work
    belongs to the record, not to the rendering that happened to receive it."""
    _insert(conn, "2026-06-28T18:00:00", "tl-1", entities='["ent-a"]', dimension="work")
    _insert(conn, "2026-06-28T18:00:00+00:00", "tl-1")

    normalize_timeline_renderings(conn, dry_run=False)

    row = _rows(conn)[0]
    assert row[2] == '["ent-a"]'
    assert row[3] == "work"


def test_the_repair_never_merges_two_real_events(conn):
    """The trap, and the reason grouping is by instant and not by record.

    One ``time_log`` record on the owner's node carries 10 distinct events under
    a shared ``record_id``. A dedup keyed on the record would report "removed 9
    duplicates" and have destroyed 9 real events.
    """
    for hour in range(17, 24):
        _insert(conn, f"2026-07-05T{hour}:00:00+00:00", "tl-job-time-log-1", source="time_log")

    stats = normalize_timeline_renderings(conn, dry_run=False)

    assert stats["rows_removed"] == 0
    assert len(_rows(conn, "tl-job-time-log-1")) == 7


def test_the_repair_separates_events_that_share_a_record_and_an_instant_only(conn):
    """Same record, one duplicated instant, one distinct — collapse only the pair."""
    _insert(conn, "2026-07-05T17:00:00", "tl-multi", source="time_log")
    _insert(conn, "2026-07-05T17:00:00+00:00", "tl-multi", source="time_log")
    _insert(conn, "2026-07-05T21:00:00+00:00", "tl-multi", source="time_log")

    normalize_timeline_renderings(conn, dry_run=False)

    assert [r[0] for r in _rows(conn, "tl-multi")] == [
        "2026-07-05T17:00:00+00:00",
        "2026-07-05T21:00:00+00:00",
    ]


def test_the_repair_does_not_merge_across_sources(conn):
    """Two connectors reporting the same moment are two observations."""
    _insert(conn, "2026-06-28T18:00:00+00:00", "tl-1", source="grow_data_file")
    _insert(conn, "2026-06-28T18:00:00+00:00", "tl-1b", source="imessage")

    normalize_timeline_renderings(conn, dry_run=False)

    assert len(_rows(conn)) == 2


def test_the_repair_is_idempotent(conn):
    _insert(conn, "2026-06-28T18:00:00", "tl-1")
    _insert(conn, "2026-06-28T18:00:00+00:00", "tl-1")

    normalize_timeline_renderings(conn, dry_run=False)
    second = normalize_timeline_renderings(conn, dry_run=False)

    assert second["rows_removed"] == 0
    assert len(_rows(conn)) == 1


def test_an_unparseable_timestamp_is_left_alone(conn):
    """Cannot verify it is a duplicate means cannot delete it."""
    _insert(conn, "not a timestamp", "tl-junk")

    normalize_timeline_renderings(conn, dry_run=False)

    assert len(_rows(conn, "tl-junk")) == 1
