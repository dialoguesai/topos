"""Migration ``ai_coding_sessions_v1``: ``ai_chat_messages.share_excluded`` and ``ai_chat_message_facets``.

The column is additive with a default of 0, so every existing row reads as shareable; the facets
table is owner-only storage keyed by message id. The step is always-run because the ai_chat
tables are created lazily by the first import, possibly after the migrations ran once.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.storage.db.migrations import MIGRATIONS, apply_all_migrations
from topos.storage.db.migrations.ai_coding_sessions_v1 import (
    FACETS_TABLE,
    MIGRATION_ID,
    apply_ai_coding_sessions_v1_up,
    ensure_facets_table,
    ensure_share_excluded_column,
)


def _columns(conn, table):
    return {row[1]: row for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_the_migration_is_registered_and_always_run() -> None:
    spec = next(m for m in MIGRATIONS if m.id == MIGRATION_ID)
    assert spec.always_run is True
    assert spec.order == 85, "83 is the AI-chat titles step and 84 is L4's file cursors (coordinator, 15 Oct)"
    assert len({m.order for m in MIGRATIONS}) == len(MIGRATIONS), "every order is unique"


def test_the_column_is_added_to_a_table_that_lacks_it_with_default_zero(tmp_path) -> None:
    """The ALTER path: a table created by the canonical DDL, which names no such column."""
    conn = sqlite3.connect(str(tmp_path / "m.db"))
    conn.execute(
        "CREATE TABLE ai_chat_messages (message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, sender_type TEXT NOT NULL,"
        " sender_id TEXT, event_at TEXT NOT NULL, content TEXT NOT NULL, content_rendered TEXT, metadata_json TEXT,"
        " sequence INTEGER NOT NULL DEFAULT 0, source_id TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, source_id)"
        " VALUES ('x:1', 'x:s', 'human', '2026-01-01T00:00:00Z', 'hello', 'claude_code_sessions')"
    )
    assert "share_excluded" not in _columns(conn, "ai_chat_messages")
    apply_ai_coding_sessions_v1_up(conn)
    column = _columns(conn, "ai_chat_messages")["share_excluded"]
    assert column[2].upper() == "INTEGER" and column[3] == 1 and column[4] == "0"
    assert conn.execute("SELECT share_excluded FROM ai_chat_messages").fetchone()[0] == 0
    apply_ai_coding_sessions_v1_up(conn)  # idempotent
    assert ensure_share_excluded_column(conn) is True
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_ai_chat_messages_share_excluded'").fetchone()


def test_the_column_reaches_a_lazily_created_table(tmp_path) -> None:
    """The ai_chat tables come after the migrations ran once; the always-run step still reaches them."""
    from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager

    conn = sqlite3.connect(str(tmp_path / "lazy.db"))
    apply_all_migrations(conn)
    assert "ai_chat_messages" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert ensure_share_excluded_column(conn) is False, "no table yet: nothing to add"
    CanonicalTablesManager(conn)
    apply_all_migrations(conn)
    assert _columns(conn, "ai_chat_messages")["share_excluded"][4] == "0"


def test_the_facets_table_is_owner_only_storage_keyed_by_message_id(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "f.db"))
    # A node that never made the ai_chat tables (messenger only) keeps no ai_chat% table at all
    # (tests/core/test_routine_lane_row_tools.py): the always-run step creates the facets table only beside them.
    apply_ai_coding_sessions_v1_up(conn)
    assert conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'ai_chat%'").fetchone()[0] == 0
    conn.execute("CREATE TABLE ai_chat_messages (message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, "
                 "sender_type TEXT NOT NULL, event_at TEXT NOT NULL, content TEXT NOT NULL, metadata_json TEXT, "
                 "source_id TEXT NOT NULL)")
    apply_ai_coding_sessions_v1_up(conn)
    cols = _columns(conn, FACETS_TABLE)
    assert set(cols) == {"message_id", "tool", "project_key", "updated_at"}
    assert cols["message_id"][5] == 1, "message_id is the primary key"
    ensure_facets_table(conn)
    conn.execute(f"INSERT OR REPLACE INTO {FACETS_TABLE} (message_id, tool, project_key, updated_at) VALUES ('x:1','claude_code','lantern','now')")
    conn.execute(f"INSERT OR REPLACE INTO {FACETS_TABLE} (message_id, tool, project_key, updated_at) VALUES ('x:1','claude_code','beacon','later')")
    assert conn.execute(f"SELECT project_key FROM {FACETS_TABLE}").fetchall() == [("beacon",)]


def test_the_facets_table_is_hidden_from_the_data_explorer() -> None:
    from topos.data_explorer_tables import OWNER_ONLY_TABLES

    assert FACETS_TABLE in OWNER_ONLY_TABLES


def _old_node_with_unmarked_rows(tmp_path):
    """A node as the trigger bug left it: the column exists, the triggers read only ``$.share_excluded``, and replies
    the reader flagged at ``$._metadata.share_excluded`` (the runtime-installed mapper's shape) were stored with the
    column at 0 and projected into the timeline."""
    import json

    conn = sqlite3.connect(str(tmp_path / "old.db"))
    conn.execute(
        "CREATE TABLE ai_chat_messages (message_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, sender_type TEXT NOT NULL,"
        " sender_id TEXT, event_at TEXT NOT NULL, content TEXT NOT NULL, content_rendered TEXT, metadata_json TEXT,"
        " sequence INTEGER NOT NULL DEFAULT 0, source_id TEXT NOT NULL, share_excluded INTEGER NOT NULL DEFAULT 0)"
    )
    conn.execute("CREATE TABLE timeline (event_at TEXT, record_id TEXT, source_id TEXT, canonical_table TEXT)")
    rows = {
        "codex:nested": {"_metadata": {"tool": "codex", "share_excluded": True}, "mapper_id": "ai_coding_tool"},
        "codex:flat": {"tool": "codex", "share_excluded": True},
        "codex:repaired": {"_metadata": {"tool": "codex", "share_excluded": False}, "mapper_id": "ai_coding_tool"},
        "codex:plain": {"_metadata": {"tool": "codex"}, "mapper_id": "ai_coding_tool"},
    }
    for message_id, meta in rows.items():
        conn.execute(
            "INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, metadata_json,"
            " source_id) VALUES (?, 'codex:s', 'assistant', '2026-10-17T15:50:00Z', 'invented reply', ?, 'codex_sessions')",
            (message_id, json.dumps(meta)),
        )
        conn.execute(
            "INSERT INTO timeline (event_at, record_id, source_id, canonical_table)"
            " VALUES ('2026-10-17T15:50:00Z', ?, 'codex_sessions', 'ai_chat_messages')",
            (message_id,),
        )
    conn.commit()
    return conn


def test_the_backfill_flags_rows_the_old_triggers_missed_and_drops_their_timeline_rows(tmp_path) -> None:
    """Fail closed on the row's own metadata, at either path: a flagged reply gets the column and loses its timeline
    row; one whose metadata says false (a touch repair clears the metadata before the column) or says nothing keeps both."""
    from topos.storage.db.migrations.ai_coding_sessions_v1 import BACKFILL_ID

    conn = _old_node_with_unmarked_rows(tmp_path)
    apply_ai_coding_sessions_v1_up(conn)
    flags = dict(conn.execute("SELECT message_id, share_excluded FROM ai_chat_messages").fetchall())
    assert flags == {"codex:nested": 1, "codex:flat": 1, "codex:repaired": 0, "codex:plain": 0}, flags
    in_timeline = {r[0] for r in conn.execute("SELECT record_id FROM timeline").fetchall()}
    assert in_timeline == {"codex:repaired", "codex:plain"}, in_timeline
    assert conn.execute("SELECT 1 FROM wiki_schema_migrations WHERE migration_id = ?", (BACKFILL_ID,)).fetchone()


def test_the_backfill_runs_once_and_waits_for_a_lazily_created_table(tmp_path) -> None:
    """A node without the ai_chat tables records nothing, so the boot that first sees the table still backfills it.
    Once recorded, a later boot does not scan again: the fixed triggers cover every write after it."""
    from topos.storage.db.migrations.ai_coding_sessions_v1 import BACKFILL_ID, backfill_unmarked_share_excluded

    empty = sqlite3.connect(str(tmp_path / "empty.db"))
    apply_ai_coding_sessions_v1_up(empty)
    assert not empty.execute("SELECT 1 FROM wiki_schema_migrations WHERE migration_id = ?", (BACKFILL_ID,)).fetchone()

    conn = _old_node_with_unmarked_rows(tmp_path)
    assert backfill_unmarked_share_excluded(conn) == {"flagged": 2, "timeline_removed": 2}
    assert backfill_unmarked_share_excluded(conn) is None, "recorded once; a second pass does not scan"
