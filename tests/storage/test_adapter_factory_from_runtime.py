"""AdapterFactory.from_runtime uses active sqlite connection."""

from pathlib import Path

import pytest


def test_from_runtime_uses_get_db_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Deliberately NO module reloading here. An earlier version popped
    # settings/state/factory from sys.modules and re-imported them, which
    # forked module identities and leaked a db singleton bound to this test's
    # temp dir into every later test (order-dependent failures across
    # tests/sources and tests/ingestion). get_db_connection() re-resolves the
    # path whenever settings.topos_database_path changes, so patching the live
    # settings object exercises the same behavior without forking anything.
    from topos.config.settings import settings
    from topos.core import state
    from topos.storage.adapters.factory import AdapterFactory

    db_path = tmp_path / "runtime.db"
    monkeypatch.setattr(settings, "topos_database_path", str(db_path))
    monkeypatch.setattr(settings, "topos_database_mode", "local", raising=False)
    # Reset the connection singleton so the patched path is picked up;
    # monkeypatch restores the original attributes afterwards.
    monkeypatch.setattr(state, "db_conn", None)
    monkeypatch.setattr(state, "_db_conn_path", None)

    conn = state.get_db_connection()
    try:
        assert conn is not None
        assert str(db_path) in str(
            conn.execute("PRAGMA database_list").fetchone()[2]
        ), "connection did not open the configured path"
        bundle = AdapterFactory.from_runtime()
        assert bundle.backend == "local_database"
        bundle.vector.list_metadata(limit=1)
    finally:
        try:
            conn.close()
        except Exception:
            pass
