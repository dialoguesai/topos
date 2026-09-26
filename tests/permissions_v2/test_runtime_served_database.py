"""get_runtime binds the database the node serves, not only an explicit TOPOS_DATABASE_PATH.

An app-launched node never sets TOPOS_DATABASE_PATH (only ``--db-path`` does). The v2 runtime
required it, so on a normally installed node every coordination message was refused with
canonical_database_binding and no grant could activate (found 26 Sep 2026 on a bound node:
the ledger was never created). The beta stacks set the path in their containers, which hid it.
"""
import sqlite3
from types import SimpleNamespace

import pytest

from topos.permissions_v2 import runtime as runtime_module
from topos.permissions_v2.canonical import PolicyError
from tests.permissions_v2.test_node_protocol import mutation, protocol  # noqa: F401
from tests.permissions_v2.test_protocol_runtime import configured  # noqa: F401


@pytest.fixture
def unpinned(configured, monkeypatch):
    """A node with the v2 flags on and NO explicit database path, as the app launches it."""
    config, path, fixture = configured
    from topos.config.settings import settings

    monkeypatch.setattr(settings, "topos_database_path", None)
    monkeypatch.delenv("TOPOS_DATABASE_PATH", raising=False)
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_ENABLED", "true")
    monkeypatch.setenv("TOPOS_PERMISSIONS_V2_CONFIG_PATH", str(path))
    monkeypatch.setattr(runtime_module, "_runtime", None)
    yield config, path, fixture
    current = runtime_module._runtime
    if current is not None:
        current.close()


def _serve(monkeypatch, db_path):
    from topos.storage.db import paths

    monkeypatch.setattr(paths, "resolve_active_database", lambda *args, **kwargs: SimpleNamespace(path=db_path))


def test_a_node_without_an_explicit_path_binds_the_database_it_serves(unpinned, monkeypatch):
    _, _, fixture = unpinned
    canonical = fixture[0].canonical_database
    _serve(monkeypatch, canonical)
    runtime = runtime_module.get_runtime()
    assert runtime.protocol.canonical_database.resolve() == canonical.resolve()


def test_a_node_serving_another_topos_stays_unbound(unpinned, monkeypatch, tmp_path):
    other = tmp_path / "another-topos.db"
    sqlite3.connect(other).close()
    _serve(monkeypatch, other)
    with pytest.raises(PolicyError, match="canonical_database_binding"):
        runtime_module.get_runtime()
    assert runtime_module._runtime is None


def test_no_served_database_stays_unbound(unpinned, monkeypatch):
    _serve(monkeypatch, None)
    with pytest.raises(PolicyError, match="canonical_database_binding"):
        runtime_module.get_runtime()
    assert runtime_module._runtime is None
