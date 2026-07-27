from __future__ import annotations

import os
import sys
from typing import Iterable

import pytest

_ENV_KEYS = (
    "DATABASE_MODE",
    "DATABASE_PATH",
    "POSTGRES_DSN",
    "ENGINE_POOL_MODE",
    "TOPOS_KEY",
    "TOPOS_CONTROL_PLANE_URL",
    "ENABLE_HEALTH_AUTH",
    "TOPOS_DATABASE_MODE",
    "TOPOS_DATABASE_PATH",
    "TOPOS_POSTGRES_DSN",
    "TOPOS_POOL_MODE",
)
_MODULE_PREFIXES = (
    "topos.sources",
    "topos.sources.registry",
    "topos.ingestion",
    "topos.ingestion.manager",
    "topos.ingestion.parsers",
    "topos.core.handlers",
    "topos.api.source_install",
    "topos.storage.db",
    "topos.config",
    "topos.core.state",
    "topos.auth",
    "topos.app",
)


def _purge_modules(prefixes: Iterable[str]) -> None:
    for mod_name in list(sys.modules.keys()):
        if any(mod_name == prefix or mod_name.startswith(f"{prefix}.") for prefix in prefixes):
            sys.modules.pop(mod_name, None)


def _reset_db_singleton() -> None:
    try:
        from topos.core import state as core_state
    except Exception:
        return
    conn = getattr(core_state, "db_conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    core_state.db_conn = None
    core_state._db_conn_path = None


@pytest.fixture
def engine_runtime_isolation():
    original_env = {key: os.environ.get(key) for key in _ENV_KEYS}
    for key in _ENV_KEYS:
        os.environ.pop(key, None)
    # Keep tests deterministic even when they still set legacy variable names.
    os.environ.setdefault("TOPOS_KEY", "test-key")
    os.environ.setdefault("ENABLE_HEALTH_AUTH", "false")
    # Never leave the DB path unset: an unset TOPOS_DATABASE_PATH resolves to
    # the developer's live ~/.topos/database.db (root conftest _no_live_db_guard
    # set the popped value; re-assert it so re-imported Settings stay guarded).
    if original_env.get("TOPOS_DATABASE_PATH"):
        os.environ["TOPOS_DATABASE_PATH"] = original_env["TOPOS_DATABASE_PATH"]
    # Snapshot original module objects so teardown restores identities. A bare
    # purge forks modules: later tests that bound topos.core.handlers/state at
    # collection time monkeypatch the old objects while re-imports resolve fresh
    # ones (same disease and cure as module_reload_isolation in the root conftest).
    snapshot = {
        name: sys.modules[name]
        for name in list(sys.modules)
        if any(name == p or name.startswith(f"{p}.") for p in _MODULE_PREFIXES)
    }
    _reset_db_singleton()
    _purge_modules(_MODULE_PREFIXES)
    yield
    _reset_db_singleton()
    _purge_modules(_MODULE_PREFIXES)
    sys.modules.update(snapshot)
    # Re-point parent-package attributes at the originals too: pytest's
    # monkeypatch resolves dotted targets by getattr traversal (topos.core →
    # .handlers), not via sys.modules, so a stale parent attr still exposes the
    # forked module to later tests.
    for name, module in snapshot.items():
        parent_name, _, child = name.rpartition(".")
        parent = sys.modules.get(parent_name) if parent_name else None
        if parent is not None:
            setattr(parent, child, module)
    _reset_db_singleton()
    for key, value in original_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(autouse=True)
def _default_topos_test_env():
    """
    Stabilize auth-sensitive tests regardless of local developer .env values.
    Individual tests can still override via monkeypatch.setenv(...).
    """
    original = {
        "TOPOS_KEY": os.environ.get("TOPOS_KEY"),
        "TOPOS_CONTROL_PLANE_URL": os.environ.get("TOPOS_CONTROL_PLANE_URL"),
        "ENABLE_HEALTH_AUTH": os.environ.get("ENABLE_HEALTH_AUTH"),
    }
    os.environ["TOPOS_KEY"] = "test-key"
    os.environ["TOPOS_CONTROL_PLANE_URL"] = ""
    os.environ["ENABLE_HEALTH_AUTH"] = "false"
    # Force deterministic defaults across local developer environments.
    os.environ["TOPOS_DATABASE_MODE"] = "local"
    os.environ["TOPOS_POOL_MODE"] = "off"
    os.environ.pop("TOPOS_POSTGRES_DSN", None)
    try:
        from topos.config.settings import settings as runtime_settings

        runtime_settings.topos_key = os.environ["TOPOS_KEY"]
        runtime_settings.topos_control_plane_url = os.environ.get("TOPOS_CONTROL_PLANE_URL") or ""
        runtime_settings.enable_health_auth = False
        runtime_settings.topos_database_mode = "local"
        runtime_settings.topos_pool_mode = "off"
        runtime_settings.topos_postgres_dsn = None
    except Exception:
        pass
    yield
    for key, value in original.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
