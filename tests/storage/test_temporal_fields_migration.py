"""Migration 75 adds the temporal record columns without touching a single existing row.

That is a review-stability requirement, not tidiness. Evidence review hashes
every non-NULL column of a leaf and a fact, so a default or a backfill would put
a new value on every existing row and stale every owner review on the node.
"""
import sqlite3

from tests.permissions_v2.test_evidence import attest, corpus, decision  # noqa: F401 (fixture)
from topos.storage.db.migrations.registry import MIGRATIONS
from topos.storage.db.migrations.temporal_fields_v1 import MIGRATION_ID, apply_temporal_fields_v1_up


def columns(conn, table):
    return {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})")}


def test_spec_75_is_registered_to_run_on_every_start():
    spec = next(spec for spec in MIGRATIONS if spec.id == MIGRATION_ID)
    assert spec.order == 75 and spec.always_run is True


def test_the_columns_arrive_empty_with_no_default_and_the_migration_is_idempotent(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "canonical.db"))
    conn.execute("CREATE TABLE signal_objects (object_id TEXT PRIMARY KEY, payload_json TEXT)")
    conn.execute("CREATE TABLE conversation_messages (message_id TEXT PRIMARY KEY, content TEXT)")
    conn.execute("INSERT INTO signal_objects VALUES ('fact-1', '{}')")
    conn.execute("INSERT INTO conversation_messages VALUES ('message-1', 'synthetic')")
    conn.commit()
    for _ in range(2):
        apply_temporal_fields_v1_up(conn)
    for table, column in (("signal_objects", "temporal_json"), ("conversation_messages", "event_time_json")):
        info = columns(conn, table)[column]
        assert (info[2], info[3], info[4]) == ("TEXT", 0, None)  # type, NOT NULL, default
        assert conn.execute(f"SELECT {column} FROM {table}").fetchall() == [(None,)]
    assert conn.execute("SELECT COUNT(*) FROM wiki_schema_migrations WHERE migration_id=?", (MIGRATION_ID,)).fetchone() == (1,)


def test_a_database_without_either_table_is_left_alone(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "empty.db"))
    apply_temporal_fields_v1_up(conn)
    assert {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"wiki_schema_migrations"}


def test_an_owner_review_recorded_before_the_migration_still_qualifies_after_it(corpus):
    attest(corpus)
    assert decision(corpus).verdict == "qualified"
    with sqlite3.connect(corpus[0].path) as conn:
        assert "temporal_json" not in columns(conn, "signal_objects")
        apply_temporal_fields_v1_up(conn)
        assert "temporal_json" in columns(conn, "signal_objects") and "event_time_json" in columns(conn, "conversation_messages")
    assert decision(corpus).verdict == "qualified"
