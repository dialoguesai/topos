from __future__ import annotations

import sqlite3

import pytest

from topos.__version__ import __version__
from topos.services.local import LocalDeviceService
import topos.core.state as state


@pytest.mark.asyncio
async def test_local_device_info_reports_runtime_version(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from topos.config.settings import settings

    # get_device_info sizes/opens the resolved DB file; pin it to a tmp path so
    # an unset TOPOS_DATABASE_PATH never resolves to the live ~/.topos DB.
    monkeypatch.setattr(settings, "topos_database_path", str(tmp_path / "device.db"))
    monkeypatch.setattr(state, "db_conn", None, raising=False)
    monkeypatch.setattr(state, "sync_client", None, raising=False)
    monkeypatch.setattr(state, "get_system_info", lambda: {"hostname": "test-host"}, raising=True)

    info = await LocalDeviceService().get_device_info()

    assert info.engine_version == __version__
    assert info.database_version == sqlite3.sqlite_version
