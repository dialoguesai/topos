"""Reprocess discovers ui_stream raw tables and supports newest-N limit."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from topos.ingestion.reprocess import (
    _load_raw_records,
    reprocess_source,
    resolve_raw_table_name,
)
from topos.storage.db.migrations import apply_all_migrations
from topos.storage.raw.raw_tables_manager import RawTablesManager

pytestmark = pytest.mark.gap


def _source_def(*, source_id: str = "grow_journal", source_type: str = "ui_stream"):
    return SimpleNamespace(
        source_id=source_id,
        source_type=source_type,
        parser_id="journal.time_log.v1",
        schema_id="journal.time_log.v1",
        canonical_group_id="journal",
    )


def test_resolve_raw_table_prefers_ui_stream_with_rows() -> None:
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    raw = RawTablesManager(conn)

    # Wrong default historically: chat_messages table exists but is empty.
    chat_table = raw.get_raw_table_name("grow_journal", "chat_messages")
    raw.ensure_raw_table(chat_table)

    ui_table = raw.get_raw_table_name("grow_journal", "ui_stream")
    assert ui_table == "raw_growjournal_ui_stream"
    raw.write_raw_record(
        source_id="grow_journal",
        source_record_id="j1",
        payload={"id": "j1", "content": "x.com things"},
        source_type="ui_stream",
    )

    resolved = resolve_raw_table_name(
        conn,
        source_id="grow_journal",
        source_def=_source_def(),
    )
    assert resolved == ui_table


def test_load_raw_records_limit_newest_first_then_oldest_order() -> None:
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    raw = RawTablesManager(conn)
    table = raw.get_raw_table_name("grow_journal", "ui_stream")
    raw.ensure_raw_table(table)

    for i, created_at in enumerate(
        ("2026-01-01T00:00:00", "2026-01-02T00:00:00", "2026-01-03T00:00:00"),
        start=1,
    ):
        conn.execute(
            f'INSERT INTO "{table}" (source_system, source_record_id, payload_json, created_at) '
            "VALUES (?, ?, ?, ?)",
            (
                "grow_journal",
                f"j{i}",
                json.dumps({"id": f"j{i}", "n": i}),
                created_at,
            ),
        )
    conn.commit()

    records, used = _load_raw_records(
        conn,
        "grow_journal",
        source_def=_source_def(),
        limit=2,
    )
    assert used == table
    assert [r["id"] for r in records] == ["j2", "j3"]


@pytest.mark.asyncio
async def test_reprocess_empty_raw_does_not_enrich_everything(monkeypatch) -> None:
    """Wrong-table miss must not fall back to loading all canonical + enrichment."""
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    monkeypatch.setattr("topos.ingestion.reprocess.get_db_connection", lambda: conn)
    monkeypatch.setattr(
        "topos.ingestion.reprocess._resolve_source_def",
        lambda _sid: _source_def(source_id="missing_source"),
    )

    called = {"enrich": False}

    async def _boom(*_a, **_k):
        called["enrich"] = True
        raise AssertionError("enrichment must not run when raw remap is empty")

    monkeypatch.setattr(
        "topos.ingestion.canonical_pipeline.run_post_canonical_pipeline",
        _boom,
    )

    result = await reprocess_source(
        source_id="missing_source",
        dataset_id="user:test",
        from_stage="raw",
        run_enrichment=True,
    )
    assert result["raw_rows_loaded"] == 0
    assert result["records_created"] == 0
    assert result["signal_derive_status"] == "skipped"
    assert called["enrich"] is False
