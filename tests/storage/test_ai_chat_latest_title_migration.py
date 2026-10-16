"""Migration 83, ai_chat_latest_title_v1 (AI chat titles, owner's ruling of 15 Oct 2026: a title is preferred, never
required).

A chat's later titles go to `ai_chat_conversations.latest_title`. `title` stays the first record's, because every
review of the chat's messages pins it; a title written there later withheld every message of the chat until its next
review pass and staled every search index holding one until it was rebuilt.

protects: the column is a registered schema step at order 83, so the downgrade guard refuses an older build; a fresh
store and a table made after the step ran both end with the column; no row is read or changed; and the column is in
the review surface's closed exclusion list, so neither the parent revision a review pins nor the Off-limits
boundary's context revision moves when it is written, or when the upgrade adds it.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.storage.canonical.ai_chat import CanonicalTablesManager
from topos.storage.db.migrations import ai_chat_latest_title_v1 as step
from topos.storage.db.migrations.registry import MIGRATIONS

pytestmark = pytest.mark.public

MIGRATION_ID = "ai_chat_latest_title_v1"
PARENT = {"conversation_id": "chatgpt:conv-a", "owner_user_id": "owner-a", "title": None, "source_id": "chatgpt"}


def _cols(conn: sqlite3.Connection) -> set:
    return {row[1] for row in conn.execute("PRAGMA table_info(ai_chat_conversations)").fetchall()}


def test_the_step_is_registered_at_83_and_always_runs():
    spec = next(spec for spec in MIGRATIONS if spec.id == MIGRATION_ID)
    assert (spec.order, spec.always_run) == (83, True)


def test_a_fresh_store_has_the_column():
    conn = sqlite3.connect(":memory:")
    CanonicalTablesManager(conn)
    assert "latest_title" in _cols(conn)


def test_no_row_is_read_or_changed_and_rerunning_is_a_no_op():
    conn = sqlite3.connect(":memory:")
    CanonicalTablesManager(conn)
    conn.execute("INSERT INTO ai_chat_conversations (conversation_id, owner_user_id, title, source_id, created_at, "
                 "updated_at) VALUES ('chatgpt:conv-a', 'owner-a', 'Old thread', 'chatgpt', '2026-01-01', '2026-01-01')")
    conn.commit()
    rows = conn.execute("SELECT * FROM ai_chat_conversations").fetchall()
    schema = conn.execute("PRAGMA schema_version").fetchone()
    step.apply_ai_chat_latest_title_v1_up(conn)
    assert conn.execute("PRAGMA schema_version").fetchone() == schema     # no DDL the second time
    assert conn.execute("SELECT * FROM ai_chat_conversations").fetchall() == rows
    assert conn.execute("SELECT COUNT(*) FROM wiki_schema_migrations WHERE migration_id=?",
                        (MIGRATION_ID,)).fetchone()[0] == 1


def test_a_table_made_later_still_gets_the_column():
    conn = sqlite3.connect(":memory:")
    step.apply_ai_chat_latest_title_v1_up(conn)                           # no table yet: records the id, adds nothing
    conn.execute("CREATE TABLE ai_chat_conversations (conversation_id TEXT PRIMARY KEY, title TEXT)")
    step.apply_ai_chat_latest_title_v1_up(conn)
    assert "latest_title" in _cols(conn)


@pytest.mark.parametrize("latest", [None, "Retention charts", "Retention rate charts"])
def test_the_latest_title_moves_no_review_and_no_context_revision(latest):
    """`PARENT` without the key is a row read before the upgrade; with it, after, as each later batch writes it."""
    from topos.permissions_v2.entity_boundary import _revision_rows, rows_revision
    from topos.permissions_v2.evidence import _row_revision

    after = {**PARENT, "latest_title": latest}
    assert _row_revision(after, table="ai_chat_conversations") == _row_revision(PARENT, table="ai_chat_conversations")
    assert (rows_revision([_revision_rows("ai_chat_conversations", [after])])
            == rows_revision([_revision_rows("ai_chat_conversations", [PARENT])]))


def test_the_first_records_title_is_still_pinned():
    from topos.permissions_v2.evidence import _row_revision

    assert (_row_revision({**PARENT, "title": "Old thread"}, table="ai_chat_conversations")
            != _row_revision(PARENT, table="ai_chat_conversations"))
