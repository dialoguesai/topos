from __future__ import annotations

from pathlib import Path

import pytest

from topos.services.local import LocalDeviceService
from topos.storage.db.paths import get_local_database_size_bytes, sqlite_on_disk_size_bytes
import topos.core.state as state


def test_sqlite_on_disk_size_bytes_includes_wal_and_shm(tmp_path: Path) -> None:
    db_path = tmp_path / "database.db"
    db_path.write_bytes(b"x" * 100)
    db_path.with_name("database.db-wal").write_bytes(b"y" * 20)
    db_path.with_name("database.db-shm").write_bytes(b"z" * 4)

    assert sqlite_on_disk_size_bytes(db_path) == 124


def test_get_local_database_size_bytes_uses_explicit_path(tmp_path: Path) -> None:
    db_path = tmp_path / "custom.db"
    db_path.write_bytes(b"a" * 50)

    assert get_local_database_size_bytes(str(db_path)) == 50


@pytest.mark.asyncio
async def test_local_device_info_reports_database_size_bytes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "database.db"
    db_path.write_bytes(b"b" * 2048)

    monkeypatch.setattr(state, "db_conn", None, raising=False)
    monkeypatch.setattr(state, "sync_client", None, raising=False)
    monkeypatch.setattr(state, "get_system_info", lambda: {"hostname": "test-host"}, raising=True)
    monkeypatch.setattr(state, "_resolve_database_path_from_settings", lambda: db_path, raising=True)
    monkeypatch.setattr(
        "topos.services.local.compute_local_storage_breakdown",
        lambda _conn, _path: {
            "total_bytes": 2048,
            "sqlite_bytes": 2048,
            "raw_files_bytes": 0,
            "categories": [
                {"id": "system_and_other", "label": "System & other", "bytes": 2048, "percent": 100.0},
            ],
        },
    )

    info = await LocalDeviceService().get_device_info()

    assert info.database_size_bytes == 2048
    assert info.storage_breakdown is not None
