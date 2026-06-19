"""Engine wiki MVP phase 4 migration tests."""

import sqlite3

from topos.storage.db.migrations import apply_all_migrations
from topos.storage.db.migrations.wiki_mvp_phase4_messages_cutover import (
    apply_wiki_mvp_phase4_messages_cutover_up,
)


def test_phase4_messages_table_marked_deprecated() -> None:
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    row = conn.execute(
        "SELECT authoritative_table, status FROM wiki_table_catalog WHERE table_name='messages'"
    ).fetchone()
    assert row is not None
    assert row[0] == "conversation_messages"
    assert row[1] == "deprecated"


def test_phase4_cutover_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    apply_wiki_mvp_phase4_messages_cutover_up(conn)
    count = conn.execute("SELECT COUNT(*) FROM wiki_table_catalog").fetchone()[0]
    assert count >= 1
