"""Asking what a canonical group writes must include its fan-out children.

``_GROUP_TO_TABLE`` answers with ONE table per group, which is right for "what
does a record map to by default" and wrong for "what does this source touch".
Anything using the second question with the first answer walks straight past the
children.

The reprocess count did exactly that: ``group == "journal"`` counted
``journal_entries`` and never saw the ``location_events`` fanned out beside them
— 362 rows on a live node. Those children stayed frozen at whatever spec
produced them while their parents were re-derived, so any embedding-model or
chunking change silently left them behind, permanently and invisibly.

This is the generalization question the workstream opened with: the fix is not
"special-case the journal" but "stop assuming one record makes one row", which
is why the group map is the thing that changed rather than the journal branch.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.disclosure.field_registry import (
    canonical_table_for_group,
    canonical_tables_for_group,
)


def test_the_journal_group_names_its_child_table():
    assert canonical_tables_for_group("journal") == ("journal_entries", "location_events")


def test_the_primary_answer_is_unchanged():
    """`canonical_table_for_group` still answers the question it was asked.

    Record mapping wants the primary table; widening it would send every
    journal record to two tables.
    """
    assert canonical_table_for_group("journal") == "journal_entries"


@pytest.mark.parametrize("group", ["conversations", "ai_messages"])
def test_a_group_that_does_not_fan_out_returns_its_one_table(group):
    assert canonical_tables_for_group(group) == (canonical_table_for_group(group),)


def test_an_unknown_group_is_empty_not_guessed():
    assert canonical_tables_for_group("nope") == ()
    assert canonical_tables_for_group(None) == ()
    assert canonical_tables_for_group("") == ()


def test_the_reprocess_count_includes_children(tmp_path):
    """End to end: the count a reprocess reports must cover the whole source."""
    from topos.ingestion.reprocess import _count_canonical_rows
    from topos.storage.db.migrations import apply_all_migrations

    conn = sqlite3.connect(str(tmp_path / "rp.db"))
    apply_all_migrations(conn)
    conn.execute(
        "INSERT INTO journal_entries (entry_id, content, source_id)"
        " VALUES ('tl-1','x','grow_journal')"
    )
    conn.execute(
        "INSERT INTO location_events (event_id, place_name, source_id, source_record_id)"
        " VALUES ('tl-1-loc','Somewhere','grow_journal','tl-1')"
    )
    conn.commit()

    class _Def:
        canonical_group_id = "journal"
        source_id = "grow_journal"

    try:
        assert _count_canonical_rows(conn, _Def()) == 2, "the child was not counted"
    finally:
        conn.close()


def test_a_missing_child_table_does_not_break_the_count(tmp_path):
    """Minimal databases exist; a count must degrade, not raise."""
    from topos.ingestion.reprocess import _count_canonical_rows
    from topos.storage.db.migrations import apply_all_migrations

    conn = sqlite3.connect(str(tmp_path / "rp2.db"))
    apply_all_migrations(conn)
    conn.execute(
        "INSERT INTO journal_entries (entry_id, content, source_id)"
        " VALUES ('tl-1','x','grow_journal')"
    )
    conn.execute("DROP TABLE location_events")
    conn.commit()

    class _Def:
        canonical_group_id = "journal"
        source_id = "grow_journal"

    try:
        assert _count_canonical_rows(conn, _Def()) == 1
    finally:
        conn.close()
