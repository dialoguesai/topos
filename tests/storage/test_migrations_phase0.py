"""Wiki MVP Phase 0 migration up/down cycle."""

from __future__ import annotations

import sqlite3

from topos.storage.db.migrations.wiki_mvp_phase0 import (
    MIGRATION_ID,
    apply_wiki_mvp_phase0_down,
    apply_wiki_mvp_phase0_up,
)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {row[0] for row in rows}


def test_migrations_phase0_up_down() -> None:
    conn = sqlite3.connect(":memory:")
    apply_wiki_mvp_phase0_up(conn)
    tables = _table_names(conn)
    assert "signal_facts" in tables
    assert "query_sessions" in tables
    assert "signal_embeddings" in tables
    row = conn.execute(
        "SELECT migration_id FROM wiki_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone()
    assert row is not None

    apply_wiki_mvp_phase0_down(conn)
    tables_after = _table_names(conn)
    assert "signal_facts" not in tables_after
    assert "query_sessions" not in tables_after
