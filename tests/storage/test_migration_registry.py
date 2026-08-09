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


def _count_always_run_calls(monkeypatch) -> dict:
    """Replace MIGRATIONS with counting wrappers; returns {migration_id: calls}."""
    from topos.storage.db import migrations as mig_mod
    from topos.storage.db.migrations.registry import MigrationSpec

    calls: dict = {}

    def wrap(spec):
        def counted(conn, _spec=spec):
            calls[_spec.id] = calls.get(_spec.id, 0) + 1
            return _spec.fn(conn)

        return MigrationSpec(
            order=spec.order,
            id=spec.id,
            fn=counted,
            always_run=spec.always_run,
            also_if_missing_table=spec.also_if_missing_table,
        )

    monkeypatch.setattr(mig_mod, "MIGRATIONS", [wrap(m) for m in MIGRATIONS])
    return calls


_ALWAYS_RUN_ID = next(m.id for m in MIGRATIONS if m.always_run)


class TestEnsureIsMemoizedPerConnection:
    """``ensure_migrations_applied`` is a hot-path call, not a startup-only one.

    ``AdapterFactory.create`` runs it per batch, per worker thread, per
    connection, and every ``always_run`` step it re-applied took the write gate
    and committed. Re-asserting unchanged schema hundreds of times per ingest is
    what turned any brief write-lock holder into a 30s busy_timeout for the next
    writer.
    """

    def test_repeat_call_without_schema_change_skips_always_run(
        self, tmp_path: Path, monkeypatch
    ):
        from topos.storage.db.migrations import reset_ensured_connections

        reset_ensured_connections()
        calls = _count_always_run_calls(monkeypatch)
        conn = sqlite3.connect(str(tmp_path / "t.db"))

        ensure_migrations_applied(conn, skip_backup=True)
        after_first = calls[_ALWAYS_RUN_ID]
        ensure_migrations_applied(conn, skip_backup=True)
        ensure_migrations_applied(conn, skip_backup=True)

        assert calls[_ALWAYS_RUN_ID] == after_first

    def test_force_still_re_runs_always_run(self, tmp_path: Path, monkeypatch):
        from topos.storage.db.migrations import reset_ensured_connections

        reset_ensured_connections()
        calls = _count_always_run_calls(monkeypatch)
        conn = sqlite3.connect(str(tmp_path / "t.db"))

        ensure_migrations_applied(conn, skip_backup=True)
        after_first = calls[_ALWAYS_RUN_ID]
        ensure_migrations_applied(conn, skip_backup=True, force=True)

        assert calls[_ALWAYS_RUN_ID] == after_first + 1

    def test_memo_is_per_connection(self, tmp_path: Path, monkeypatch):
        from topos.storage.db.migrations import reset_ensured_connections

        reset_ensured_connections()
        calls = _count_always_run_calls(monkeypatch)
        db = tmp_path / "t.db"
        conn = sqlite3.connect(str(db))
        ensure_migrations_applied(conn, skip_backup=True)
        after_first = calls[_ALWAYS_RUN_ID]

        other = sqlite3.connect(str(db))
        ensure_migrations_applied(other, skip_backup=True)

        assert calls[_ALWAYS_RUN_ID] == after_first + 1

    def test_in_memory_databases_are_never_memoized(self, monkeypatch):
        """A recycled id on a fresh :memory: handle must not skip migrations."""
        from topos.storage.db.migrations import reset_ensured_connections

        reset_ensured_connections()
        calls = _count_always_run_calls(monkeypatch)
        conn = sqlite3.connect(":memory:")

        ensure_migrations_applied(conn, skip_backup=True)
        after_first = calls[_ALWAYS_RUN_ID]
        ensure_migrations_applied(conn, skip_backup=True)

        assert calls[_ALWAYS_RUN_ID] == after_first + 1

    def test_memo_is_ignored_when_the_database_is_not_stamped(self, tmp_path: Path):
        """Memo hit + unstamped DB (a recycled id) falls through to the full run."""
        from topos.storage.db.migrations import reset_ensured_connections

        reset_ensured_connections()
        conn = sqlite3.connect(str(tmp_path / "t.db"))
        ensure_migrations_applied(conn, skip_backup=True)
        conn.execute("DROP TABLE pipeline_jobs")
        conn.execute("PRAGMA user_version = 0")
        conn.commit()

        ensure_migrations_applied(conn, skip_backup=True)
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pipeline_jobs'"
            ).fetchone()
            is not None
        )
        assert read_user_version(conn) == max_migration_order()


class TestAlwaysRunRepairsLateDDL:
    """``always_run`` steps ALTER tables that legacy DDL creates after the run.

    ``CanonicalTablesManager`` builds ``ai_chat_conversations`` without the
    provenance columns; ``wiki_mvp_phase1`` adds them on the next pass. Memoizing
    the runner must not swallow that repair — SQLite's ``schema_version`` bumps
    on the late CREATE, which is what re-arms it.
    """

    def test_table_created_after_ensure_still_gets_provenance_columns(
        self, tmp_path: Path
    ):
        from topos.storage.db.migrations import reset_ensured_connections

        reset_ensured_connections()
        conn = sqlite3.connect(str(tmp_path / "t.db"))
        ensure_migrations_applied(conn, skip_backup=True)

        conn.execute("DROP TABLE IF EXISTS ai_chat_conversations")
        conn.execute("CREATE TABLE ai_chat_conversations (conversation_id TEXT PRIMARY KEY)")
        conn.commit()

        ensure_migrations_applied(conn, skip_backup=True)

        columns = {r[1] for r in conn.execute("PRAGMA table_info(ai_chat_conversations)")}
        assert "source_record_id" in columns

    def test_ddl_on_another_connection_also_re_arms_the_runner(self, tmp_path: Path):
        from topos.storage.db.migrations import reset_ensured_connections

        reset_ensured_connections()
        db = tmp_path / "t.db"
        conn = sqlite3.connect(str(db))
        ensure_migrations_applied(conn, skip_backup=True)

        writer = sqlite3.connect(str(db))
        writer.execute("DROP TABLE IF EXISTS ai_chat_messages")
        writer.execute("CREATE TABLE ai_chat_messages (message_id TEXT PRIMARY KEY)")
        writer.commit()
        writer.close()

        ensure_migrations_applied(conn, skip_backup=True)

        columns = {r[1] for r in conn.execute("PRAGMA table_info(ai_chat_messages)")}
        assert "source_record_id" in columns
