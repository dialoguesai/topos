"""Migration 78, permissions_fact_lineage_keys_v1: registration, idempotence, rebuild on drift."""
from __future__ import annotations

import sqlite3

from tests.permissions_v2 import production_corpus as pc
from topos.storage.db.migrations import permissions_fact_lineage_keys_v1 as lk
from topos.storage.db.migrations.registry import MIGRATIONS


def test_spec_78_is_the_head_and_runs_on_every_start():
    spec = next(spec for spec in MIGRATIONS if spec.id == lk.MIGRATION_ID)
    assert spec.order == 78 and spec.always_run is True
    assert max(spec.order for spec in MIGRATIONS) == 78
    assert 77 not in {spec.order for spec in MIGRATIONS}  # reserved for the D8 reach witness


def test_a_fresh_node_is_stamped_78_with_every_key_object(tmp_path):
    conn = sqlite3.connect(tmp_path / "canonical.db")
    pc.production_schema(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 78
    assert lk.installed(conn)


def test_rerunning_is_a_no_op_that_keeps_the_keys(tmp_path):
    conn = sqlite3.connect(tmp_path / "canonical.db")
    pc.production_schema(conn)
    conn.execute("INSERT INTO signal_objects(object_id, signal_dimension, object_type, object_key, payload_json, "
                 "source_refs_json, valid_from, created_at, updated_at) VALUES ('f1','p','fact','k','{}',"
                 "'[{\"record_id\":\"imessage:5\"}]','t','t','t')")
    before = conn.execute("SELECT * FROM permissions_v2_fact_ref_keys").fetchall()
    schema = conn.execute("PRAGMA schema_version").fetchone()
    lk.apply_permissions_fact_lineage_keys_v1_up(conn)
    assert conn.execute("SELECT * FROM permissions_v2_fact_ref_keys").fetchall() == before == [("f1", "imessage:5")]
    assert conn.execute("PRAGMA schema_version").fetchone() == schema


def test_an_altered_table_is_rebuilt_with_every_row_keyed(tmp_path):
    conn = sqlite3.connect(tmp_path / "canonical.db")
    pc.production_schema(conn)
    conn.execute("DROP TRIGGER fact_lineage_keys_ad")
    conn.execute("INSERT INTO signal_objects(object_id, signal_dimension, object_type, object_key, payload_json, "
                 "source_refs_json, valid_from, created_at, updated_at) VALUES ('f1','p','fact','k','{}',"
                 "'[{\"record_id\":\"imessage:5\"}]','t','t','t')")
    conn.execute("DELETE FROM signal_objects WHERE object_id='f1'")  # its keys outlive it while the trigger is gone
    assert conn.execute("SELECT count(*) FROM permissions_v2_fact_key_rows").fetchone()[0] == 1
    lk.apply_permissions_fact_lineage_keys_v1_up(conn)
    assert lk.installed(conn) and conn.execute("SELECT count(*) FROM permissions_v2_fact_key_rows").fetchone()[0] == 0
