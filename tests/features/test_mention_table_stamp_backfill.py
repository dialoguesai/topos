"""A mention without a table stamp is invisible, not withheld.

``entity_mentions.canonical_table`` is what every table-scoped read and delete
travels along. Without it a mention is missed by a table-scoped grant, a table
purge and a disclosure sweep alike — and the failure is silent, because nothing
reports a row it never found.

Measured on a live node 2026-08-27: 619 unstamped mentions from
``browser_visits`` (566), ``github_activity`` (52) and ``chatgpt`` (1). None
came from the journal fan-out, so the ingest-time stamp correction could not
reach them; these connectors simply never set the field.

The stamp is RECOVERED, not recomputed — the mention's own ``record_id``
resolves against exactly one canonical table. All 619 resolved on that node, 618
to ``activity_events``. No re-extraction, no model.

Ambiguity and absence both fail toward leaving the row alone. An invented stamp
routes a read to the wrong table, which is worse than the absence it repairs.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.storage.db.migrations.mention_table_stamp_backfill_v1 import (
    backfill_mention_table_stamps,
)


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "stamp.db"))
    apply_all_migrations(c)
    c.execute("DELETE FROM entity_mentions")
    c.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
        " mention_count, is_self) VALUES ('ent-1','person','Ada','ada',1,0)"
    )
    c.execute(
        "INSERT INTO journal_entries (entry_id, content, source_id)"
        " VALUES ('tl-1','hello','grow_journal')"
    )
    c.execute(
        "INSERT INTO activity_events (event_id, source_id, occurred_at)"
        " VALUES ('ev-1','browser_visits','2026-07-01T00:00:00Z')"
    )
    c.commit()
    yield c
    c.close()


def _mention(conn, mention_id, record_id, table=""):
    conn.execute(
        "INSERT INTO entity_mentions (mention_id, entity_id, record_id, surface_text,"
        " canonical_table) VALUES (?,?,?,?,?)",
        (mention_id, "ent-1", record_id, "Ada", table),
    )
    conn.commit()


def _stamp(conn, mention_id):
    return conn.execute(
        "SELECT canonical_table FROM entity_mentions WHERE mention_id=?", (mention_id,)
    ).fetchone()[0]


def test_a_stamp_is_recovered_from_the_record(conn):
    _mention(conn, "m1", "ev-1")

    counts = backfill_mention_table_stamps(conn)
    conn.commit()

    assert counts["stamped"] == 1
    assert _stamp(conn, "m1") == "activity_events"


def test_the_right_table_is_chosen_among_several(conn):
    _mention(conn, "m2", "tl-1")

    backfill_mention_table_stamps(conn)
    conn.commit()

    assert _stamp(conn, "m2") == "journal_entries"


def test_an_already_stamped_mention_is_untouched(conn):
    _mention(conn, "m3", "ev-1", table="journal_entries")

    counts = backfill_mention_table_stamps(conn)
    conn.commit()

    assert counts["scanned"] == 0
    assert _stamp(conn, "m3") == "journal_entries", "an existing stamp is not second-guessed"


def test_an_unresolvable_record_is_left_unstamped(conn):
    """Absence beats invention: a wrong stamp routes reads to the wrong table."""
    _mention(conn, "m4", "does-not-exist")

    counts = backfill_mention_table_stamps(conn)
    conn.commit()

    assert counts["unresolved"] == 1
    assert _stamp(conn, "m4") == ""


def test_an_ambiguous_id_is_left_unstamped(conn):
    """Two canonical tables claiming one id is its own identity problem."""
    conn.execute(
        "INSERT INTO calendar_events (event_id, source_id, starts_at)"
        " VALUES ('ev-1','gcal','2026-07-01T00:00:00Z')"
    )
    conn.commit()
    _mention(conn, "m5", "ev-1")

    counts = backfill_mention_table_stamps(conn)
    conn.commit()

    assert counts["ambiguous"] == 1
    assert _stamp(conn, "m5") == ""


def test_the_backfill_dry_runs(conn):
    _mention(conn, "m6", "ev-1")

    counts = backfill_mention_table_stamps(conn, dry_run=True)
    conn.commit()

    assert counts["stamped"] == 1
    assert _stamp(conn, "m6") == ""


def test_the_backfill_is_idempotent(conn):
    _mention(conn, "m7", "ev-1")

    backfill_mention_table_stamps(conn)
    conn.commit()

    assert backfill_mention_table_stamps(conn)["scanned"] == 0


def test_it_runs_as_a_migration_on_a_fresh_database(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "fresh.db"))
    try:
        apply_all_migrations(c)
        applied = {r[0] for r in c.execute("SELECT migration_id FROM wiki_schema_migrations")}
        assert "mention_table_stamp_backfill_v1" in applied
    finally:
        c.close()
