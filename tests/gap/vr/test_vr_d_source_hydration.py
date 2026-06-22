"""Gap tests for source hydration (Phase D)."""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.signal.source_hydration import hydrate_record_text
from topos.storage.db.migrations import ensure_migrations_applied

pytestmark = pytest.mark.gap


def test_hydrate_journal_entry(tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "hydrate.db"))
    ensure_migrations_applied(conn)
    conn.execute(
        """
        INSERT INTO journal_entries (entry_id, content, source_id)
        VALUES ('j1', 'Slept eight hours', 'demo_journal_file')
        """
    )
    conn.commit()
    result = hydrate_record_text(conn, "j1", record_type="journal_entry")
    assert result.found is True
    assert "Slept" in result.content
