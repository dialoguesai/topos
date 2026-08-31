"""Archived Topoi: self-contained, self-describing, and not each other's.

Three failures this pins down, all of them invisible while they happen:

* an archive kept a hot WAL, so the file could not be read without its
  sidecars — which is why the switch preflight could not learn the schema
  version of the profile it was about to activate;
* an archive recorded nothing about what it was last used WITH, so every
  question about it (how big, which engine, which Topos even) meant opening a
  database or guessing from a display name;
* every Topos wrote pre-migration backups into one shared directory under
  indistinguishable names, and retention kept "the newest 2" across the lot —
  so letting a second Topos migrate deleted the first one's safety net.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from topos import profiles
from topos.defaults import DEFAULT_NODE_PORT
from topos.storage.db.migrations import backup as backup_mod

pytestmark = [pytest.mark.public]


def _real_database(path: Path, rows: int = 3) -> None:
    """A WAL-mode SQLite database with an un-checkpointed WAL beside it.

    Built in a staging directory and COPIED into place while its connection is
    still open: SQLite checkpoints and deletes the WAL on a clean close, so a
    hot WAL is not something you can leave behind by closing carefully. This is
    the state a node that was killed (or a database moved by hand) leaves.
    """
    import shutil

    path.parent.mkdir(parents=True, exist_ok=True)
    stage = path.parent / "_stage"
    stage.mkdir(parents=True, exist_ok=True)
    staged = stage / path.name
    conn = sqlite3.connect(staged)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        for i in range(rows):
            conn.execute("INSERT INTO t (id) VALUES (?)", (i,))
        conn.commit()
        for suffix in ("", "-wal", "-shm"):
            src = staged.with_name(staged.name + suffix)
            if src.is_file():
                shutil.copy2(src, path.with_name(path.name + suffix))
    finally:
        conn.close()
    shutil.rmtree(stage)


def _seed_active(base: Path, *, key: str = "KEYAAA", real_db: bool = True) -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / ".env").write_text(f"TOPOS_KEY={key}\n")
    if real_db:
        _real_database(base / profiles.DATABASE_FILENAME)
    else:
        (base / profiles.DATABASE_FILENAME).write_bytes(b"not-a-database")
        (base / f"{profiles.DATABASE_FILENAME}-wal").write_bytes(b"wal")


@pytest.fixture(autouse=True)
def _no_running_node(monkeypatch):
    monkeypatch.setattr(profiles, "node_is_running", lambda port=DEFAULT_NODE_PORT: False)


class TestArchiveIsSelfContained:
    def test_wal_is_folded_in_before_the_database_is_put_away(self, tmp_path):
        _seed_active(tmp_path)
        wal = tmp_path / f"{profiles.DATABASE_FILENAME}-wal"
        assert wal.stat().st_size > 0, "fixture should start with a hot WAL"

        profiles.new_profile(tmp_path, name="PersonalDB")

        archived = tmp_path / "profiles" / "personaldb" / profiles.DATABASE_FILENAME
        archived_wal = archived.with_name(f"{archived.name}-wal")
        assert not archived_wal.exists() or archived_wal.stat().st_size == 0
        # And the archived file alone answers, which is what the preflight needs.
        conn = sqlite3.connect(f"file:{archived}?mode=ro", uri=True)
        try:
            assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 3
        finally:
            conn.close()

    def test_a_file_that_is_not_a_database_is_never_opened(self, tmp_path):
        """Its sidecars must survive: sqlite3 can delete the sidecars of a file
        it fails to parse, and an archive must not lose bytes it cannot read."""
        _seed_active(tmp_path, real_db=False)

        profiles.new_profile(tmp_path, name="Scrambled")

        archived = tmp_path / "profiles" / "scrambled"
        assert (archived / profiles.DATABASE_FILENAME).read_bytes() == b"not-a-database"
        assert (archived / f"{profiles.DATABASE_FILENAME}-wal").read_bytes() == b"wal"


class TestArchiveStamps:
    def test_archive_records_what_it_was_last_used_with(self, tmp_path):
        _seed_active(tmp_path)

        profiles.new_profile(tmp_path, name="PersonalDB")

        meta = json.loads(
            (tmp_path / "profiles" / "personaldb" / profiles.PROFILE_META_FILENAME).read_text()
        )
        assert meta["engine_version"]
        assert meta["size_bytes"] > 0
        assert len(meta["key_fingerprint"]) == 12
        assert meta["schema_version"] == 0  # a database with no migrations run
        assert meta["last_active_at"]

    def test_listing_profiles_opens_no_database(self, tmp_path, monkeypatch):
        """The tray lists profiles on every menu render; that has to stay a
        folder read, which is the whole point of stamping at archive time."""
        _seed_active(tmp_path)
        profiles.new_profile(tmp_path, name="PersonalDB")

        def _no_connections(*_args, **_kwargs):
            raise AssertionError("listing profiles must not open a database")

        monkeypatch.setattr(sqlite3, "connect", _no_connections)

        listed = profiles.list_profiles(tmp_path)

        archived = [p for p in listed if not p.active]
        assert archived[0].engine_version
        assert archived[0].schema_version == 0
        assert archived[0].size_bytes > 0

    def test_same_key_twice_is_recorded_not_hidden(self, tmp_path):
        """"Start Fresh" deliberately leaves two profiles on one key. The menu
        should be able to say so instead of showing two identical-looking rows."""
        _seed_active(tmp_path, key="SAMEKEY")
        profiles.new_profile(tmp_path, name="Mine")
        _seed_active(tmp_path, key="SAMEKEY")
        profiles.new_profile(tmp_path, name="Mine")

        first, second = (
            json.loads((tmp_path / "profiles" / slug / profiles.PROFILE_META_FILENAME).read_text())
            for slug in ("mine", "mine-2")
        )
        assert first["key_fingerprint"] == second["key_fingerprint"]
        assert second["same_topos_as"] == ["mine"]

    def test_same_name_different_key_is_two_topoi(self, tmp_path):
        """The case on the machine this came from: two archived profiles both
        called "q4", bound to different Topoi."""
        _seed_active(tmp_path, key="KEY-ONE")
        profiles.new_profile(tmp_path, name="q4")
        _seed_active(tmp_path, key="KEY-TWO")
        profiles.new_profile(tmp_path, name="q4")

        listed = {p.profile_id: p for p in profiles.list_profiles(tmp_path)}
        assert listed["q4"].key_fingerprint != listed["q4-2"].key_fingerprint
        assert "same_topos_as" not in json.loads(
            (tmp_path / "profiles" / "q4-2" / profiles.PROFILE_META_FILENAME).read_text()
        )


class TestPreflightUsesTheStamp:
    def test_falls_back_to_the_recorded_schema_version(self, tmp_path):
        """An archive whose database will not open read-only still carries its
        schema version beside it."""
        _seed_active(tmp_path, key="CURRENT")
        dest = tmp_path / "profiles" / "from-the-future"
        dest.mkdir(parents=True)
        (dest / ".env").write_text("TOPOS_KEY=FUTURE\n")
        (dest / profiles.DATABASE_FILENAME).write_bytes(b"unreadable")
        (dest / profiles.PROFILE_META_FILENAME).write_text(
            json.dumps({"profile_id": "from-the-future", "schema_version": 99_999})
        )

        with pytest.raises(profiles.ProfileError) as excinfo:
            profiles.switch_profile("from-the-future", tmp_path)

        assert "newer version of Topos" in str(excinfo.value)


class TestBackupsBelongToOneTopos:
    def _backup(self, directory: Path, name: str, mtime: float) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_bytes(b"backup")
        import os

        os.utime(path, (mtime, mtime))
        return path

    def test_one_topos_never_prunes_anothers(self, tmp_path):
        mine = [
            self._backup(tmp_path, f"database-pre-v1.3.19--personaldb-2026081{i}T000000Z.db", 1e9 + i)
            for i in range(4)
        ]
        theirs = self._backup(
            tmp_path, "database-pre-v1.3.19--q4-20260810T000000Z.db", 1e9
        )

        removed = backup_mod.prune_old_backups(tmp_path, keep=2, profile_id="personaldb")

        assert set(removed) == {mine[0], mine[1]}
        assert theirs.is_file()

    def test_a_prefix_is_not_a_match(self, tmp_path):
        """``q4`` and ``q4-2`` are different Topoi; a glob would conflate them."""
        q4 = self._backup(tmp_path, "database-pre-v1.3.19--q4-20260810T000000Z.db", 1e9)
        q4_2 = [
            self._backup(tmp_path, f"database-pre-v1.3.19--q4-2-2026081{i}T000000Z.db", 1e9 + i)
            for i in range(3)
        ]

        removed = backup_mod.prune_old_backups(tmp_path, keep=1, profile_id="q4-2")

        assert q4.is_file()
        assert set(removed) == {q4_2[0], q4_2[1]}

    def test_backups_from_before_namespacing_are_left_alone(self, tmp_path):
        legacy = self._backup(tmp_path, "database-pre-v1.3.15-20260815T044225Z.db", 1e9)
        self._backup(tmp_path, "database-pre-v1.3.19--personaldb-20260817T000000Z.db", 1e9 + 5)

        removed = backup_mod.prune_old_backups(tmp_path, keep=0, profile_id="personaldb")

        assert legacy.is_file(), "nothing can prove which Topos an un-named backup came from"
        assert len(removed) == 1

    def test_owner_parses_both_shapes(self):
        assert backup_mod.backup_owner("database-pre-v1.3.19--q4-2-20260817T000000Z.db") == "q4-2"
        assert backup_mod.backup_owner("database-pre-v1.3.15-20260815T044225Z.db") is None
        assert backup_mod.backup_owner("something-else.db") is None
