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


class TestBackupPrecedesEveryStampMove:
    """The pre-migration backup is written before ``user_version`` moves.

    The stamp is what the downgrade guard reads, so once it moves an older
    engine refuses the database, and ``always_run`` steps move it too. 1.4.0
    took a database from 73 to 78 with no backup: its one ledger-guarded step,
    74, was already recorded (a stamp walked back keeps its ledger rows), and
    75, 76 and 78 are ``always_run``, which ``pending_ledger_migrations`` skips.
    """

    def test_a_stamp_jump_with_nothing_ledger_pending_backs_up_first(
        self, tmp_path: Path, monkeypatch
    ):
        conn = sqlite3.connect(str(tmp_path / "database.db"))
        apply_all_migrations(conn)
        # That database's shape: every ledger row recorded, the stamp just
        # below the newest ledger-guarded step.
        behind = max(m.order for m in MIGRATIONS if not m.always_run) - 1
        conn.execute(f"PRAGMA user_version = {behind}")
        conn.commit()
        assert pending_ledger_migrations(conn) == []
        backup_root = tmp_path / "backups"
        monkeypatch.setenv("TOPOS_BACKUP_DIR", str(backup_root))

        path = ensure_migrations_applied(conn)

        assert path is not None
        assert Path(path).parent == backup_root
        assert read_user_version(conn) == max_migration_order()
        # Copied before the stamp moved, so an engine that stops at ``behind``
        # can still open it.
        copy = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            assert read_user_version(copy) == behind
        finally:
            copy.close()

    def test_always_run_boots_at_the_head_write_no_backup(
        self, tmp_path: Path, monkeypatch
    ):
        from topos.storage.db.migrations import reset_ensured_connections

        db = tmp_path / "database.db"
        conn = sqlite3.connect(str(db))
        apply_all_migrations(conn)
        conn.commit()
        conn.close()
        backup_root = tmp_path / "backups"
        monkeypatch.setenv("TOPOS_BACKUP_DIR", str(backup_root))
        calls = _count_always_run_calls(monkeypatch)

        for _boot in range(2):
            reset_ensured_connections()  # a new process: nothing memoized
            conn = sqlite3.connect(str(db))
            try:
                assert pending_ledger_migrations(conn) == []
                assert ensure_migrations_applied(conn) is None
                assert read_user_version(conn) == max_migration_order()
            finally:
                conn.close()

        assert calls[_ALWAYS_RUN_ID] == 2  # the always_run steps ran on both boots
        assert sorted(backup_root.glob("*")) == []


_NEWEST_LEDGER_STEP = max((m for m in MIGRATIONS if not m.always_run), key=lambda m: m.order)
_NEWEST_ALWAYS_RUN_STEP = max((m for m in MIGRATIONS if m.always_run), key=lambda m: m.order)

#: (stamp, ledger row to drop, step that fails). A stamp jump whose ``always_run``
#: step fails, and a ledger-pending step failing at the head, which is how every
#: failed ledger migration behaved before the stamp trigger existed.
_FAILING_RUNS = {
    "stamp_jump": (_NEWEST_LEDGER_STEP.order - 1, None, _NEWEST_ALWAYS_RUN_STEP.order),
    "ledger_pending": (max_migration_order(), _NEWEST_LEDGER_STEP.id, _NEWEST_LEDGER_STEP.order),
}


