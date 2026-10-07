"""Migration 81, off_limits_carried_waiting_v1 (fifth round, second re-check R3-M4).

The mark of an Off-limits entry the upgrade carried (`entity_blackholes.carried_waiting_json`) is read only by a
build that knows it. An older build reads a carried entry as an ordinary one, with every handle and the contact id
as names: its read-time scan withholds the owner's own messages from his own client, and a clean-up he starts there
deletes what the mark was written to keep. The column used to be added in place by the first write that needed it,
with no schema version, so nothing stopped an older build from opening such a database.

protects: the column is a registered schema step, so the schema version moves and the downgrade guard that already
stands in front of every earlier step refuses an older build; a database of the release before, one that got the
column in place, and a fresh install all end with the column at the new version; no row is read or changed.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.lifecycle.blackhole import WAITING_COLUMN, BlackholeStore
from topos.storage.db import migrations
from topos.storage.db.migrations import (DowngradeGuardError, apply_all_migrations, ensure_migrations_applied,
                                         max_migration_order, read_user_version)
from topos.storage.db.migrations.registry import MIGRATIONS

pytestmark = pytest.mark.public

HEAD = 81
MIGRATION_ID = "off_limits_carried_waiting_v1"


def _cols(conn: sqlite3.Connection) -> set:
    return {row[1] for row in conn.execute("PRAGMA table_info(entity_blackholes)").fetchall()}


def as_the_release_before(conn: sqlite3.Connection, *, with_the_column: bool) -> None:
    """A database as a build at schema 80 left it: every earlier step applied, this one never. With the column when
    such a build's carry step had added it in place (the builds between 1.4.4 and this one did), without it as
    1.4.4 itself leaves it."""
    apply_all_migrations(conn)
    conn.execute("DELETE FROM wiki_schema_migrations WHERE migration_id=?", (MIGRATION_ID,))
    if not with_the_column and WAITING_COLUMN in _cols(conn):
        conn.execute(f"ALTER TABLE entity_blackholes DROP COLUMN {WAITING_COLUMN}")
    conn.execute("PRAGMA user_version = 80")
    conn.commit()
    migrations.reset_ensured_connections()


def test_spec_81_is_the_always_run_head():
    spec = next(spec for spec in MIGRATIONS if spec.id == MIGRATION_ID)
    assert spec.order == HEAD and spec.always_run is True
    assert max_migration_order() == HEAD
    assert len({spec.order for spec in MIGRATIONS}) == len(MIGRATIONS)
    assert WAITING_COLUMN == "carried_waiting_json"


def test_a_fresh_install_has_the_column_at_the_new_version(tmp_path):
    for name, migrate in (("all", apply_all_migrations),
                          ("ensure", lambda c: ensure_migrations_applied(c, skip_backup=True))):
        conn = sqlite3.connect(str(tmp_path / f"{name}.db"))
        try:
            migrate(conn)
            assert WAITING_COLUMN in _cols(conn), name
            assert read_user_version(conn) == HEAD, name
        finally:
            conn.close()


@pytest.mark.parametrize("with_the_column", [False, True], ids=["as 1.4.4 left it", "with the column added in place"])
def test_a_database_of_the_release_before_ends_with_the_column_and_the_new_version(tmp_path, monkeypatch,
                                                                                   with_the_column):
    """Rule: registry order 81. Without it such a database stays at 80 and an older build opens it."""
    monkeypatch.setenv("TOPOS_BACKUP_DIR", str(tmp_path / "backups"))
    conn = sqlite3.connect(str(tmp_path / "database.db"))
    as_the_release_before(conn, with_the_column=with_the_column)
    store = BlackholeStore(conn)
    conn.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name, canonical_name, "
                 "aliases_json, processing_tier, rebuild_state, note) VALUES ('bh_aaaaaaaaaaaa', '', "
                 "'perrin ashgrove', 'Perrin Ashgrove', '[]', 'secure', 'complete', 'mine')")
    if with_the_column:
        conn.execute(f"UPDATE entity_blackholes SET {WAITING_COLUMN}=?", ('{"whole": true, "terms": ["perrin ashgrove"]}',))
    conn.commit()
    before = conn.execute("SELECT blackhole_id, normalized_name, canonical_name, aliases_json, processing_tier, "
                          "rebuild_state, note FROM entity_blackholes").fetchall()
    assert read_user_version(conn) == 80 and (WAITING_COLUMN in _cols(conn)) is with_the_column
    backup = ensure_migrations_applied(conn)
    assert read_user_version(conn) == HEAD and WAITING_COLUMN in _cols(conn)
    assert backup is not None and "database-pre-v" in backup              # the stamp moved: a backup first, as always
    assert conn.execute("SELECT blackhole_id, normalized_name, canonical_name, aliases_json, processing_tier, "
                        "rebuild_state, note FROM entity_blackholes").fetchall() == before
    marks = [row[0] for row in conn.execute(f"SELECT {WAITING_COLUMN} FROM entity_blackholes")]
    assert marks == (['{"whole": true, "terms": ["perrin ashgrove"]}'] if with_the_column else [None])
    assert [bool(entry["carried_waiting"]) for entry in store.list()] == [with_the_column]
    conn.close()


def test_a_build_that_predates_the_step_refuses_the_database(tmp_path, monkeypatch):
    """What the step is for. A build whose registry stops at 80 (every build before this one) meets a database
    this build has opened: the guard that already stands there refuses it, in its own words. The older tree's own
    code is run against such a database in the round's report; here the registry's head is put back by one."""
    conn = sqlite3.connect(str(tmp_path / "database.db"))
    ensure_migrations_applied(conn, skip_backup=True)
    assert read_user_version(conn) == HEAD
    monkeypatch.setattr(migrations, "max_migration_order", lambda: HEAD - 1)
    with pytest.raises(DowngradeGuardError) as refused:
        ensure_migrations_applied(conn, skip_backup=True)
    assert str(refused.value) == (
        "database was upgraded by a newer topos-node (PRAGMA user_version=81 > 80 known to this build); upgrade the "
        "package or restore the pre-upgrade backup under ~/.topos/backups/")
    conn.close()


def test_no_row_is_read_or_changed_and_rerunning_is_a_no_op():
    from topos.storage.db.migrations import off_limits_carried_waiting_v1 as step

    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    conn.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name, canonical_name, "
                 "aliases_json, processing_tier, rebuild_state) VALUES ('bh_aaaaaaaaaaaa', '', 'perrin ashgrove', "
                 "'Perrin Ashgrove', '[]', 'secure', 'pending')")
    conn.commit()
    rows = conn.execute("SELECT * FROM entity_blackholes").fetchall()
    schema = conn.execute("PRAGMA schema_version").fetchone()
    step.apply_off_limits_carried_waiting_v1_up(conn)
    assert conn.execute("PRAGMA schema_version").fetchone() == schema     # this step changed no DDL the second time
    apply_all_migrations(conn)
    assert conn.execute("SELECT * FROM entity_blackholes").fetchall() == rows
    assert conn.execute("SELECT COUNT(*) FROM wiki_schema_migrations WHERE migration_id=?",
                        (MIGRATION_ID,)).fetchone()[0] == 1


def test_a_table_made_later_still_gets_the_column():
    from topos.storage.db.migrations import off_limits_carried_waiting_v1 as step

    conn = sqlite3.connect(":memory:")
    step.apply_off_limits_carried_waiting_v1_up(conn)                     # no table yet: records the id, adds nothing
    conn.execute("CREATE TABLE entity_blackholes (blackhole_id TEXT PRIMARY KEY, normalized_name TEXT)")
    step.apply_off_limits_carried_waiting_v1_up(conn)
    assert WAITING_COLUMN in _cols(conn)
