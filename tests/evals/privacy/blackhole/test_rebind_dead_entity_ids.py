"""A black hole must keep pointing at the entity it protects.

A black hole stores the entity_id it was created against. If that entity is
later reaped and the name is re-extracted from a record, the spine mints a NEW
id — and the black hole goes on excluding a row that no longer exists while the
live entity carrying the withdrawn name matches nothing.

Found on the owner's node 2026-08-27: one of three black holes pointed at a
deleted id while an entity with the identical canonical name sat in the spine
under a fresh one. The name terms still covered it, so this narrowed protection
rather than removing it — but the id join is the primary filter and it was
silently covering nothing.

The repair only ever re-points to an entity whose NAME matches, on either the
stored normalization or one freshly derived from ``canonical_name`` (those
disagree for rows written before ``normalize_entity_name`` last changed). It
never invents a binding.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.lifecycle.blackhole import BlackholeStore, normalize_entity_name


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "bh.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _entity(conn, entity_id, name, mentions=1):
    conn.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
        " mention_count, is_self) VALUES (?,?,?,?,?,0)",
        (entity_id, "place", name, normalize_entity_name(name), mentions),
    )
    conn.commit()


def _blackhole(conn, ref, entity_id, name, normalized=None):
    conn.execute(
        "INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name,"
        " canonical_name, aliases_json, processing_tier, rebuild_state, created_at,"
        " updated_at, created_by) VALUES (?,?,?,?,'[]','secure','complete',"
        " datetime('now'), datetime('now'), 'owner')",
        (ref, entity_id, normalized or normalize_entity_name(name), name),
    )
    conn.commit()


def _bound_id(conn, ref):
    return conn.execute(
        "SELECT entity_id FROM entity_blackholes WHERE blackhole_id=?", (ref,)
    ).fetchone()[0]


def test_a_dead_id_is_repointed_to_the_live_entity(conn):
    """The live case: reaped, then re-extracted under a new id."""
    _entity(conn, "ent-new", "Old Harbor- Rey's Place")
    _blackhole(conn, "bh-1", "ent-dead", "Old Harbor- Rey's Place")

    assert BlackholeStore(conn).rebind_dead_entity_ids() == 1
    assert _bound_id(conn, "bh-1") == "ent-new"


def test_a_live_id_is_left_alone(conn):
    _entity(conn, "ent-live", "Old Harbor- Rey's Place")
    _blackhole(conn, "bh-2", "ent-live", "Old Harbor- Rey's Place")

    assert BlackholeStore(conn).rebind_dead_entity_ids() == 0
    assert _bound_id(conn, "bh-2") == "ent-live"


def test_a_dead_id_with_no_matching_name_is_not_invented(conn):
    """Nothing to bind to means leave it dead — a wrong binding would exclude
    an unrelated entity from every read."""
    _entity(conn, "ent-other", "Central Library")
    _blackhole(conn, "bh-3", "ent-dead", "Old Harbor- Rey's Place")

    assert BlackholeStore(conn).rebind_dead_entity_ids() == 0
    assert _bound_id(conn, "bh-3") == "ent-dead"


def test_a_stale_stored_normalization_still_matches(conn):
    """`normalize_entity_name` changed after some rows were written.

    The stored value is frozen at write time, so matching on it alone would
    miss the entity whose name normalizes differently today.
    """
    _entity(conn, "ent-new", "Old Harbor- Rey's Place")
    _blackhole(
        conn, "bh-4", "ent-dead", "Old Harbor- Rey's Place",
        normalized="old harbor- rey s place",  # the pre-change normalization
    )

    assert BlackholeStore(conn).rebind_dead_entity_ids() == 1
    assert _bound_id(conn, "bh-4") == "ent-new"


def test_the_most_mentioned_match_wins(conn):
    """Two entities can share a normalized name after a bad split."""
    _entity(conn, "ent-small", "Old Harbor- Rey's Place", mentions=1)
    _entity(conn, "ent-big", "Old Harbor- Rey's Place", mentions=42)
    _blackhole(conn, "bh-5", "ent-dead", "Old Harbor- Rey's Place")

    BlackholeStore(conn).rebind_dead_entity_ids()

    assert _bound_id(conn, "bh-5") == "ent-big"


def test_the_rebound_id_reaches_the_exclusion_set(conn):
    """The point of the repair: the id filter must cover the live entity."""
    _entity(conn, "ent-new", "Old Harbor- Rey's Place")
    _blackhole(conn, "bh-6", "ent-dead", "Old Harbor- Rey's Place")
    store = BlackholeStore(conn)

    assert "ent-new" not in store.blackholed_entity_ids()
    store.rebind_dead_entity_ids()

    assert "ent-new" in store.blackholed_entity_ids()


def test_rebinding_is_idempotent(conn):
    _entity(conn, "ent-new", "Old Harbor- Rey's Place")
    _blackhole(conn, "bh-7", "ent-dead", "Old Harbor- Rey's Place")
    store = BlackholeStore(conn)

    store.rebind_dead_entity_ids()
    assert store.rebind_dead_entity_ids() == 0


def test_an_empty_id_is_not_this_functions_job(conn):
    """`bind_entity_id` covers pre-emptive protection; this covers dead ids.

    Keeping them separate matters: an empty id means "protected before the
    entity existed", which is a different state from "the entity it protected
    was deleted" and must not be silently merged.
    """
    _entity(conn, "ent-new", "Old Harbor- Rey's Place")
    _blackhole(conn, "bh-8", "", "Old Harbor- Rey's Place")

    assert BlackholeStore(conn).rebind_dead_entity_ids() == 0
