"""Pipeline-aware delete_database_rows for Data Explorer."""

from __future__ import annotations

import importlib
import sqlite3

import pytest

pytestmark = pytest.mark.usefixtures("engine_runtime_isolation")


def _set_local_db(monkeypatch: pytest.MonkeyPatch, db_path: str) -> None:
    config_settings = importlib.import_module("topos.config.settings").settings
    handlers_settings = importlib.import_module("topos.core.handlers").settings
    state_settings = importlib.import_module("topos.core.state").settings
    for target in (config_settings, handlers_settings, state_settings):
        monkeypatch.setattr(target, "engine_pool_mode", "off", raising=False)
        monkeypatch.setattr(target, "database_mode", "local", raising=False)
        monkeypatch.setattr(target, "database_path", str(db_path), raising=False)
    # These fixtures seed a minimal schema for delete-lineage logic; do not run
    # the full migration registry against that incomplete shape.
    monkeypatch.setattr(
        "topos.storage.db.migrations.ensure_migrations_applied",
        lambda *_a, **_k: None,
    )


async def _handle(message: dict) -> dict:
    handlers = importlib.import_module("topos.core.handlers")
    return await handlers.handle_control_plane_request(message)


def _seed_pipeline_db(db_path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE grow_journal_sessions (
            record_id TEXT PRIMARY KEY,
            goal TEXT,
            source_id TEXT
        );
        CREATE TABLE journal_entries (
            entry_id TEXT PRIMARY KEY,
            content TEXT,
            source_id TEXT,
            source_record_id TEXT
        );
        CREATE TABLE message_emotions (
            emotion_id TEXT PRIMARY KEY,
            record_id TEXT,
            source_id TEXT,
            payload_json TEXT
        );
        CREATE TABLE signal_embeddings (
            embedding_id TEXT PRIMARY KEY,
            record_id TEXT,
            source_id TEXT,
            payload_json TEXT
        );
        INSERT INTO grow_journal_sessions (record_id, goal, source_id)
        VALUES ('tl-1', 'Ship', 'grow_journal');
        INSERT INTO journal_entries (entry_id, content, source_id, source_record_id)
        VALUES ('tl-1', 'Worked on resume', 'grow_journal', 'tl-1');
        INSERT INTO message_emotions (emotion_id, record_id, source_id, payload_json)
        VALUES ('emo-1', 'tl-1', 'grow_journal', '{}');
        INSERT INTO signal_embeddings (embedding_id, record_id, source_id, payload_json)
        VALUES ('emb-1', 'tl-1', 'grow_journal', '{}');
        """
    )
    conn.commit()
    conn.close()


@pytest.mark.asyncio
async def test_delete_database_rows_row_only(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "row_only.db"
    _seed_pipeline_db(db_path)
    _set_local_db(monkeypatch, str(db_path))

    result = await _handle(
        {
            "id": "row-only",
            "type": "delete_database_rows",
            "payload": {
                "table_name": "journal_entries",
                "row_ids": ["tl-1"],
                "scope": "row_only",
            },
        }
    )
    assert result["status"] == "ok"
    assert result["payload"]["rows_deleted"] == 1

    verify = sqlite3.connect(db_path)
    try:
        assert verify.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0
        assert verify.execute("SELECT COUNT(*) FROM grow_journal_sessions").fetchone()[0] == 1
        assert verify.execute("SELECT COUNT(*) FROM message_emotions").fetchone()[0] == 1
        assert verify.execute("SELECT COUNT(*) FROM signal_embeddings").fetchone()[0] == 1
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_delete_database_rows_with_downstream(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "downstream.db"
    _seed_pipeline_db(db_path)
    _set_local_db(monkeypatch, str(db_path))

    result = await _handle(
        {
            "id": "downstream",
            "type": "delete_database_rows",
            "payload": {
                "table_name": "journal_entries",
                "row_ids": ["tl-1"],
                "scope": "with_downstream",
            },
        }
    )
    assert result["status"] == "ok"

    verify = sqlite3.connect(db_path)
    try:
        assert verify.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0
        assert verify.execute("SELECT COUNT(*) FROM message_emotions").fetchone()[0] == 0
        assert verify.execute("SELECT COUNT(*) FROM signal_embeddings").fetchone()[0] == 0
        assert verify.execute("SELECT COUNT(*) FROM grow_journal_sessions").fetchone()[0] == 1
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_delete_database_rows_with_upstream(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "upstream.db"
    _seed_pipeline_db(db_path)
    _set_local_db(monkeypatch, str(db_path))

    result = await _handle(
        {
            "id": "upstream",
            "type": "delete_database_rows",
            "payload": {
                "table_name": "journal_entries",
                "row_ids": ["tl-1"],
                "scope": "with_upstream",
            },
        }
    )
    assert result["status"] == "ok"

    verify = sqlite3.connect(db_path)
    try:
        assert verify.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0
        assert verify.execute("SELECT COUNT(*) FROM grow_journal_sessions").fetchone()[0] == 0
        assert verify.execute("SELECT COUNT(*) FROM message_emotions").fetchone()[0] == 1
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_delete_database_rows_full_lineage_from_raw(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "full.db"
    _seed_pipeline_db(db_path)
    _set_local_db(monkeypatch, str(db_path))

    result = await _handle(
        {
            "id": "full",
            "type": "delete_database_rows",
            "payload": {
                "table_name": "grow_journal_sessions",
                "row_ids": ["tl-1"],
                "scope": "full_lineage",
            },
        }
    )
    assert result["status"] == "ok"

    verify = sqlite3.connect(db_path)
    try:
        assert verify.execute("SELECT COUNT(*) FROM grow_journal_sessions").fetchone()[0] == 0
        assert verify.execute("SELECT COUNT(*) FROM journal_entries").fetchone()[0] == 0
        assert verify.execute("SELECT COUNT(*) FROM message_emotions").fetchone()[0] == 0
        assert verify.execute("SELECT COUNT(*) FROM signal_embeddings").fetchone()[0] == 0
    finally:
        verify.close()
