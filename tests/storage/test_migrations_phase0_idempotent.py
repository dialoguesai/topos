"""Phase 0 migration is idempotent when applied twice."""

from __future__ import annotations

import sqlite3

from topos.storage.db.migrations.wiki_mvp_phase0 import apply_wiki_mvp_phase0_up


def test_migrations_phase0_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    apply_wiki_mvp_phase0_up(conn)
    apply_wiki_mvp_phase0_up(conn)
    count = conn.execute("SELECT COUNT(*) FROM signal_facts").fetchone()[0]
    assert count == 0
    migrations = conn.execute("SELECT COUNT(*) FROM wiki_schema_migrations").fetchone()[0]
    assert migrations >= 1
