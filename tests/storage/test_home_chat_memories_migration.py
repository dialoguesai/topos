"""Memory schema upgrades are backed up, idempotent, and refuse old binaries."""
import sqlite3

import pytest

from topos.storage.db import migrations
from topos.storage.db.migrations.registry import MIGRATIONS, max_migration_order


def test_memory_schema_is_a_registered_ledger_step():
    spec = next(s for s in MIGRATIONS if s.id == 'home_chat_memories_v1')
    assert spec.order == 82 and not spec.always_run
    assert max_migration_order() >= spec.order


def test_upgrade_from_81_preserves_chats_and_backs_up_before_memory_ddl(tmp_path, monkeypatch):
    monkeypatch.setenv('TOPOS_BACKUP_DIR', str(tmp_path / 'backups'))
    with sqlite3.connect(tmp_path / 'database.db') as conn:
        migrations.apply_all_migrations(conn)
        conn.execute('DROP TABLE home_chat_memories')
        conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id='home_chat_memories_v1'")
        conn.execute('CREATE TABLE home_chat_sessions(id TEXT PRIMARY KEY, content TEXT)')
        conn.execute("INSERT INTO home_chat_sessions VALUES ('chat-1', 'Keep this source')")
        conn.execute('PRAGMA user_version=81')
        conn.commit()
        migrations.reset_ensured_connections()
        backup = migrations.ensure_migrations_applied(conn)
        assert backup
        with sqlite3.connect(backup) as before:
            assert migrations.read_user_version(before) == 81
            assert not before.execute("SELECT 1 FROM sqlite_master WHERE name='home_chat_memories'").fetchone()
        assert migrations.read_user_version(conn) == max_migration_order()
        assert conn.execute('SELECT * FROM home_chat_sessions').fetchall() == [('chat-1', 'Keep this source')]
        assert conn.execute("SELECT count(*) FROM wiki_schema_migrations WHERE migration_id='home_chat_memories_v1'").fetchone()[0] == 1
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='idx_home_chat_memories_owner'").fetchone()
        migrations.reset_ensured_connections()
        assert migrations.ensure_migrations_applied(conn) is None
        monkeypatch.setattr(migrations, 'max_migration_order', lambda: 81)
        with pytest.raises(migrations.DowngradeGuardError):
            migrations.ensure_migrations_applied(conn, skip_backup=True)
