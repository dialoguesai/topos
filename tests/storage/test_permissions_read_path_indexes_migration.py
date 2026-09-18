"""Migration 76 adds four read-path indexes and touches no row.

An index is not on the surface an owner review hashes, so no review goes stale and no
pinned digest moves; the step must also be safe on a database that lacks any of the
tables, and must catch a table that legacy DDL or the merge feature creates later.
"""
import sqlite3

from tests.permissions_v2.test_evidence import attest, corpus, decision  # noqa: F401 (fixture)
from topos.storage.db.migrations import ensure_migrations_applied, reset_ensured_connections
from topos.storage.db.migrations.registry import MIGRATIONS
from topos.storage.db.migrations.permissions_read_path_indexes_v1 import (INDEXES, MIGRATION_ID,
    apply_permissions_read_path_indexes_v1_up)

NAMES = {name for _table, _columns, name, _sql in INDEXES}


def index_names(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}


def test_spec_76_is_registered_to_run_on_every_start():
    spec = next(spec for spec in MIGRATIONS if spec.id == MIGRATION_ID)
    assert spec.order == 76 and spec.always_run is True
    assert max(spec.order for spec in MIGRATIONS) == 76


def test_the_indexes_arrive_where_their_tables_are_and_the_step_is_idempotent(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "canonical.db"))
    conn.execute("CREATE TABLE entities (entity_id TEXT PRIMARY KEY, is_self INTEGER NOT NULL DEFAULT 0)")
    conn.execute("CREATE TABLE entity_merge_tombstones (absorbed_entity_id TEXT PRIMARY KEY, merged_into TEXT NOT NULL)")
    conn.execute("CREATE TABLE conversation_messages (message_id TEXT PRIMARY KEY, content TEXT)")
    conn.execute("CREATE TABLE ai_chat_messages (message_id TEXT PRIMARY KEY, content TEXT NOT NULL)")
    conn.execute("INSERT INTO conversation_messages VALUES ('message-1', 'synthetic')")
    conn.commit()
    for _ in range(2):
        apply_permissions_read_path_indexes_v1_up(conn)
    assert NAMES <= index_names(conn)
    assert conn.execute("SELECT * FROM conversation_messages").fetchall() == [("message-1", "synthetic")]
    assert conn.execute("SELECT COUNT(*) FROM wiki_schema_migrations WHERE migration_id=?", (MIGRATION_ID,)).fetchone() == (1,)
    partial = conn.execute("SELECT sql FROM sqlite_master WHERE name='idx_entities_is_self'").fetchone()[0]
    assert partial.endswith("WHERE is_self=1")


def test_a_database_without_the_tables_is_left_alone(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    apply_permissions_read_path_indexes_v1_up(conn)
    assert {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"wiki_schema_migrations"}
    assert not index_names(conn) & NAMES


def test_a_table_without_the_column_is_skipped(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "odd.db"))
    conn.execute("CREATE TABLE entity_merge_tombstones (absorbed_entity_id TEXT PRIMARY KEY)")
    apply_permissions_read_path_indexes_v1_up(conn)
    assert "idx_entity_merge_tombstones_merged_into" not in index_names(conn)


def test_a_table_created_after_the_first_run_gets_its_index_on_the_next(tmp_path):
    """The registry creates `entities`; the message tables and the tombstones arrive later, from legacy DDL and the merge feature."""
    reset_ensured_connections()
    conn = sqlite3.connect(str(tmp_path / "late.db"))
    ensure_migrations_applied(conn, skip_backup=True)
    assert "idx_entities_is_self" in index_names(conn)
    assert not index_names(conn) & (NAMES - {"idx_entities_is_self"})
    conn.execute("CREATE TABLE entity_merge_tombstones (absorbed_entity_id TEXT PRIMARY KEY, merged_into TEXT NOT NULL)")
    conn.execute("CREATE TABLE conversation_messages (message_id TEXT PRIMARY KEY, content TEXT)")
    conn.execute("CREATE TABLE ai_chat_messages (message_id TEXT PRIMARY KEY, content TEXT NOT NULL)")
    conn.commit()
    ensure_migrations_applied(conn, skip_backup=True)
    assert NAMES <= index_names(conn)


def test_an_owner_review_recorded_before_the_indexes_still_qualifies_after_them(corpus):
    attest(corpus)
    assert decision(corpus).verdict == "qualified"
    with sqlite3.connect(corpus[0].path) as conn:
        rows = conn.execute("SELECT * FROM conversation_messages").fetchall()
        apply_permissions_read_path_indexes_v1_up(conn)
        assert NAMES <= index_names(conn)
        assert conn.execute("SELECT * FROM conversation_messages").fetchall() == rows
    assert decision(corpus).verdict == "qualified"
