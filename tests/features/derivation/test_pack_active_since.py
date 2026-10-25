"""Activation dates describe the current enabled period, not catalog updates."""
import sqlite3

import pytest

from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry
from topos.features.derivation.surfaces import list_packs, set_pack_enabled, resolve_pack_offer


@pytest.fixture
def conn():
    c = sqlite3.connect(':memory:')
    c.executescript("""
      CREATE TABLE pack_registry (pack_id TEXT PRIMARY KEY, version TEXT, enabled INTEGER,
        disclosure_default TEXT, last_run_at TEXT, updated_at TEXT);
      INSERT INTO pack_registry VALUES ('health.mental', 'old', 1, 'owner_only', NULL, '2020-01-01');
      CREATE TABLE signal_objects (object_type TEXT, ontology_id TEXT, valid_to TEXT);
      CREATE TABLE fact_conflicts (predicate TEXT, status TEXT);
      CREATE TABLE pack_offers (offer_id TEXT PRIMARY KEY, pack_id TEXT, kind TEXT,
        status TEXT, stats_json TEXT, created_at TEXT, updated_at TEXT);
    """)
    seed_pack_registry(c, bundled_pack_dir())
    yield c
    c.close()


def date(c, pid='behavior.habits'):
    return c.execute('SELECT active_since FROM pack_registry WHERE pack_id=?', (pid,)).fetchone()[0]


def test_legacy_unknown_and_new_default_activation(conn):
    assert date(conn, 'health.mental') is None
    assert date(conn, 'health.physical') is not None
    packs = {p['pack_id']: p for p in list_packs(conn)['packs']}
    assert packs['health.mental']['active_since'] is None
    assert packs['health.physical']['active_since'] == date(conn, 'health.physical')


def test_enable_retry_refresh_disable_reenable(conn):
    assert date(conn) is None
    set_pack_enabled(conn, 'behavior.habits', True)
    assert date(conn)
    conn.execute("UPDATE pack_registry SET active_since='2025-01-01 00:00:00' WHERE pack_id='behavior.habits'")
    set_pack_enabled(conn, 'behavior.habits', True)
    seed_pack_registry(conn, bundled_pack_dir())
    assert date(conn) == '2025-01-01 00:00:00'
    set_pack_enabled(conn, 'behavior.habits', False)
    assert date(conn) is None
    set_pack_enabled(conn, 'behavior.habits', True)
    assert date(conn) and date(conn) != '2025-01-01 00:00:00'


def test_accepted_offers_record_and_clear_activation(conn):
    for oid, kind in [('on', 'enable_offer'), ('off', 'disable_nudge')]:
        conn.execute("INSERT INTO pack_offers VALUES (?, 'behavior.habits', ?, 'pending', '{}', NULL, NULL)", (oid, kind))
    resolve_pack_offer(conn, 'on', 'accept')
    assert date(conn)
    resolve_pack_offer(conn, 'off', 'accept')
    assert date(conn) is None
