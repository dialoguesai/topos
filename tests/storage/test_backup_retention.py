"""What retention keeps, what it takes, and what it refuses to touch.

These backups are the entire rollback story — RELEASING.md sends owners to
``~/.topos/backups/database-pre-v{X}-*.db`` and nowhere else — so the tests that
matter here are the refusals. A retention policy that frees space by deleting
the file an owner needs after a bad upgrade has turned a full disk into an
unrecoverable node.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from topos.engine import disk_space
from topos.storage.db.migrations import backup as backup_mod

GB = 1024**3
REPO_ROOT = Path(__file__).resolve().parents[2]


def write_backup(directory: Path, name: str, mtime: float, size: int = 64) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    return path


def named(version: str, stamp: str, profile: str = "personaldb") -> str:
    return f"database-pre-v{version}--{profile}-{stamp}.db"


class TestTheLadderIsThreeRungs:
    def test_retention_keeps_three(self, tmp_path):
        """Two rungs leaves nowhere to stand when a bad upgrade lands on a bad one."""
        assert backup_mod.BACKUP_RETENTION == 3

        made = [
            write_backup(tmp_path, named("1.3.10", f"2026081{i}T000000Z"), 1e9 + i)
            for i in range(5)
        ]

        removed = backup_mod.prune_old_backups(tmp_path, profile_id="personaldb")

        assert set(removed) == {made[0], made[1]}
        assert all(path.is_file() for path in made[2:])

    def test_a_count_is_the_wrong_reason_to_delete_the_running_versions_rollback(
        self, tmp_path
    ):
        """``database-pre-vX`` is the only way back to pre-X schema while on X."""
        current = write_backup(tmp_path, named("1.3.32", "20260801T000000Z"), 1e9)
        newer = [
            write_backup(tmp_path, named("1.3.10", f"2026081{i}T000000Z"), 1e9 + 10 + i)
            for i in range(4)
        ]

        removed = backup_mod.prune_old_backups(
            tmp_path, profile_id="personaldb", installed="1.3.32"
        )

        assert current.is_file(), "the running version's rollback point is not a count"
        assert set(removed) == {newer[0]}, "everything else past three still goes"

    def test_a_downgraded_node_keeps_the_way_back_up(self, tmp_path):
        """After a downgrade, backups tagged ABOVE the running version are the route back."""
        ahead = write_backup(tmp_path, named("1.4.0", "20260801T000000Z"), 1e9)
        for i in range(4):
            write_backup(tmp_path, named("1.2.0", f"2026081{i}T000000Z"), 1e9 + 10 + i)

        backup_mod.prune_old_backups(tmp_path, profile_id="personaldb", installed="1.3.0")

        assert ahead.is_file()

    def test_a_boot_looping_migration_cannot_fill_the_disk(self, tmp_path):
        """One rollback point per VERSION, not per file.

        A migration that fails on every boot re-backs-up under the same tag each
        time. Protecting every file carrying that tag would defend the whole pile
        — filling the volume fastest exactly when a broken upgrade has the node
        in a loop.
        """
        loop = [
            write_backup(tmp_path, named("1.3.32", f"2026081{i}T000000Z"), 1e9 + i)
            for i in range(6)
        ]

        removed = backup_mod.prune_old_backups(
            tmp_path, profile_id="personaldb", installed="1.3.32"
        )

        assert loop[-1].is_file(), "the newest is the rollback point and stays"
        assert len(removed) == 3, "the newest three stay by count; the rest go"

    def test_an_unreadable_version_prunes_on_recency_rather_than_never(self, tmp_path):
        """A guard that fires on 'could not tell' is a directory that never prunes."""
        for i in range(5):
            write_backup(tmp_path, named("nightly", f"2026081{i}T000000Z"), 1e9 + i)

        removed = backup_mod.prune_old_backups(
            tmp_path, profile_id="personaldb", installed="1.3.32"
        )

        assert len(removed) == 2


class TestOneToposNeverSpendsAnothers:
    def test_condemned_is_counted_per_owner(self, tmp_path):
        """Each Topos gets its own budget of three — the old shared budget was the bug."""
        for i in range(5):
            write_backup(tmp_path, named("1.3.10", f"2026081{i}T000000Z", "personaldb"), 1e9 + i)
        theirs = [
            write_backup(tmp_path, named("1.3.10", f"2026081{i}T000000Z", "q4"), 1e9 + i)
            for i in range(2)
        ]

        condemned = backup_mod.condemned_backups(tmp_path, installed="1.3.32")

        assert all(entry.profile == "personaldb" for entry in condemned)
        assert len(condemned) == 2
        assert all(path.is_file() for path in theirs)

    def test_a_prefix_is_not_an_owner(self, tmp_path):
        """``q4`` and ``q4-2`` are different Topoi; a glob would conflate them."""
        for i in range(4):
            write_backup(tmp_path, named("1.3.10", f"2026081{i}T000000Z", "q4-2"), 1e9 + i)
        q4 = write_backup(tmp_path, named("1.3.10", "20260810T000000Z", "q4"), 1e9)

        condemned = backup_mod.condemned_backups(tmp_path, installed="1.3.32")

        assert q4 not in {entry.path for entry in condemned}
        assert len(condemned) == 1


class TestTheOwnersOwnSnapshotsAreNotOursToSpend:
    def test_untagged_snapshots_are_reported_and_never_pruned(self, tmp_path):
        """No version tag means nothing to reason with — so we report, not delete."""
        manual = [
            write_backup(tmp_path, "pre-unify-20260825-220047.db", 1e9, size=2048),
            write_backup(tmp_path, "database-pre-case-backfill-20260826-175220.db", 1e9, size=1024),
        ]
        for i in range(5):
            write_backup(tmp_path, named("1.3.10", f"2026081{i}T000000Z"), 1e9 + i)

        removed, _ = backup_mod.prune_to_retention(tmp_path, installed="1.3.32")

        assert all(path.is_file() for path in manual)
        assert not any(path in removed for path in manual)
        assert {path.name for path in backup_mod.untracked_snapshots(tmp_path)} == {
            path.name for path in manual
        }

    def test_sidecars_are_counted_with_the_snapshot_that_names_them(self, tmp_path):
        write_backup(tmp_path, "pre-derivation-backfill-20260826-052948.db", 1e9, size=1000)
        write_backup(tmp_path, "pre-derivation-backfill-20260826-052948.db-wal", 1e9, size=500)

        found = backup_mod.untracked_snapshots(tmp_path)

        assert len(found) == 2
        assert backup_mod.retention_report(tmp_path, installed="1.3.32")["manual_bytes"] == 1500
        assert backup_mod.retention_report(tmp_path, installed="1.3.32")["manual_count"] == 1

    def test_an_adopted_legacy_database_is_data_not_a_snapshot(self, tmp_path):
        """``orphaned-legacy-*/`` holds a database, not a copy of one."""
        write_backup(tmp_path / "orphaned-legacy-20260817", "database.db", 1e9, size=4096)

        assert backup_mod.untracked_snapshots(tmp_path) == []


class TestTheReportSplitsWhoMayReclaimWhat:
    def test_three_numbers_because_there_are_three_answers(self, tmp_path):
        for i in range(5):
            write_backup(tmp_path, named("1.3.10", f"2026081{i}T000000Z"), 1e9 + i, size=100)
        write_backup(tmp_path, "pre-unify-20260825-220047.db", 1e9, size=700)

        report = backup_mod.retention_report(tmp_path, installed="1.3.32")

        assert report["prunable_bytes"] == 200, "the two past the ladder"
        assert report["retained_bytes"] == 300, "the ladder itself, never reclaimable"
        assert report["manual_bytes"] == 700, "real space, but only the owner may say"

    def test_pruning_frees_the_bytes_it_reported(self, tmp_path):
        for i in range(5):
            write_backup(tmp_path, named("1.3.10", f"2026081{i}T000000Z"), 1e9 + i, size=100)
        promised = backup_mod.retention_report(tmp_path, installed="1.3.32")["prunable_bytes"]

        removed, freed = backup_mod.prune_to_retention(tmp_path, installed="1.3.32")

        assert freed == promised == 200
        assert len(removed) == 2
        assert backup_mod.retention_report(tmp_path, installed="1.3.32")["prunable_bytes"] == 0


@pytest.mark.usefixtures("node_backups_handed_over")
class TestTheFloorCanSeeTheBackups:
    """With custody handed over, as node startup hands it over."""

    def test_a_different_volume_reports_nothing_rather_than_a_misleading_number(
        self, tmp_path, monkeypatch
    ):
        """Deleting on the home volume does not make room on a second drive."""
        monkeypatch.setenv("TOPOS_BACKUP_DIR", str(tmp_path))
        write_backup(tmp_path, named("1.3.10", "20260810T000000Z"), 1e9, size=100)

        with patch.object(disk_space, "on_same_volume", return_value=False):
            space = disk_space.backup_space()

        assert space["applies"] is False
        assert space["prunable_bytes"] == 0

    def test_an_unreadable_device_is_not_a_match(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOPOS_BACKUP_DIR", str(tmp_path))

        with patch.object(disk_space, "on_same_volume", return_value=None):
            assert disk_space.backup_space()["applies"] is False

    def test_the_status_payload_carries_what_the_floor_needs(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOPOS_BACKUP_DIR", str(tmp_path))
        for i in range(5):
            write_backup(tmp_path, named("1.3.10", f"2026081{i}T000000Z"), 1e9 + i, size=100)

        # Pin the floor so `disk_status` does not open a database: resolving it
        # for real connects the guard DB, which migrates, which writes its own
        # pre-migration backup into the directory under test.
        with patch.object(disk_space, "on_same_volume", return_value=True), patch.object(
            disk_space, "min_free_bytes", return_value=10 * GB
        ):
            status = disk_space.disk_status()

        assert status["schema"] == 2
        assert status["backups"]["applies"] is True
        assert status["backups"]["prunable_bytes"] == 200
        assert status["backups"]["retained_count"] == 3


@pytest.mark.usefixtures("node_backups_withheld")
class TestCustodyIsWhatTurnsTheLaneOn:
    """SYS-node I1: the floor reports on no backups it was not handed.

    The engine plane is meant to be movable to a second machine, so it never
    resolves a database path of its own. A process that did not hand its backups
    over — a remote engine, the CLI — has none to report, even standing in a
    directory full of them. Both tests below stand in the same directory; the
    only difference between them is custody.
    """

    def test_the_status_payload_reports_none(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOPOS_BACKUP_DIR", str(tmp_path))
        write_backup(tmp_path, named("1.3.10", "20260810T000000Z"), 1e9, size=100)

        with patch.object(disk_space, "on_same_volume", return_value=True), patch.object(
            disk_space, "min_free_bytes", return_value=10 * GB
        ):
            status = disk_space.disk_status()

        assert disk_space.node_backups() is None
        assert status["backups"]["applies"] is False
        assert status["backups"]["prunable_bytes"] == 0

    def test_handing_them_over_is_what_turns_the_lane_on(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TOPOS_BACKUP_DIR", str(tmp_path))
        for i in range(5):
            write_backup(tmp_path, named("1.3.10", f"2026081{i}T000000Z"), 1e9 + i, size=100)

        from topos.storage.db.migrations.backup_custody import ActiveNodeBackups

        monkeypatch.setattr(disk_space, "_CUSTODY", ActiveNodeBackups())
        with patch.object(disk_space, "on_same_volume", return_value=True):
            assert disk_space.backup_space()["prunable_bytes"] == 200


class TestTheHandoverIsActuallyWired:
    """The seam is only worth anything if node startup crosses it.

    Rule 0 goes quiet if nothing hands the backups over, and quiet is the one
    failure mode it has: the node keeps working, keeps honouring the floor, and
    keeps paying for it in re-downloaded models while a directory of condemned
    database copies sits beside them. Nothing else in the suite notices.
    """

    def test_startup_hands_them_over(self):
        source = (REPO_ROOT / "topos" / "app.py").read_text(encoding="utf-8")
        startup = next(
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "startup_event"
        )
        called = {
            node.func.id
            for node in ast.walk(startup)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert "hand_backups_to_the_disk_floor" in called, (
            "Node startup no longer hands its backups to the disk floor, so "
            "model_manager rule 0 is inert: the node will evict a model it has "
            "to re-download while superseded backups sit on the same volume."
        )

    def test_the_handover_installs_custody(self, monkeypatch):
        from topos.storage.db.migrations.backup_custody import (
            hand_backups_to_the_disk_floor,
        )

        monkeypatch.setattr(disk_space, "_CUSTODY", None)
        hand_backups_to_the_disk_floor()

        custody = disk_space.node_backups()
        assert custody is not None
        assert custody.directory() == Path(os.environ["TOPOS_BACKUP_DIR"])
