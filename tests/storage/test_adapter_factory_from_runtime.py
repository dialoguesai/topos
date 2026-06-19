"""AdapterFactory.from_runtime uses active sqlite connection."""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest


def test_from_runtime_uses_get_db_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "runtime.db"
        monkeypatch.setenv("TOPOS_DATABASE_PATH", str(db_path))
        monkeypatch.setenv("TOPOS_DATABASE_MODE", "local")
        for mod in ("topos.config.settings", "topos.core.state", "topos.storage.adapters.factory"):
            sys.modules.pop(mod, None)

        from topos.core.state import get_db_connection
        from topos.storage.adapters.factory import AdapterFactory

        conn = get_db_connection()
        assert conn is not None
        bundle = AdapterFactory.from_runtime()
        assert bundle.backend == "local_database"
        bundle.vector.list_metadata(limit=1)
