"""Snapshot isolation, using temporary databases only."""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "snapshot_owner_db_under_test", Path(__file__).parents[2] / "scripts" / "snapshot_owner_db.py",
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def seed(path):
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE records(value TEXT)")
    conn.execute("INSERT INTO records VALUES ('source')")
    conn.commit()
    return conn


def test_backup_includes_committed_wal_and_isolates_writes(tmp_path):
    source = tmp_path / "owner.db"
    conn = seed(source)
    try:
        dest = module.snapshot(source, tmp_path / "copy.db")
        with sqlite3.connect(dest) as copy:
            assert copy.execute("SELECT value FROM records").fetchall() == [("source",)]
            copy.execute("UPDATE records SET value='canary'")
        assert conn.execute("SELECT value FROM records").fetchall() == [("source",)]
        assert dest.stat().st_mode & 0o777 == 0o600
    finally:
        conn.close()


@pytest.mark.parametrize("alias", ["same", "symlink", "hardlink", "existing", "dangling"])
def test_rejects_existing_or_source_destinations_without_overwrite(tmp_path, alias):
    source = tmp_path / "source.db"
    conn = seed(source)
    conn.close()
    dest = tmp_path / "dest.db"
    if alias == "same":
        dest = source
    elif alias == "symlink":
        dest.symlink_to(source)
    elif alias == "hardlink":
        dest.hardlink_to(source)
    elif alias == "existing":
        dest.write_bytes(b"preserve me")
    else:
        dest.symlink_to(tmp_path / "missing.db")
    before = source.read_bytes()
    with pytest.raises(SystemExit):
        module.snapshot(source, dest)
    assert source.read_bytes() == before
    if alias == "existing":
        assert dest.read_bytes() == b"preserve me"
    if alias == "dangling":
        assert not (tmp_path / "missing.db").exists()


def test_source_name_cannot_change_read_only_uri_options(tmp_path):
    source = tmp_path / "owner?mode=rw#copy.db"
    seed(source).close()
    dest = module.snapshot(source, tmp_path / "copy.db")
    with sqlite3.connect(dest) as conn:
        assert conn.execute("SELECT value FROM records").fetchone() == ("source",)
