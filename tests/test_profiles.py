"""Profile archive/switch: the multi-Topos-per-machine primitive.

Everything runs against a tmp_path standing in for ~/.topos — no live home
directory, no database opens. The invariants under test are the ones the
by-hand folder dance kept violating: WAL sidecars travel with the database,
non-allowlisted junk stays put, a crash mid-switch is recoverable, and a
running node refuses the operation outright.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from topos import profiles
from topos.cli.profile_cmd import profile_group

pytestmark = [pytest.mark.public]


def _seed_active(base: Path, *, key: str = "KEYAAA", db: bytes = b"sqlite-bytes") -> None:
    base.mkdir(parents=True, exist_ok=True)
    (base / ".env").write_text(f"TOPOS_KEY={key}\n")
    (base / "database.db").write_bytes(db)
    (base / "database.db-wal").write_bytes(b"wal")
    (base / "database.db-shm").write_bytes(b"shm")
    (base / "ingestion").mkdir(exist_ok=True)
    (base / "ingestion" / "file.txt").write_text("raw")
    (base / "config.yaml").write_text("a: 1\n")


def _seed_profile(base: Path, slug: str, *, name: str | None = None, key: str = "KEYBBB") -> Path:
    dest = base / "profiles" / slug
    dest.mkdir(parents=True)
    (dest / ".env").write_text(f"TOPOS_KEY={key}\n")
    (dest / "database.db").write_bytes(b"other-db")
    (dest / "profile.json").write_text(
        json.dumps({"profile_id": slug, "topos_name": name}) + "\n"
    )
    return dest


@pytest.fixture(autouse=True)
def _no_running_node(monkeypatch):
    monkeypatch.setattr(profiles, "node_is_running", lambda port=9000: False)


class TestNewProfile:
    def test_archives_allowlist_and_leaves_junk(self, tmp_path):
        _seed_active(tmp_path)
        (tmp_path / "database.db.bak-20260101").write_bytes(b"backup")  # junk stays
        (tmp_path / "logs").mkdir()
        (tmp_path / "logs" / "node.log").write_text("log")  # logs are machine-global

        result = profiles.new_profile(tmp_path)

        assert result["archived"] is True
        slug = result["archived_as"]
        archived = tmp_path / "profiles" / slug
        assert (archived / ".env").read_text().startswith("TOPOS_KEY=KEYAAA")
        assert (archived / "database.db-wal").exists()  # sidecars travel together
        assert (archived / "ingestion" / "file.txt").exists()
        assert json.loads((archived / "profile.json").read_text())["profile_id"] == slug
        # The machine is now fresh: nothing bindable left at the top level…
        assert not (tmp_path / ".env").exists()
        assert not (tmp_path / "database.db").exists()
        # …but machine-level files never moved.
        assert (tmp_path / "database.db.bak-20260101").exists()
        assert (tmp_path / "logs" / "node.log").exists()

    def test_noop_on_fresh_machine(self, tmp_path):
        result = profiles.new_profile(tmp_path)
        assert result == {"archived": False, "archived_as": None}

    def test_uses_marker_name_for_slug(self, tmp_path):
        _seed_active(tmp_path)
        profiles.set_active_name("Personal DB", tmp_path)
        result = profiles.new_profile(tmp_path)
        assert result["archived_as"] == "personal-db"
        meta = json.loads((tmp_path / "profiles" / "personal-db" / "profile.json").read_text())
        assert meta["topos_name"] == "Personal DB"

    def test_slug_collision_gets_suffix(self, tmp_path):
        _seed_active(tmp_path)
        _seed_profile(tmp_path, "work")
        result = profiles.new_profile(tmp_path, name="work")
        assert result["archived_as"] == "work-2"


class TestSwitchProfile:
    def test_roundtrip_swaps_identity_and_data(self, tmp_path):
        _seed_active(tmp_path, key="KEYAAA")
        profiles.set_active_name("First", tmp_path)
        _seed_profile(tmp_path, "work", name="Work", key="KEYBBB")

        result = profiles.switch_profile("work", tmp_path)

        assert result["activated"] == "work"
        assert result["archived_as"] == "first"
        assert (tmp_path / ".env").read_text().startswith("TOPOS_KEY=KEYBBB")
        assert (tmp_path / "database.db").read_bytes() == b"other-db"
        marker = json.loads((tmp_path / "active-profile.json").read_text())
        assert marker["profile_id"] == "work"
        assert marker["topos_name"] == "Work"
        # The old Topos is intact and switch-back-able.
        first = tmp_path / "profiles" / "first"
        assert (first / ".env").read_text().startswith("TOPOS_KEY=KEYAAA")
        assert (first / "database.db-wal").exists()
        # Switching back restores the original world.
        back = profiles.switch_profile("first", tmp_path)
        assert back["archived_as"] == "work"
        assert (tmp_path / ".env").read_text().startswith("TOPOS_KEY=KEYAAA")

    def test_switch_onto_fresh_machine_archives_nothing(self, tmp_path):
        _seed_profile(tmp_path, "work")
        result = profiles.switch_profile("work", tmp_path)
        assert result["archived_as"] is None
        assert (tmp_path / "database.db").read_bytes() == b"other-db"

    def test_unknown_profile_refused(self, tmp_path):
        _seed_active(tmp_path)
        with pytest.raises(profiles.ProfileError, match="No profile named"):
            profiles.switch_profile("nope", tmp_path)

    def test_refuses_while_node_running(self, tmp_path, monkeypatch):
        _seed_active(tmp_path)
        _seed_profile(tmp_path, "work")
        monkeypatch.setattr(profiles, "node_is_running", lambda port=9000: True)
        with pytest.raises(profiles.ProfileError, match="running"):
            profiles.switch_profile("work", tmp_path)
        # Nothing moved.
        assert (tmp_path / ".env").exists()

    def test_a_leftover_lock_file_does_not_block_forever(self, tmp_path):
        # The rebuild lock is an advisory flock that is never deleted: the OS
        # drops the lock when the child exits, the file stays. Refusing on the
        # file's existence blocked switching permanently on every machine that
        # had ever rebuilt its graph — found by hand on a week-old empty lock.
        _seed_active(tmp_path)
        _seed_profile(tmp_path, "work")
        (tmp_path / "database.db.rebuild.lock").touch()
        result = profiles.switch_profile("work", tmp_path)
        assert result["activated"] == "work"

    def test_refuses_while_a_rebuild_actually_holds_the_lock(self, tmp_path):
        import fcntl

        _seed_active(tmp_path)
        _seed_profile(tmp_path, "work")
        lock_path = tmp_path / "database.db.rebuild.lock"
        with open(lock_path, "a+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with pytest.raises(profiles.ProfileError, match="rebuild"):
                profiles.switch_profile("work", tmp_path)
        # Released: the same machine may now switch.
        assert profiles.switch_profile("work", tmp_path)["activated"] == "work"

    def test_a_platform_without_flock_does_not_block_forever(self, tmp_path, monkeypatch):
        # Windows has no fcntl, and the engine's acquire path has none either —
        # it falls back to a plain open, so nothing ever holds the file there
        # and its existence carries no information. Reporting a rebuild would
        # rebuild this very bug one platform over.
        import builtins

        real_import = builtins.__import__

        def no_fcntl(name, *args, **kwargs):
            if name == "fcntl":
                raise ImportError("no fcntl on this platform")
            return real_import(name, *args, **kwargs)

        (tmp_path / "database.db.rebuild.lock").touch()
        monkeypatch.setattr(builtins, "__import__", no_fcntl)
        assert profiles.rebuild_in_progress(tmp_path) is False

    def test_rebuild_probe_reports_holder_liveness(self, tmp_path):
        import fcntl

        lock_path = tmp_path / "database.db.rebuild.lock"
        assert profiles.rebuild_in_progress(tmp_path) is False  # no file at all
        lock_path.touch()
        assert profiles.rebuild_in_progress(tmp_path) is False  # stale leftover
        with open(lock_path, "a+") as held:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert profiles.rebuild_in_progress(tmp_path) is True


class TestCrashRecovery:
    def test_journal_rollback_restores_pre_switch_layout(self, tmp_path):
        _seed_active(tmp_path, key="KEYAAA")
        # Simulate a switch that died after moving two files out.
        dest = tmp_path / "profiles" / "half"
        dest.mkdir(parents=True)
        (tmp_path / ".env").rename(dest / ".env")
        (tmp_path / "database.db").rename(dest / "database.db")
        (tmp_path / ".profile-switch.json").write_text(
            json.dumps(
                {
                    "op": "switch",
                    "moves": [
                        [str(tmp_path / ".env"), str(dest / ".env")],
                        [str(tmp_path / "database.db"), str(dest / "database.db")],
                    ],
                }
            )
        )

        assert profiles.recover_interrupted_switch(tmp_path) is True
        assert (tmp_path / ".env").read_text().startswith("TOPOS_KEY=KEYAAA")
        assert (tmp_path / "database.db").exists()
        assert not (tmp_path / ".profile-switch.json").exists()

    def test_recovery_runs_automatically_before_listing(self, tmp_path):
        _seed_active(tmp_path)
        dest = tmp_path / "profiles" / "half"
        dest.mkdir(parents=True)
        (tmp_path / ".env").rename(dest / ".env")
        (tmp_path / ".profile-switch.json").write_text(
            json.dumps({"op": "switch", "moves": [[str(tmp_path / ".env"), str(dest / ".env")]]})
        )
        infos = profiles.list_profiles(tmp_path)
        assert (tmp_path / ".env").exists()
        assert any(i.active for i in infos)

    def test_failure_mid_switch_rolls_back(self, tmp_path):
        # A failure on the activate step (after the active profile was already
        # archived) must undo the whole operation, not stop half-moved.
        _seed_active(tmp_path, key="KEYAAA")
        _seed_profile(tmp_path, "work")
        original_move = profiles._move_allowlisted

        def exploding_move(base, journal, src_dir, dst_dir):
            if src_dir != base:  # the activate step
                raise profiles.ProfileError("boom")
            return original_move(base, journal, src_dir, dst_dir)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(profiles, "_move_allowlisted", exploding_move)
            with pytest.raises(profiles.ProfileError, match="boom"):
                profiles.switch_profile("work", tmp_path)

        # Active world restored exactly.
        assert (tmp_path / ".env").read_text().startswith("TOPOS_KEY=KEYAAA")
        assert (tmp_path / "database.db").exists()
        assert not (tmp_path / ".profile-switch.json").exists()


class TestQueries:
    def test_current_none_on_fresh_machine(self, tmp_path):
        tmp_path.mkdir(exist_ok=True)
        assert profiles.current_profile(tmp_path) is None

    def test_list_orders_active_first_with_sizes(self, tmp_path):
        _seed_active(tmp_path)
        profiles.set_active_name("Mine", tmp_path)
        _seed_profile(tmp_path, "work", name="Work")
        infos = profiles.list_profiles(tmp_path)
        assert [i.active for i in infos] == [True, False]
        assert infos[0].name == "Mine"
        assert infos[0].size_bytes > 0
        assert infos[1].profile_id == "work"
        assert infos[1].size_bytes > 0


class TestCli:
    def test_list_and_switch_json(self, tmp_path):
        _seed_active(tmp_path)
        profiles.set_active_name("Mine", tmp_path)
        _seed_profile(tmp_path, "work", name="Work")
        runner = CliRunner()

        listed = runner.invoke(profile_group, ["list", "--json", "--base", str(tmp_path)])
        assert listed.exit_code == 0, listed.output
        payload = json.loads(listed.output)
        assert [p["profile_id"] for p in payload["profiles"]] == ["mine", "work"]

        switched = runner.invoke(profile_group, ["switch", "work", "--json", "--base", str(tmp_path)])
        assert switched.exit_code == 0, switched.output
        assert json.loads(switched.output) == {"activated": "work", "archived_as": "mine"}

    def test_switch_error_is_clean_not_traceback(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(profile_group, ["switch", "nope", "--base", str(tmp_path)])
        assert result.exit_code != 0
        assert "No profile named 'nope'" in result.output

    def test_new_on_fresh_machine_is_noop_success(self, tmp_path):
        runner = CliRunner()
        result = runner.invoke(profile_group, ["new", "--json", "--base", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output) == {"archived": False, "archived_as": None}
