"""derivation_provenance_v1 (registry order 64) — the columns pack facts are written with.

Also pins the rule the migration exists to enforce: schema comes from the registry, and a
feature that finds it missing FAILS rather than creating it (the ad-hoc ALTER TABLE this
replaced is the same shape as the incident that stamped a live DB past its engine).
"""
import sqlite3

import pytest

from topos.storage.db.migrations.derivation_provenance_v1 import (
    MIGRATION_ID,
    apply_derivation_provenance_v1_down,
    apply_derivation_provenance_v1_up,
)
from topos.storage.db.migrations.registry import MIGRATIONS


def _db(with_signal_objects: bool = True) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE wiki_schema_migrations (migration_id TEXT PRIMARY KEY)")
    if with_signal_objects:
        conn.execute(
            """CREATE TABLE signal_objects (
                 object_id TEXT PRIMARY KEY, object_type TEXT, object_key TEXT,
                 payload_json TEXT, valid_from TEXT, valid_to TEXT)"""
        )
    return conn


def _columns(conn) -> set:
    return {r[1] for r in conn.execute("PRAGMA table_info(signal_objects)")}


def test_adds_the_three_provenance_columns():
    conn = _db()
    apply_derivation_provenance_v1_up(conn)
    assert {"ontology_id", "ontology_version", "altitude"} <= _columns(conn)


def test_creates_the_pack_lookup_index():
    conn = _db()
    apply_derivation_provenance_v1_up(conn)
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='signal_objects'")}
    assert "idx_signal_objects_ontology" in idx


def test_is_idempotent():
    conn = _db()
    apply_derivation_provenance_v1_up(conn)
    apply_derivation_provenance_v1_up(conn)  # re-run must not raise on existing columns
    assert len([c for c in _columns(conn) if c == "ontology_id"]) == 1


def test_stamps_the_ledger():
    conn = _db()
    apply_derivation_provenance_v1_up(conn)
    stamped = conn.execute(
        "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?", (MIGRATION_ID,)
    ).fetchone()
    assert stamped


def test_tolerates_a_missing_table():
    conn = _db(with_signal_objects=False)
    apply_derivation_provenance_v1_up(conn)  # harness bootstrap: no table, no crash
    assert conn.execute(
        "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?", (MIGRATION_ID,)
    ).fetchone()


def test_down_drops_index_and_unstamps():
    conn = _db()
    apply_derivation_provenance_v1_up(conn)
    apply_derivation_provenance_v1_down(conn)
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='signal_objects'")}
    assert "idx_signal_objects_ontology" not in idx
    assert not conn.execute(
        "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?", (MIGRATION_ID,)
    ).fetchone()


def test_registered_at_order_64_and_orders_are_unique():
    by_id = {m.id: m.order for m in MIGRATIONS}
    assert by_id[MIGRATION_ID] == 64
    assert by_id["enrichment_record_progress_v1"] == 63, "the held migration ships in this release"
    orders = [m.order for m in MIGRATIONS]
    assert len(orders) == len(set(orders)), "migration orders must be unique"
    assert orders == sorted(orders), "MIGRATIONS must stay in ascending order"


def test_writer_refuses_an_unmigrated_database():
    """The rule the migration enforces: features read schema, they never create it."""
    from topos.features.derivation.writer import DerivationSchemaMissing, DerivationWriter

    conn = _db()  # signal_objects WITHOUT the provenance columns
    with pytest.raises(DerivationSchemaMissing):
        DerivationWriter(conn, model="test-model")
