from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from topos.services.local import LocalDeviceService
from topos.storage.db.storage_breakdown import compute_local_storage_breakdown
import topos.core.state as state


def test_compute_local_storage_breakdown_groups_sqlite_and_raw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "database.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE signal_embeddings (
            embedding_id TEXT PRIMARY KEY,
            vector_blob BLOB
        );
        CREATE TABLE ai_chat_messages (
            message_id TEXT PRIMARY KEY,
            content TEXT NOT NULL
        );
        CREATE TABLE uma_access_requests (
            request_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO signal_embeddings (embedding_id, vector_blob) VALUES (?, ?)",
        ("emb-1", b"x" * 4096),
    )
    conn.execute(
        "INSERT INTO ai_chat_messages (message_id, content) VALUES (?, ?)",
        ("msg-1", "hello" * 200),
    )
    conn.execute(
        "INSERT INTO uma_access_requests (request_id, payload_json) VALUES (?, ?)",
        ("req-1", '{"scope":"read"}' * 100),
    )
    conn.commit()

    raw_root = tmp_path / "ingestion"
    raw_root.mkdir()
    (raw_root / "dataset_messages.jsonl").write_bytes(b"z" * 8192)
    monkeypatch.setenv("TOPOS_INGESTION_BASE_PATH", str(raw_root))
    monkeypatch.setattr(
        "topos.storage.db.storage_breakdown.raw_ingestion_size_bytes",
        lambda: 8192,
    )

    breakdown = compute_local_storage_breakdown(conn, db_path)
    conn.close()

    assert breakdown is not None
    assert breakdown["total_bytes"] >= breakdown["sqlite_bytes"]
    assert breakdown["raw_files_bytes"] == 8192
    categories = {item["id"]: item for item in breakdown["categories"]}
    assert "raw_files" in categories
    assert "vector_embeddings" in categories
    assert "messages_and_records" in categories
    assert sum(item["percent"] for item in breakdown["categories"]) == pytest.approx(100.0, abs=0.2)


@pytest.mark.asyncio
async def test_local_device_info_includes_storage_breakdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "database.db"
    db_path.write_bytes(b"a" * 4096)

    monkeypatch.setattr(state, "db_conn", None, raising=False)
    monkeypatch.setattr(state, "sync_client", None, raising=False)
    monkeypatch.setattr(state, "get_system_info", lambda: {"hostname": "test-host"}, raising=True)
    monkeypatch.setattr(state, "_resolve_database_path_from_settings", lambda: db_path, raising=True)
    monkeypatch.setattr(
        "topos.services.local.compute_local_storage_breakdown",
        lambda _conn, _path: {
            "total_bytes": 12000,
            "sqlite_bytes": 4096,
            "raw_files_bytes": 7904,
            "categories": [
                {"id": "raw_files", "label": "Raw files", "bytes": 7904, "percent": 65.9},
                {"id": "system_and_other", "label": "System & other", "bytes": 4096, "percent": 34.1},
            ],
        },
    )

    info = await LocalDeviceService().get_device_info()

    assert info.database_size_bytes == 12000
    assert info.storage_breakdown is not None
    assert info.storage_breakdown["categories"]
