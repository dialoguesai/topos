"""Which database does this node serve?

One resolver answers that, and these are the invariants it exists to hold. The
bug they were written against: on 2026-08-17 a newly created Topos — an active
slot with no ``database.db`` in it yet — resolved to
``~/Library/Application Support/ToposEngine/database.db``, a leftover from a
pre-profile install. The node migrated that file in place, ran a full session
against it, and no surface said so; a profile switch would never have carried
the data anywhere, because no profile owned it.

Everything runs against a tmp_path standing in for ~/.topos. No live home
directory is read and no live database is opened.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from topos.defaults import DEFAULT_NODE_PORT
from topos.storage.db import paths

pytestmark = [pytest.mark.public]


def _seed_slot(base: Path, *, marker: bool = True) -> None:
    """A machine that uses the profile layout, with an EMPTY active slot."""
    base.mkdir(parents=True, exist_ok=True)
    (base / paths.PROFILES_DIRNAME).mkdir(exist_ok=True)
    if marker:
        (base / paths.ACTIVE_MARKER_FILENAME).write_text(
            json.dumps({"profile_id": "personaldb", "topos_name": "PersonalDB"}) + "\n"
        )


def _seed_legacy(tmp_path: Path, monkeypatch, *, name: str = "ToposEngine") -> Path:
    """A pre-profile database somewhere outside ~/.topos."""
    legacy = tmp_path / "legacy" / name / paths.DATABASE_FILENAME
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"legacy-db")
    (legacy.parent / f"{paths.DATABASE_FILENAME}-wal").write_bytes(b"legacy-wal")
    monkeypatch.setattr(paths, "legacy_database_candidates", lambda: [legacy])
    return legacy


@pytest.fixture(autouse=True)
def _no_settings_pin(monkeypatch):
    """No explicit database_path unless a test sets one."""
    from topos.config.settings import settings

    monkeypatch.setattr(settings, "topos_database_path", None, raising=False)


class TestEmptySlot:
    def test_empty_slot_with_profiles_never_reaches_for_a_legacy_database(
        self, tmp_path, monkeypatch
    ):
        """THE regression. A new Topos owns its slot before it writes to it."""
        base = tmp_path / ".topos"
        _seed_slot(base)
        legacy = _seed_legacy(tmp_path, monkeypatch)

        resolved = paths.resolve_active_database(base, adopt=True)

        assert resolved.path == base / paths.DATABASE_FILENAME
        assert resolved.source == paths.SOURCE_NEW_SLOT
        assert resolved.profile_id == "personaldb"
        assert resolved.in_active_slot
        # And nothing was copied out of the legacy location either way.
        assert not (base / paths.DATABASE_FILENAME).exists()
        assert legacy.read_bytes() == b"legacy-db"

    def test_profiles_dir_alone_is_enough(self, tmp_path, monkeypatch):
        """A machine mid-switch has no marker — the profiles dir still says
        "this machine has Topoi" and the slot is still the answer."""
        base = tmp_path / ".topos"
        _seed_slot(base, marker=False)
        _seed_legacy(tmp_path, monkeypatch)

        resolved = paths.resolve_active_database(base, adopt=True)

        assert resolved.path == base / paths.DATABASE_FILENAME
        assert resolved.profile_id is None

    def test_existing_slot_database_wins(self, tmp_path, monkeypatch):
        base = tmp_path / ".topos"
        _seed_slot(base)
        (base / paths.DATABASE_FILENAME).write_bytes(b"the-real-one")
        _seed_legacy(tmp_path, monkeypatch)

        resolved = paths.resolve_active_database(base)

        assert resolved.path == base / paths.DATABASE_FILENAME
        assert resolved.source == paths.SOURCE_SLOT


class TestLegacyAdoption:
    def test_first_run_adopts_into_the_slot(self, tmp_path, monkeypatch):
        """No profiles ever on this machine: the legacy database is pulled in,
        sidecars and all, and served from inside the slot."""
        base = tmp_path / ".topos"
        legacy = _seed_legacy(tmp_path, monkeypatch)

        resolved = paths.resolve_active_database(base, adopt=True)

        assert resolved.source == paths.SOURCE_ADOPTED
        assert resolved.path == base / paths.DATABASE_FILENAME
        assert resolved.adopted_from == legacy
        assert (base / paths.DATABASE_FILENAME).read_bytes() == b"legacy-db"
        assert (base / f"{paths.DATABASE_FILENAME}-wal").read_bytes() == b"legacy-wal"
        # Copied, never moved: the original stays as its own backup.
        assert legacy.is_file()

    def test_resolution_without_adopt_has_no_side_effects(self, tmp_path, monkeypatch):
        """The per-API-call path must not copy files — but it must still name
        the file that would actually be served, or the size readout and the
        connection disagree."""
        base = tmp_path / ".topos"
        legacy = _seed_legacy(tmp_path, monkeypatch)

        resolved = paths.resolve_active_database(base)

        assert resolved.path == legacy
        assert resolved.source == paths.SOURCE_LEGACY
        assert not resolved.in_active_slot
        assert not (base / paths.DATABASE_FILENAME).exists()

    def test_a_newer_databases_adoption_is_explained_first(self, tmp_path, monkeypatch, caplog):
        """A machine that downgraded its node still gets its data — and a
        warning that pre-explains the downgrade guard, instead of a bare
        startup failure. Adopting beats starting empty: an empty Topos looks
        like the data is gone."""
        import logging

        from topos.storage.db.migrations import max_migration_order

        base = tmp_path / ".topos"
        legacy = tmp_path / "legacy" / paths.DATABASE_FILENAME
        legacy.parent.mkdir(parents=True)
        conn = sqlite3.connect(legacy)
        try:
            conn.execute(f"PRAGMA user_version = {max_migration_order() + 7}")
        finally:
            conn.close()
        monkeypatch.setattr(paths, "legacy_database_candidates", lambda: [legacy])

        with caplog.at_level(logging.WARNING, logger="topos.storage.db.paths"):
            resolved = paths.resolve_active_database(base, adopt=True)

        assert resolved.source == paths.SOURCE_ADOPTED
        assert "written by a NEWER version" in caplog.text

    def test_newest_legacy_database_wins(self, tmp_path, monkeypatch):
        older = tmp_path / "legacy" / "a" / paths.DATABASE_FILENAME
        newer = tmp_path / "legacy" / "b" / paths.DATABASE_FILENAME
        for path, mtime in ((older, 1_000_000), (newer, 2_000_000)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"db")
            import os

            os.utime(path, (mtime, mtime))
        monkeypatch.setattr(paths, "legacy_database_candidates", lambda: [older, newer])

        assert paths.newest_legacy_database() == newer

    def test_nothing_anywhere_resolves_to_the_slot(self, tmp_path, monkeypatch):
        base = tmp_path / ".topos"
        monkeypatch.setattr(paths, "legacy_database_candidates", list)

        resolved = paths.resolve_active_database(base, adopt=True)

        assert resolved.path == base / paths.DATABASE_FILENAME
        assert resolved.source == paths.SOURCE_NEW_SLOT


class TestTheBindingIsAnnounced:
    """The binding is decided and stated BEFORE the first connection exists.

    It first lived in `startup_event`, gated on "no connection yet" — but the
    CLI opens the owner connection before uvicorn starts (to print pending
    consent steps), so the gate was never true in a real run. The line never
    appeared on a live node and adoption never got its one useful moment. It
    now hangs off the code that is about to open the connection.
    """

    @pytest.fixture(autouse=True)
    def _fresh_binding(self, monkeypatch):
        from topos.core import state

        monkeypatch.setattr(state, "_binding_announced", False, raising=False)
        monkeypatch.setattr(state, "db_conn", None, raising=False)
        monkeypatch.setattr(state, "active_database", None, raising=False)

    def test_opening_a_connection_announces_the_binding(self, tmp_path, monkeypatch, caplog):
        import logging

        from topos.core import state

        base = tmp_path / ".topos"
        _seed_slot(base)
        (base / paths.DATABASE_FILENAME).write_bytes(b"active")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        with caplog.at_level(logging.INFO, logger="topos.core.state"):
            state.bind_active_database()

        assert "Serving database" in caplog.text
        assert "source=slot" in caplog.text and "profile=personaldb" in caplog.text
        assert state.active_database.path == base / paths.DATABASE_FILENAME

    def test_actually_opening_a_connection_triggers_it(self, tmp_path, monkeypatch, caplog):
        """The WIRING, not just the function.

        The first version of this hung off `startup_event` behind a condition
        that is never true in a real run, and every test called the function
        directly — so nothing failed while the live node said nothing at all.
        This one goes through `get_db_connection`, which is what production
        does.
        """
        import logging

        from topos.core import state

        _seed_slot(tmp_path / ".topos")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(state, "_db_conn_path", None, raising=False)
        monkeypatch.setattr(state, "_conn_owner_thread", None, raising=False)

        with caplog.at_level(logging.INFO, logger="topos.core.state"):
            conn = state.get_db_connection()
        try:
            assert conn is not None
            assert "Serving database" in caplog.text
            assert str(tmp_path) in caplog.text  # the temp home, not the real one
        finally:
            if conn is not None:
                conn.close()

    def test_it_announces_once(self, tmp_path, monkeypatch, caplog):
        import logging

        from topos.core import state

        _seed_slot(tmp_path / ".topos")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        with caplog.at_level(logging.INFO, logger="topos.core.state"):
            state.bind_active_database()
            state.bind_active_database()

        assert caplog.text.count("Serving database") == 1

    def test_an_injected_connection_is_left_alone(self, tmp_path, monkeypatch, caplog):
        """A test harness that supplied its own connection already decided the
        binding; adopting on top of that would write into a real home."""
        import logging
        import sqlite3

        from topos.core import state

        monkeypatch.setattr(state, "db_conn", sqlite3.connect(":memory:"), raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        with caplog.at_level(logging.INFO, logger="topos.core.state"):
            state.bind_active_database()

        assert "Serving database" not in caplog.text
        assert state.active_database is None


class TestOneResolver:
    """Every consumer of "which database?" gives the same answer.

    Four searches with four candidate lists used to disagree in ways nobody
    could see — the size shown in the app could describe a different file than
    the one being read.
    """

    def test_settings_pin_wins_everywhere(self, tmp_path, monkeypatch):
        from topos.config.settings import settings

        pinned = tmp_path / "pinned.db"
        pinned.write_bytes(b"x" * 42)
        monkeypatch.setattr(settings, "topos_database_path", str(pinned), raising=False)

        resolved = paths.resolve_active_database(tmp_path / ".topos")

        assert resolved.path == pinned
        assert resolved.source == paths.SOURCE_SETTINGS
        assert paths.get_local_database_size_bytes() == 42

    def test_answers_on_an_unpaired_machine(self, tmp_path, monkeypatch):
        """``Settings()`` refuses to build without a TOPOS_KEY, and the
        diagnostic that names the database is exactly what someone runs when
        their install is not working yet."""

        def _no_settings(*_args, **_kwargs):
            raise ValueError("TOPOS_KEY is required")

        monkeypatch.setattr(
            "topos.config.settings.Settings", _no_settings, raising=False
        )
        monkeypatch.setitem(
            __import__("sys").modules, "topos.config.settings", None
        )
        monkeypatch.setenv("TOPOS_DATABASE_PATH", str(tmp_path / "from-env.db"))

        resolved = paths.resolve_active_database(tmp_path / ".topos")

        assert resolved.path == tmp_path / "from-env.db"
        assert resolved.source == paths.SOURCE_SETTINGS

    def test_connection_path_and_size_agree(self, tmp_path, monkeypatch):
        from topos.core.state import _resolve_database_path_from_settings

        base = tmp_path / ".topos"
        _seed_slot(base)
        (base / paths.DATABASE_FILENAME).write_bytes(b"y" * 30)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

        assert _resolve_database_path_from_settings() == base / paths.DATABASE_FILENAME
        assert paths.get_local_database_size_bytes() == 30

    def test_discover_names_the_served_database_and_labels_strays(self, tmp_path, monkeypatch):
        """``--discover`` is what someone runs to find their database. It used
        to print a legacy stray first and never mention the active slot at all,
        which is not merely incomplete — it is the wrong answer."""
        from click.testing import CliRunner

        from topos.cli.commands import main

        base = tmp_path / ".topos"
        _seed_slot(base)
        (base / paths.DATABASE_FILENAME).write_bytes(b"active")
        legacy = _seed_legacy(tmp_path, monkeypatch)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(paths, "load_config", dict)

        output = CliRunner().invoke(main, ["--discover"]).output

        assert f"Active database: {base / paths.DATABASE_FILENAME}" in output
        assert "Topos 'personaldb'" in output
        assert "not served" in output and str(legacy) in output

    def test_discover_lists_the_active_database_then_strays(self, tmp_path, monkeypatch):
        base = tmp_path / ".topos"
        _seed_slot(base)
        (base / paths.DATABASE_FILENAME).write_bytes(b"active")
        legacy = _seed_legacy(tmp_path, monkeypatch)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.setattr(paths, "load_config", dict)

        found = paths.discover_databases()

        assert found[0] == base / paths.DATABASE_FILENAME
        assert legacy in found  # the stray is visible instead of silently served


class TestSwitchPreflight:
    """A Topos from a newer engine is refused BEFORE its files move."""

    def _profile_with_schema(self, base: Path, slug: str, user_version: int) -> Path:
        from topos import profiles

        dest = base / profiles.PROFILES_DIRNAME / slug
        dest.mkdir(parents=True)
        (dest / ".env").write_text("TOPOS_KEY=KEYBBB\n")
        (dest / profiles.PROFILE_META_FILENAME).write_text(
            json.dumps({"profile_id": slug}) + "\n"
        )
        conn = sqlite3.connect(dest / paths.DATABASE_FILENAME)
        try:
            conn.execute(f"PRAGMA user_version = {user_version}")
        finally:
            conn.close()
        return dest

    @pytest.fixture(autouse=True)
    def _no_running_node(self, monkeypatch):
        from topos import profiles

        monkeypatch.setattr(profiles, "node_is_running", lambda port=DEFAULT_NODE_PORT: False)

    def test_refuses_a_topos_from_a_newer_engine(self, tmp_path):
        from topos import profiles
        from topos.storage.db.migrations import max_migration_order

        (tmp_path / ".env").write_text("TOPOS_KEY=KEYAAA\n")
        (tmp_path / paths.DATABASE_FILENAME).write_bytes(b"current")
        self._profile_with_schema(tmp_path, "from-the-future", max_migration_order() + 5)

        with pytest.raises(profiles.ProfileError) as excinfo:
            profiles.switch_profile("from-the-future", tmp_path)

        assert "newer version of Topos" in str(excinfo.value)
        # Refused, not half-done: the active Topos never moved.
        assert (tmp_path / paths.DATABASE_FILENAME).read_bytes() == b"current"
        assert not (tmp_path / profiles.JOURNAL_FILENAME).exists()

    def test_allows_a_topos_this_build_can_migrate(self, tmp_path):
        from topos import profiles

        (tmp_path / ".env").write_text("TOPOS_KEY=KEYAAA\n")
        (tmp_path / paths.DATABASE_FILENAME).write_bytes(b"current")
        self._profile_with_schema(tmp_path, "older", 1)

        result = profiles.switch_profile("older", tmp_path)

        assert result["activated"] == "older"

    def test_unreadable_database_fails_open(self, tmp_path):
        """"Cannot tell" is not "too new". A database this cannot open is the
        engine's problem to report on start, not a reason to strand the user on
        the Topos they are trying to leave."""
        from topos import profiles

        (tmp_path / ".env").write_text("TOPOS_KEY=KEYAAA\n")
        (tmp_path / paths.DATABASE_FILENAME).write_bytes(b"current")
        dest = tmp_path / profiles.PROFILES_DIRNAME / "scrambled"
        dest.mkdir(parents=True)
        (dest / ".env").write_text("TOPOS_KEY=KEYBBB\n")
        (dest / paths.DATABASE_FILENAME).write_bytes(b"not-a-sqlite-file")

        result = profiles.switch_profile("scrambled", tmp_path)

        assert result["activated"] == "scrambled"
