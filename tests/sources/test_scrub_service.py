"""Tests for scrub_service orchestration."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from unittest.mock import patch

import pytest

from topos.sources.scrub_service import (
    REMOVE_SOURCE_OPTIONS,
    SCRUB_SOURCE_OPTIONS,
    ScrubInProgressError,
    scrub_source,
    scrub_source_async,
)
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture(autouse=True)
def _stub_recompute(monkeypatch) -> None:
    async def _fake_recompute(*_args, **_kwargs):
        return (
            {
                "topic_clusters": {"status": "skipped", "reason": "test"},
                "dimension_briefs": [],
                "dimension_profiles": {"status": "skipped", "reason": "test"},
            },
            False,
        )

    monkeypatch.setattr("topos.sources.scrub_service._run_recompute_phase", _fake_recompute)


@pytest.fixture
def conn(tmp_path, monkeypatch) -> sqlite3.Connection:
    db = sqlite3.connect(str(tmp_path / "scrub-service.db"))
    db.row_factory = sqlite3.Row
    apply_all_migrations(db)
    monkeypatch.setattr("topos.sources.scrub_service.get_db_connection", lambda: db)
    return db


def test_scrub_source_dry_run_counts_without_mutation(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO journal_entries (entry_id, source_id, content, entry_at)
        VALUES ('j1', 'grow_journal', 'alpha', '2026-01-01')
        """
    )
    conn.commit()

    result = asyncio.run(
        scrub_source_async(
            source_id="grow_journal",
            options=replace(SCRUB_SOURCE_OPTIONS, dry_run=True),
        )
    )

    assert result["scrub_status"] == "dry_run"
    assert conn.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 1
    assert result["report"]["totals"]["rows_deleted"] >= 1


def test_remove_preset_leaves_canonical_rows(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE grow_journal_sessions (
            record_id TEXT PRIMARY KEY,
            source_id TEXT,
            payload_json TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO grow_journal_sessions (record_id, source_id, payload_json) VALUES ('s1', 'grow_journal', '{}')"
    )
    conn.execute(
        """
        INSERT INTO journal_entries (entry_id, source_id, content, entry_at)
        VALUES ('j1', 'grow_journal', 'keep', '2026-01-01')
        """
    )
    conn.commit()

    with patch("topos.sources.scrub_service.install_service.uninstall_source") as uninstall:
        uninstall.return_value = {"uninstalled": True}
        result = scrub_source(
            source_id="grow_journal",
            options=REMOVE_SOURCE_OPTIONS,
        )

    assert result["scrub_status"] == "completed"
    assert conn.execute("SELECT COUNT(*) FROM grow_journal_sessions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM journal_entries WHERE source_id='grow_journal'").fetchone()[0] == 1


def test_scrub_preset_deletes_canonical_rows(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        INSERT INTO journal_entries (entry_id, source_id, content, entry_at)
        VALUES ('j1', 'grow_journal', 'gone', '2026-01-01')
        """
    )
    conn.commit()

    with patch("topos.sources.scrub_service.install_service.uninstall_source") as uninstall:
        uninstall.return_value = {"uninstalled": True}
        result = scrub_source(source_id="grow_journal", options=SCRUB_SOURCE_OPTIONS)

    assert result["scrub_status"] == "completed"
    assert conn.execute("SELECT COUNT(*) FROM journal_entries WHERE source_id='grow_journal'").fetchone()[0] == 0
    assert "residue" in result["report"]


def test_scrub_in_progress_raises(conn: sqlite3.Connection) -> None:
    with patch("topos.sources.scrub_service._ACTIVE_SCRUBS", {"busy_source"}):
        with pytest.raises(ScrubInProgressError):
            scrub_source(source_id="busy_source", options=REMOVE_SOURCE_OPTIONS)
