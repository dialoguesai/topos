"""
Gap: Phase 4 messages cutover — legacy messages table undocumented → wiki_table_catalog marks deprecated
Sprint: EN-P4-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

import sqlite3

import pytest

from topos.storage.db.migrations import apply_all_migrations

pytestmark = pytest.mark.gap


def test_messages_table_deprecated_in_catalog() -> None:
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    row = conn.execute(
        "SELECT status, authoritative_table FROM wiki_table_catalog WHERE table_name='messages'"
    ).fetchone()
    assert row is not None
    assert row[0] == "deprecated"
    assert row[1] == "conversation_messages"
