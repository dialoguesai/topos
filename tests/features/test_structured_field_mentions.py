"""A record that names an entity in its own column must carry that mention.

NER reads ``content``. A journal entry's place lives in ``place_name`` — a
declared column — so the record naming the place got no mention for it, while the
fan-out child minted from that same column got one instead.

Measured on the live node 2026-08-27: 333 of 362 ``journal_entries`` rows carry a
non-empty ``place_name`` and have no place mention of their own; place mentions
sit 178 on children against 648 on parents.

This is what replaces widening the black hole. Protecting a place must not hide a
day's journal entry because a SIBLING row named it — that was decided against.
But these records are not siblings of the evidence, they contain it, so
attributing the mention to the record whose column holds it blocks the parent
*because the parent names it*. A record that never names the entity is still
never blocked, which is the property `test_a_record_that_does_not_name_it_is_not
_blocked` pins.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.resolver import EntityResolver
from topos.features.entities.structured_fields import (
    STRUCTURED_ENTITY_FIELDS,
    record_structured_mentions,
    structured_fields_for,
)

PLACE = "Northgate- The Foundry"


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "structured.db"))
    apply_all_migrations(c)
    c.execute(
        "INSERT INTO journal_entries (entry_id, content, place_name, source_id)"
        " VALUES (?,?,?,?)",
        ("tl-1", "Deep work on the eval set with Ada.", PLACE, "grow_journal"),
    )
    c.commit()
    yield c
    c.close()


def _parent_msg(**over):
    base = {
        "record_id": "tl-1",
        "_table": "journal_entries",
        "content": "Deep work on the eval set with Ada.",
        "place_name": PLACE,
        "source_id": "grow_journal",
        "event_at": "2026-07-06T19:05:00",
    }
    base.update(over)
    return base


def _mentions(conn, record_id):
    return {
        (r[0], r[1])
        for r in conn.execute(
            "SELECT e.canonical_name, e.entity_type FROM entity_mentions m"
            " JOIN entities e ON e.entity_id = m.entity_id WHERE m.record_id=?",
            (record_id,),
        )
    }


# ------------------------------------------------------------------ the fix


def test_the_parent_gets_a_mention_for_the_place_it_names(conn):
    resolver = EntityResolver(conn)
    assert _mentions(conn, "tl-1") == set(), "fixture must start with no mentions"

    by_record = record_structured_mentions(conn, resolver, [_parent_msg()])
    conn.commit()

    assert by_record["tl-1"], "the parent must be credited with the place"
    assert (PLACE, "place") in _mentions(conn, "tl-1")


def test_a_record_that_does_not_name_it_is_not_blocked(conn):
    """The no-widen guarantee, stated as a test.

    A second journal entry with a DIFFERENT place must not acquire the first
    one's place. If this ever fails, attribution has drifted into unit-scoping.
    """
    conn.execute(
        "INSERT INTO journal_entries (entry_id, content, place_name, source_id)"
        " VALUES (?,?,?,?)",
        ("tl-2", "A different day entirely.", "Mill Pond Trail", "grow_journal"),
    )
    conn.commit()
    resolver = EntityResolver(conn)

    record_structured_mentions(
        conn,
        resolver,
        [_parent_msg(), _parent_msg(record_id="tl-2", place_name="Mill Pond Trail")],
    )
    conn.commit()

    assert (PLACE, "place") in _mentions(conn, "tl-1")
    assert (PLACE, "place") not in _mentions(conn, "tl-2")
    assert ("Mill Pond Trail", "place") in _mentions(conn, "tl-2")


def test_an_empty_column_records_nothing(conn):
    resolver = EntityResolver(conn)

    by_record = record_structured_mentions(
        conn, resolver, [_parent_msg(record_id="tl-3", place_name="   ")]
    )

    assert by_record == {}


def test_a_table_with_no_declared_fields_is_untouched(conn):
    resolver = EntityResolver(conn)

    by_record = record_structured_mentions(
        conn,
        resolver,
        [_parent_msg(_table="conversation_messages", record_id="msg-1")],
    )

    assert by_record == {}
    assert structured_fields_for("conversation_messages") == ()


def test_structured_mentions_are_marked_as_declared_not_guessed(conn):
    """Confidence separates a column from an NER span in the same table."""
    resolver = EntityResolver(conn)

    record_structured_mentions(conn, resolver, [_parent_msg()])
    conn.commit()

    confidence = conn.execute(
        "SELECT confidence FROM entity_mentions WHERE record_id='tl-1'"
    ).fetchone()[0]
    assert confidence == 1.0


def test_it_is_idempotent(conn):
    """Re-enrichment must not multiply mentions."""
    resolver = EntityResolver(conn)

    record_structured_mentions(conn, resolver, [_parent_msg()])
    conn.commit()
    first = conn.execute(
        "SELECT COUNT(*) FROM entity_mentions WHERE record_id='tl-1'"
    ).fetchone()[0]

    record_structured_mentions(conn, resolver, [_parent_msg()])
    conn.commit()
    second = conn.execute(
        "SELECT COUNT(*) FROM entity_mentions WHERE record_id='tl-1'"
    ).fetchone()[0]

    assert second == first


# --------------------------------------------------- the black-hole consequence


def test_a_protected_place_now_blocks_the_record_that_names_it(conn):
    """The leak the no-widen decision left open, closed the narrow way.

    Before: the mention lived only on the derived child, so blocking the place hid
    a two-word stub and left the entry describing the evening fully visible.
    """
    from topos.features.lifecycle.blackhole import BlackholeStore
    from topos.features.lifecycle.blackhole_guard import guard_for

    resolver = EntityResolver(conn)
    record_structured_mentions(conn, resolver, [_parent_msg()])
    conn.commit()

    entity_id = conn.execute(
        "SELECT entity_id FROM entity_mentions WHERE record_id='tl-1'"
    ).fetchone()[0]
    BlackholeStore(conn).blackhole_entity(entity_ref=entity_id)
    conn.commit()

    blocked = guard_for(conn, mcp_source="claude_desktop").blocked_record_ids()

    assert "tl-1" in blocked, (
        "the journal entry names the protected place in its own column and must be "
        "blocked on that evidence"
    )


# ------------------------------------------------------------------ the contract


def test_only_whole_cell_entity_columns_are_declared():
    """Free-text columns must never be added: the whole cell is asserted as one name.

    ``content`` in this map would mint an entity per journal entry named after the
    entry's prose. The declaration is the guard, so it is worth pinning.
    """
    forbidden = {"content", "content_rendered", "body", "summary_text", "goal", "accomplished"}
    offenders = [
        f"{table}.{column}"
        for table, fields in STRUCTURED_ENTITY_FIELDS.items()
        for column, _type in fields
        if column in forbidden
    ]
    assert offenders == [], f"free-text columns declared as whole-cell entities: {offenders}"
