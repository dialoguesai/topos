"""Audit row written on scrub completion."""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import patch

import pytest

from topos.sources.scrub_service import SCRUB_SOURCE_OPTIONS, scrub_source_async
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture(autouse=True)
def _stub_recompute(monkeypatch) -> None:
    async def _fake_recompute(*_args, **_kwargs):
        return (
            {"topic_clusters": {"status": "skipped"}, "dimension_briefs": [], "dimension_profiles": {"status": "skipped"}},
            False,
        )

    monkeypatch.setattr("topos.sources.scrub_service._run_recompute_phase", _fake_recompute)


@pytest.mark.asyncio
async def test_scrub_writes_audit_row(tmp_path, monkeypatch) -> None:
    db = sqlite3.connect(str(tmp_path / "audit.db"))
    db.row_factory = sqlite3.Row
    apply_all_migrations(db)
    monkeypatch.setattr("topos.sources.scrub_service.get_db_connection", lambda: db)

    db.execute(
        "INSERT INTO journal_entries (entry_id, source_id, content, entry_at) VALUES ('j1', 'grow_journal', 'x', '2026-01-01')"
    )
    db.commit()

    with patch("topos.sources.scrub_service.install_service.uninstall_source") as uninstall:
        uninstall.return_value = {"uninstalled": True}
        await scrub_source_async(source_id="grow_journal", options=SCRUB_SOURCE_OPTIONS)

    row = db.execute(
        "SELECT stage, status, records_out FROM ingest_audit WHERE source_id='grow_journal' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert row[0] == "source_scrub"
    assert row[1] == "completed"
    assert int(row[2] or 0) >= 1
