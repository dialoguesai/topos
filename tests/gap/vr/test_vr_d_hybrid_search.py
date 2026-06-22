"""Gap tests for hybrid search helpers (Phase D)."""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.signal.hybrid_search import reciprocal_rank_fusion
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.db.migrations import ensure_migrations_applied

pytestmark = pytest.mark.gap


def test_reciprocal_rank_fusion_prefers_overlap() -> None:
    fused = reciprocal_rank_fusion([["a", "b", "c"], ["b", "a", "d"]])
    assert fused["b"] > fused["c"]
    assert fused["b"] > fused["d"]


def test_fts_table_created_after_migration(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "fts.db"))
    ensure_migrations_applied(conn)
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='signal_embeddings_fts'"
    ).fetchone()
    assert row is not None
