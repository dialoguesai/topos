"""The coverage repair runs by itself (any-to-any N2; inventory E1, surprise 16).

A node whose protection clock was installed before it had its people tables, and that gained them later (a
migration creates them when the database opens), refused every v2 read until ``resync_identity_coverage`` was
run by hand: nothing called it. The runtime now runs it each time it loads, and a bind before it installs or
verifies the clock (``tests/permissions_v2/test_self_bind.py``). It may only ever watch a table that appeared:
a clock that is damaged in any other way is refused exactly as before and left exactly as it is.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from tests.permissions_v2.test_node_protocol import protocol  # noqa: F401
from tests.permissions_v2.test_protocol_runtime import configured  # noqa: F401
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.protection_clock import (TABLE, clock_state, identity_coverage,
                                                   repair_identity_coverage)
from topos.permissions_v2.runtime import load_runtime
from topos.storage.db.migrations.wiki_entities_v1 import apply_wiki_entities_v1_up


def clock_rows(path):
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        triggers = sorted(conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' "
                                       "AND name LIKE 'permissions\\_v2\\_%' ESCAPE '\\'").fetchall())
        state = conn.execute(f"SELECT * FROM {TABLE}").fetchall()
    return triggers, state


def test_a_node_that_gains_its_people_tables_watches_them_from_its_next_load(configured):
    _config, path, fixture = configured
    canonical = fixture[0].canonical_database
    with closing(sqlite3.connect(canonical)) as conn:
        assert identity_coverage(conn) == ()             # the clock went in before the node had them
        before = clock_state(conn)
        apply_wiki_entities_v1_up(conn)                  # the migration that brings them
        conn.commit()
    with closing(sqlite3.connect(canonical)) as conn, pytest.raises(PolicyError, match="protection_clock_unavailable"):
        clock_state(conn)                                # every read refused, until now by hand
    runtime = load_runtime(path, active_database=canonical)
    runtime.close()
    with closing(sqlite3.connect(canonical)) as conn:
        assert clock_state(conn) == (before[0], before[1] + 1)
        assert identity_coverage(conn) == ("entities", "entity_mentions")
    settled = clock_rows(canonical)
    load_runtime(path, active_database=canonical).close()   # the next load has nothing to do
    assert clock_rows(canonical) == settled


@pytest.mark.parametrize("damage", [
    "DROP TRIGGER permissions_v2_owner_only_records_insert",
    "DROP TRIGGER permissions_v2_identity_attestations_insert",
], ids=["floor_trigger_lost", "consent_trigger_lost"])
def test_a_damaged_clock_is_refused_as_before_and_left_exactly_as_it_is(configured, damage):
    _config, path, fixture = configured
    canonical = fixture[0].canonical_database
    with closing(sqlite3.connect(canonical)) as conn:
        apply_wiki_entities_v1_up(conn)                  # a coverage change as well, so a repair is tempting
        conn.execute(damage)
        conn.commit()
    before = clock_rows(canonical)
    with pytest.raises(PolicyError, match="protection_clock_unavailable"):
        load_runtime(path, active_database=canonical)
    assert clock_rows(canonical) == before
    assert repair_identity_coverage(canonical, owner_id="owner-1") is None
    assert clock_rows(canonical) == before


def test_the_repair_never_acts_for_another_owner(protocol):
    canonical = protocol[0].canonical_database
    with closing(sqlite3.connect(canonical)) as conn:
        apply_wiki_entities_v1_up(conn)
        conn.commit()
    before = clock_rows(canonical)
    assert repair_identity_coverage(canonical, owner_id="owner-2") is None
    assert clock_rows(canonical) == before


def test_the_repair_does_nothing_on_a_complete_clock_or_where_there_is_none(protocol, tmp_path):
    canonical = protocol[0].canonical_database
    before = clock_rows(canonical)
    assert repair_identity_coverage(canonical, owner_id="owner-1") is None
    assert clock_rows(canonical) == before
    bare = tmp_path / "no-clock.db"
    with closing(sqlite3.connect(bare)) as conn:
        conn.execute("CREATE TABLE engine_config (key TEXT PRIMARY KEY, value TEXT)")
        conn.commit()
    assert repair_identity_coverage(bare, owner_id="owner-1") is None
    with closing(sqlite3.connect(bare)) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'permissions%'").fetchall() == []
    missing = tmp_path / "missing.db"
    assert repair_identity_coverage(missing, owner_id="owner-1") is None and not missing.exists()
