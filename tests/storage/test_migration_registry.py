"""Migration registry hardening (PLAN_NODE_RELEASE_MIGRATIONS M1)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from topos.storage.db.migrations import (
    DowngradeGuardError,
    MigrationError,
    apply_all_migrations,
    ensure_migrations_applied,
    max_migration_order,
    pending_ledger_migrations,
    read_user_version,
)
from topos.storage.db.migrations.registry import MIGRATIONS

pytestmark = pytest.mark.public


def test_registry_orders_are_unique_and_dense():
    orders = [m.order for m in MIGRATIONS]
    assert orders == sorted(orders)
    assert len(orders) == len(set(orders))
    ids = [m.id for m in MIGRATIONS]
    assert len(ids) == len(set(ids))


def test_apply_all_stamps_user_version(tmp_path: Path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    apply_all_migrations(conn)
    assert read_user_version(conn) == max_migration_order()
    assert pending_ledger_migrations(conn) == []


def test_ensure_migrations_is_idempotent(tmp_path: Path):
    db = tmp_path / "t.db"
    conn = sqlite3.connect(str(db))
    ensure_migrations_applied(conn, skip_backup=True)
    v1 = read_user_version(conn)
    ensure_migrations_applied(conn, skip_backup=True)
    assert read_user_version(conn) == v1 == max_migration_order()


def test_downgrade_guard_refuses_ahead_user_version(tmp_path: Path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    apply_all_migrations(conn)
    ahead = max_migration_order() + 10
    conn.execute(f"PRAGMA user_version = {ahead}")
    with pytest.raises(DowngradeGuardError, match="newer topos-node"):
        ensure_migrations_applied(conn, skip_backup=True)


def test_pre_migration_backup_written_when_pending(tmp_path: Path, monkeypatch):
    db = tmp_path / "database.db"
    conn = sqlite3.connect(str(db))
    # Fresh file with no ledger → pending tail non-empty → backup required.
    backup_root = tmp_path / "backups"
    monkeypatch.setenv("TOPOS_BACKUP_DIR", str(backup_root))
    path = ensure_migrations_applied(conn, skip_backup=False)
    assert path is not None
    assert Path(path).is_file()
    assert Path(path).parent == backup_root
    assert "database-pre-v" in Path(path).name


def test_migration_error_wraps_failure(tmp_path: Path, monkeypatch):
    conn = sqlite3.connect(str(tmp_path / "t.db"))

    def boom(_conn):
        raise RuntimeError("synthetic failure")

    target = next(m for m in MIGRATIONS if not m.always_run)
    from topos.storage.db import migrations as mig_mod
    from topos.storage.db.migrations.registry import MigrationSpec

    broken = [
        MigrationSpec(
            order=m.order,
            id=m.id,
            fn=boom if m.id == target.id else m.fn,
            always_run=m.always_run,
            also_if_missing_table=m.also_if_missing_table,
        )
        for m in MIGRATIONS
    ]
    monkeypatch.setattr(mig_mod, "MIGRATIONS", broken)
    with pytest.raises(MigrationError, match=target.id):
        ensure_migrations_applied(conn, skip_backup=True)
