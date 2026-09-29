import sqlite3

import pytest

from topos.storage.db.migrations.permissions_message_context_indexes_v1 import (
    INDEXES, apply_permissions_message_context_indexes_v1_up as migrate,
)


@pytest.mark.parametrize("table,name,columns", INDEXES)
def test_neighbor_equivalence_scope_order_and_live_updates(table, name, columns):
    conn = sqlite3.connect(":memory:")
    conn.execute(f"CREATE TABLE {table}(source_id TEXT, conversation_id TEXT, dataset_id TEXT, "
                 "event_at TEXT, message_id TEXT PRIMARY KEY, content TEXT)")
    rows = [("src", "thread", "dataset", f"2026-09-{i:02}", f"m{i}", f"invented text {i}") for i in range(1, 25)]
    rows += [("other", "thread", "dataset", "2026-09-12", "other-source", "not in scope"),
             ("src", "other-thread", "dataset", "2026-09-12", "other-thread", "not in scope"),
             ("src", "thread", "dataset", "2026-09-12", "m12b", "same timestamp")]
    if table == "conversation_messages":
        rows += [("src", "thread", "other-dataset", "2026-09-12", "other-dataset", "not in scope")]
    conn.executemany(f"INSERT INTO {table} VALUES (?,?,?,?,?,?)", rows)
    scope = "source_id=? AND conversation_id=?"
    args = ["src", "thread"]
    if table == "conversation_messages":
        scope += " AND dataset_id=?"; args.append("dataset")
    queries = [f"SELECT message_id,content,event_at FROM {table} WHERE {scope} "
               f"AND (event_at,message_id){op}(?,?) ORDER BY event_at {order},message_id {order} LIMIT 2"
               for op, order in [("<", "DESC"), (">", "ASC")]]
    params = (*args, "2026-09-12", "m12")
    expected = [conn.execute(q, params).fetchall() for q in queries]
    migrate(conn); migrate(conn)
    assert [conn.execute(q, params).fetchall() for q in queries] == expected
    for q in queries:
        plan = " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + q, params))
        assert name in plan and "TEMP B-TREE" not in plan
    conn.execute(f"UPDATE {table} SET content='changed' WHERE message_id='m12b'")
    assert conn.execute(queries[1], params).fetchone()[1] == "changed"
    conn.execute(f"DELETE FROM {table} WHERE message_id='m12b'")
    assert conn.execute(queries[1], params).fetchone()[0] == "m13"


def test_late_legacy_table_is_indexed_on_repeat():
    conn = sqlite3.connect(":memory:"); migrate(conn)
    conn.execute("CREATE TABLE ai_chat_messages(source_id TEXT,conversation_id TEXT,event_at TEXT,message_id TEXT)")
    migrate(conn)
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_ai_chat_messages_permission_context'").fetchone()
