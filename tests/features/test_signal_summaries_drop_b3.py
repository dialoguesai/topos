"""C5 / Wave B3: signal_summaries dropped — no zombie table after migrate."""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.lifecycle.gc import DEPRECATED_TABLES
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.db.migrations.signal_summaries_drop_v1 import (
    MIGRATION_ID,
    apply_signal_summaries_drop_v1_up,
)
from topos.storage.db.migrations.wiki_mvp_phase0 import apply_wiki_mvp_phase0_up

pytestmark = [pytest.mark.check("C-quality-unread-surfaces-d18")]


def test_phase0_still_creates_then_drop_removes() -> None:
    conn = sqlite3.connect(":memory:")
    apply_wiki_mvp_phase0_up(conn)
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_summaries'"
    ).fetchone()
    apply_signal_summaries_drop_v1_up(conn)
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_summaries'"
        ).fetchone()
        is None
    )
    mid = conn.execute(
        "SELECT 1 FROM wiki_schema_migrations WHERE migration_id=?",
        (MIGRATION_ID,),
    ).fetchone()
    assert mid is not None
    conn.close()


def test_full_migrate_has_no_signal_summaries(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "b3.db"))
    apply_all_migrations(conn)
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_summaries'"
        ).fetchone()
        is None
    )
    assert "signal_summaries" not in DEPRECATED_TABLES
    conn.close()