class TestBackupIsWrittenOncePerStartingStamp:
    """A failed run is retried on the next connection; its backup is not.

    ``core.state`` answers a MigrationError by opening the database again on
    the next ``get_db_connection()`` call, which re-runs this runner. Each run
    copied the whole database again, and retention (the newest three) then
    pruned the first copy, the only one taken before any step ran.
    """

    @staticmethod
    def _database(db: Path, *, stamp: int, unrecord=None) -> sqlite3.Connection:
        conn = sqlite3.connect(str(db))
        apply_all_migrations(conn)
        if unrecord is not None:
            conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id=?", (unrecord,))
        conn.execute(f"PRAGMA user_version = {stamp}")
        conn.commit()
        return conn

    @staticmethod
    def _count_backups(monkeypatch) -> list:
        from topos.storage.db import migrations as mig_mod

        copies: list = []
        real = mig_mod.backup_database_before_migrations

        def counting(conn, **kwargs):
            path = real(conn, **kwargs)
            copies.append(path)
            return path

        monkeypatch.setattr(mig_mod, "backup_database_before_migrations", counting)
        return copies

    @staticmethod
    def _fail_step(monkeypatch, order: int) -> None:
        from topos.storage.db import migrations as mig_mod
        from topos.storage.db.migrations.registry import MigrationSpec

        def boom(_conn):
            raise sqlite3.OperationalError("synthetic: this step keeps failing")

        monkeypatch.setattr(
            mig_mod,
            "MIGRATIONS",
            [
                MigrationSpec(
                    order=m.order,
                    id=m.id,
                    fn=boom if m.order == order else m.fn,
                    always_run=m.always_run,
                    also_if_missing_table=m.also_if_missing_table,
                )
                for m in MIGRATIONS
            ],
        )

    @pytest.mark.parametrize("shape", sorted(_FAILING_RUNS))
    def test_retries_reuse_the_first_copy_until_the_run_succeeds(
        self, tmp_path: Path, monkeypatch, shape
    ):
        from topos.storage.db import migrations as mig_mod
        from topos.storage.db.migrations import reset_ensured_connections

        stamp, unrecord, failing = _FAILING_RUNS[shape]
        reset_ensured_connections()
        monkeypatch.setenv("TOPOS_BACKUP_DIR", str(tmp_path / "backups"))
        conn = self._database(tmp_path / "database.db", stamp=stamp, unrecord=unrecord)
        copies = self._count_backups(monkeypatch)
        self._fail_step(monkeypatch, failing)

        for _retry in range(3):
            with pytest.raises(MigrationError) as failed:
                ensure_migrations_applied(conn)
            # Every failure still names the copy taken before any step ran.
            assert f"Restore from {copies[0]}." in str(failed.value)
        assert len(copies) == 1

        monkeypatch.setattr(mig_mod, "MIGRATIONS", MIGRATIONS)  # the step is fixed
        assert ensure_migrations_applied(conn) == str(copies[0])
        assert len(copies) == 1
        assert read_user_version(conn) == max_migration_order()

    def test_a_copy_deleted_since_is_taken_again(self, tmp_path: Path, monkeypatch):
        from topos.storage.db.migrations import reset_ensured_connections

        stamp, _unrecord, failing = _FAILING_RUNS["stamp_jump"]
        reset_ensured_connections()
        monkeypatch.setenv("TOPOS_BACKUP_DIR", str(tmp_path / "backups"))
        conn = self._database(tmp_path / "database.db", stamp=stamp)
        copies = self._count_backups(monkeypatch)
        self._fail_step(monkeypatch, failing)

        with pytest.raises(MigrationError):
            ensure_migrations_applied(conn)
        Path(copies[0]).unlink()
        with pytest.raises(MigrationError):
            ensure_migrations_applied(conn)

        assert len(copies) == 2
        assert Path(copies[1]).is_file()

    def test_a_different_database_in_the_same_slot_gets_its_own_copy(
        self, tmp_path: Path, monkeypatch
    ):
        from topos.storage.db.migrations import reset_ensured_connections

        stamp, _unrecord, failing = _FAILING_RUNS["stamp_jump"]
        reset_ensured_connections()
        monkeypatch.setenv("TOPOS_BACKUP_DIR", str(tmp_path / "backups"))
        db = tmp_path / "database.db"
        first = self._database(db, stamp=stamp)
        self._database(tmp_path / "other.db", stamp=stamp).close()
        copies = self._count_backups(monkeypatch)
        self._fail_step(monkeypatch, failing)
        with pytest.raises(MigrationError):
            ensure_migrations_applied(first)
        first.close()

        # Another Topos takes the slot: the same path and stamp, a different file.
        db.rename(tmp_path / "first.db")
        (tmp_path / "other.db").rename(db)
        second = sqlite3.connect(str(db))
        try:
            with pytest.raises(MigrationError):
                ensure_migrations_applied(second)
        finally:
            second.close()

        assert len(copies) == 2
