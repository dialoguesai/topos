"""``first_seen`` and ``last_seen`` must describe the evidence, not the extractor.

Both were stamped with ``datetime('now')`` at mint time, and ``record_mention``
only ever advanced ``last_seen`` upward. So ``first_seen`` meant "when extraction
happened to reach this entity" and ``last_seen`` sat ahead of the newest real
mention — the observation window was fiction at both ends.

Measured on the owner's node 2026-08-27, of 989 mentioned entities:

  * ``first_seen`` late for **835**, worst by 1,191 days — ``plurigrid`` was first
    mentioned 2023-04-11 and stamped 2026-07-15;
  * ``last_seen`` ahead of the latest mention for **699**.

"Entities first seen before 2024" returned nothing on a node holding three years
of history, and the value is read by the dossier the LLM is given, the graph node
property exposed to queries, and the API.

Two halves, gated separately because they fail differently: ``record_mention``
keeps new writes honest, and ``_recount_entity_mentions`` repairs what is already
stored — the latter placed there so every scrub and rebuild corrects the window
without a migration anyone has to remember.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.entities.resolver import EntityResolver
from topos.features.lifecycle.derived_scrub import _recount_entity_mentions

EARLY = "2023-04-11T09:00:00"
MID = "2025-01-02T09:00:00"
LATE = "2026-07-15T09:00:00"


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "window.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _window(conn, entity_id):
    return conn.execute(
        "SELECT first_seen, last_seen FROM entities WHERE entity_id=?", (entity_id,)
    ).fetchone()


# ------------------------------------------------ new writes stay honest


def test_a_later_mention_does_not_move_first_seen_forward(conn):
    r = EntityResolver(conn)
    eid, _ = r.resolve("Plurigrid", entity_type="org", record_id="r1")

    r.record_mention(eid, record_id="r1", surface_text="Plurigrid", event_at=EARLY)
    conn.commit()
    r.record_mention(eid, record_id="r2", surface_text="Plurigrid", event_at=LATE)
    conn.commit()

    first, last = _window(conn, eid)
    assert first == EARLY
    assert last == LATE


def test_an_earlier_mention_pulls_first_seen_back(conn):
    """The regression. Arrival order is not chronological order.

    A 2023 mention almost always reaches the spine AFTER a 2026 one, because
    extraction runs newest-first on a backfill.
    """
    r = EntityResolver(conn)
    eid, _ = r.resolve("Plurigrid", entity_type="org", record_id="r1")

    r.record_mention(eid, record_id="r1", surface_text="Plurigrid", event_at=LATE)
    conn.commit()
    assert _window(conn, eid)[0] == LATE

    r.record_mention(eid, record_id="r2", surface_text="Plurigrid", event_at=EARLY)
    conn.commit()

    assert _window(conn, eid)[0] == EARLY


def test_an_undated_mention_does_not_clobber_the_window(conn):
    """MIN over the empty string would beat any real date."""
    r = EntityResolver(conn)
    eid, _ = r.resolve("Plurigrid", entity_type="org", record_id="r1")
    r.record_mention(eid, record_id="r1", surface_text="Plurigrid", event_at=EARLY)
    conn.commit()

    r.record_mention(eid, record_id="r2", surface_text="Plurigrid", event_at=None)
    conn.commit()

    assert _window(conn, eid)[0] == EARLY


# ------------------------------------------------------- stored data is repaired


def _seed_wrong_window(conn):
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
        " mention_count, is_self, first_seen, last_seen) VALUES (?,?,?,?,?,0,?,?)",
        ("ent-wrong", "org", "Plurigrid", "plurigrid", 0, LATE, LATE),
    )
    for i, when in enumerate((EARLY, MID)):
        conn.execute(
            "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id,"
            " canonical_table, surface_text, event_at) VALUES (?,?,?,?,?,?,?)",
            (f"m{i}", "ent-wrong", f"r{i}", "github", "activity_events", "Plurigrid", when),
        )
    conn.commit()


def test_the_recount_repairs_a_stored_window(conn):
    _seed_wrong_window(conn)
    assert _window(conn, "ent-wrong") == (LATE, LATE), "fixture must start wrong"

    _recount_entity_mentions(conn)
    conn.commit()

    first, last = _window(conn, "ent-wrong")
    assert first == EARLY, "first_seen must fall back to the earliest evidence"
    assert last == MID, "last_seen must not sit ahead of the newest evidence"


def test_the_recount_still_fixes_mention_count(conn):
    """Control: the added repair must not displace what this function already did."""
    _seed_wrong_window(conn)

    _recount_entity_mentions(conn)
    conn.commit()

    assert (
        conn.execute(
            "SELECT mention_count FROM entities WHERE entity_id='ent-wrong'"
        ).fetchone()[0]
        == 2
    )


def test_a_mentionless_hub_keeps_its_dates(conn):
    """A materialized vertex is not a sighting and has no observation window.

    Goals, topics and conversation hubs are minted from other tables and are
    mention-less by nature. Inventing a window for them from no evidence would be
    a different lie, so they are left alone.
    """
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
        " mention_count, is_self, first_seen, last_seen) VALUES (?,?,?,?,0,0,?,?)",
        ("goal_hub", "goal", "Ship the node", "ship the node", LATE, LATE),
    )
    conn.commit()

    _recount_entity_mentions(conn)
    conn.commit()

    assert _window(conn, "goal_hub") == (LATE, LATE)


def test_the_window_is_idempotent_across_repairs(conn):
    _seed_wrong_window(conn)

    _recount_entity_mentions(conn)
    conn.commit()
    once = _window(conn, "ent-wrong")
    _recount_entity_mentions(conn)
    conn.commit()

    assert _window(conn, "ent-wrong") == once
