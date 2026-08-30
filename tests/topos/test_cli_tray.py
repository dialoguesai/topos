"""Tray icon module: enable/auto-detect logic and status image rendering."""

from __future__ import annotations

import sys

import pytest

pytestmark = pytest.mark.public

from topos.cli import tray


class TestShouldEnableTray:
    def test_explicit_off_wins(self, monkeypatch):
        monkeypatch.setattr(tray, "tray_available", lambda: True)
        assert tray.should_enable_tray(cli_flag=False) is False

    def test_env_off_wins(self, monkeypatch):
        monkeypatch.setenv("TOPOS_TRAY", "0")
        monkeypatch.setattr(tray, "tray_available", lambda: True)
        assert tray.should_enable_tray(cli_flag=None) is False

    def test_explicit_on_requires_deps(self, monkeypatch):
        monkeypatch.delenv("TOPOS_TRAY", raising=False)
        monkeypatch.setattr(tray, "tray_available", lambda: False)
        assert tray.should_enable_tray(cli_flag=True) is False
        monkeypatch.setattr(tray, "tray_available", lambda: True)
        assert tray.should_enable_tray(cli_flag=True) is True

    def test_auto_headless_linux_disabled(self, monkeypatch):
        monkeypatch.delenv("TOPOS_TRAY", raising=False)
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr(tray.sys, "platform", "linux")
        monkeypatch.setattr(tray, "tray_available", lambda: True)
        assert tray.should_enable_tray(cli_flag=None) is False

    def test_auto_darwin_enabled_when_deps_import(self, monkeypatch):
        monkeypatch.delenv("TOPOS_TRAY", raising=False)
        monkeypatch.setattr(tray.sys, "platform", "darwin")
        monkeypatch.setattr(tray, "tray_available", lambda: True)
        assert tray.should_enable_tray(cli_flag=None) is True

    def test_env_on_enables_without_gui_detection(self, monkeypatch):
        monkeypatch.setenv("TOPOS_TRAY", "1")
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        monkeypatch.setattr(tray.sys, "platform", "linux")
        monkeypatch.setattr(tray, "tray_available", lambda: True)
        assert tray.should_enable_tray(cli_flag=None) is True


class TestStatusImage:
    @pytest.fixture(autouse=True)
    def _needs_pillow(self):
        pytest.importorskip("PIL")

    @pytest.mark.parametrize("glyph", ["topos_white.png", "topos_blk_rounded.png"])
    def test_glyph_assets_ship_and_render(self, glyph):
        image = tray.create_status_image("healthy", glyph=glyph)
        assert image.size == (tray.ICON_SIZE, tray.ICON_SIZE)
        assert image.mode == "RGBA"

    @pytest.mark.parametrize("status", list(tray.STATUS_COLORS))
    def test_all_statuses_render_their_dot(self, status):
        image = tray.create_status_image(status, glyph="topos_white.png")
        r, g, b, a = image.getpixel((27, 27))  # center of the status dot
        assert (r, g, b, a) == tray.STATUS_COLORS[status]

    def test_unknown_status_falls_back_to_starting(self):
        image = tray.create_status_image("nonsense", glyph="topos_white.png")
        assert image.getpixel((27, 27)) == tray.STATUS_COLORS["starting"]

    def test_update_status_uses_download_badge_not_orange_dot(self):
        image = tray.create_status_image("update", glyph="topos_white.png")
        # Badge circle is white on the white glyph (contrast for dark menu bars).
        assert image.getpixel((22, 22)) == (255, 255, 255, 255)
        # Arrow shaft near center is black ink on that circle.
        r, g, b, a = image.getpixel((26, 24))
        assert a == 255 and r < 40 and g < 40 and b < 40


class TestTrayHealthHysteresis:
    """A busy node that answers /healthcheck late must not paint the tray red."""

    def test_single_miss_keeps_green(self):
        status, failures = tray.resolve_tray_health_status(
            probe_ok=False, consecutive_failures=0, current_status="healthy"
        )
        assert status == "healthy"
        assert failures == 1

    def test_second_consecutive_miss_goes_red(self):
        status, failures = tray.resolve_tray_health_status(
            probe_ok=False, consecutive_failures=1, current_status="healthy"
        )
        assert status == "down"
        assert failures == 2

    def test_success_clears_the_count_and_goes_green(self):
        status, failures = tray.resolve_tray_health_status(
            probe_ok=True, consecutive_failures=1, current_status="down"
        )
        assert status == "healthy"
        assert failures == 0

    def test_starting_does_not_flip_red_on_the_first_miss(self):
        status, failures = tray.resolve_tray_health_status(
            probe_ok=False, consecutive_failures=0, current_status="starting"
        )
        assert status == "starting"
        assert failures == 1

    def test_client_timeout_sits_above_the_database_probe(self):
        assert tray.HEALTH_TIMEOUT_SECONDS > 2.0
        assert tray.HEALTH_FAILURE_THRESHOLD == 2

    def test_poller_uses_hysteresis_and_the_raised_timeout(self):
        import inspect

        body = inspect.getsource(tray.ToposTray._poll_health)
        assert "resolve_tray_health_status(" in body
        assert "HEALTH_TIMEOUT_SECONDS" in body
        assert "timeout=3.0" not in body


class TestToposTray:
    def test_poll_host_rewrites_wildcard_bind(self):
        t = tray.ToposTray(
            host="0.0.0.0", port=9000, version="1.0.0", package_name="topos-node", on_quit=lambda: None
        )
        assert t.health_url == "http://127.0.0.1:9000/healthcheck"
        assert t.docs_url == tray.TOPOS_DOCS_URL  # product docs, not the local API docs

    def test_specific_host_kept(self):
        t = tray.ToposTray(
            host="192.168.1.5", port=9100, version="1.0.0", package_name="topos-node", on_quit=lambda: None
        )
        assert t.health_url == "http://192.168.1.5:9100/healthcheck"


class TestQuitSemantics:
    """Quit means quit, attached or not — the design of record shared with the
    macOS shell (0.2.11). "Close Tray (node keeps running)" as the only exit
    stranded users whose tray attached after a crash or update restart."""

    def _tray(self, *, attached: bool) -> tray.ToposTray:
        return tray.ToposTray(
            host="127.0.0.1",
            port=9000,
            version="1.0.0",
            package_name="topos-node",
            on_quit=lambda: None,
            attached=attached,
        )

    def test_quit_is_always_the_primary_exit(self):
        for attached in (False, True):
            labels = self._tray(attached=attached)._menu_labels()
            assert "Quit Topos Node" in labels
            assert "Close Tray (node keeps running)" not in labels

    def test_tray_only_exit_appears_only_when_attached(self):
        assert "Close Tray Only (node keeps running)" in self._tray(attached=True)._menu_labels()
        assert (
            "Close Tray Only (node keeps running)"
            not in self._tray(attached=False)._menu_labels()
        )

    def test_quit_comes_before_the_tray_only_exit(self):
        labels = self._tray(attached=True)._menu_labels()
        assert labels.index("Quit Topos Node") < labels.index("Close Tray Only (node keeps running)")

    def test_attached_quit_stops_the_node_by_pid(self, monkeypatch):
        t = self._tray(attached=True)
        t.node_pid = 4242
        stopped = {}
        monkeypatch.setattr(tray.ToposTray, "_stop_node_by_pid", staticmethod(lambda pid: stopped.setdefault("pid", pid)))
        t._quit()
        assert stopped["pid"] == 4242

    def test_close_tray_only_never_touches_the_node(self, monkeypatch):
        t = self._tray(attached=True)
        t.node_pid = 4242
        monkeypatch.setattr(
            tray.ToposTray,
            "_stop_node_by_pid",
            staticmethod(lambda pid: (_ for _ in ()).throw(AssertionError("node was stopped"))),
        )
        t._close_tray_only()


class TestToposNameRow:
    def test_named_topos_gets_its_row(self):
        t = tray.ToposTray(
            host="127.0.0.1", port=9000, version="1.0.0", package_name="topos-node", on_quit=lambda: None
        )
        assert not any(l.startswith("Topos: ") for l in t._menu_labels())
        t.topos_name = "PersonalDB"
        assert "Topos: PersonalDB" in t._menu_labels()


class TestStartingSpinner:
    def test_phase_renders_distinct_frames(self):
        a = tray.create_status_image("starting", glyph="topos_white.png", phase=0.0)
        b = tray.create_status_image("starting", glyph="topos_white.png", phase=0.5)
        assert list(a.getdata()) != list(b.getdata()), "spinner frames must differ or nothing animates"

    def test_no_phase_keeps_the_static_dot(self):
        static1 = tray.create_status_image("starting", glyph="topos_white.png")
        static2 = tray.create_status_image("starting", glyph="topos_white.png")
        assert list(static1.getdata()) == list(static2.getdata())


class TestFailedUpdateIsVisible:
    """A failed update rendered nothing, so the menu fell back to "Update to
    vX" — indistinguishable from a click that never registered, which is
    exactly how the macOS bug was reported (2026-08-08)."""

    def _tray(self) -> tray.ToposTray:
        return tray.ToposTray(
            host="127.0.0.1", port=9000, version="1.3.6", package_name="topos-node", on_quit=lambda: None
        )

    def test_failure_is_named(self):
        t = self._tray()
        t.update = {"available": True, "latest": "1.3.7", "applying": False, "last_result": "failed"}
        labels = t._menu_labels()
        assert "Update failed — click to retry" in labels
        assert not any(l.startswith("Update to v") for l in labels)

    def test_success_and_applying_are_unchanged(self):
        t = self._tray()
        t.update = {"available": True, "latest": "1.3.7", "applying": True, "last_result": None}
        assert "Installing update…" in t._menu_labels()
        t.update = {"available": True, "latest": "1.3.7", "applying": False, "last_result": "success"}
        assert "Update installed — restart to finish" in t._menu_labels()


class TestSelectToposMenu:
    """Multi-Topos parity with the macOS shell (PLAN_MULTI_TOPOS_SWITCHING M5).

    The submenu itself needs a display; these pin the parts that do not —
    which profiles are offered, which are switchable, and the rule that a tray
    not owning the node must not offer to swap the database under it.
    """

    def _tray(self, *, attached: bool = False) -> tray.ToposTray:
        return tray.ToposTray(
            host="127.0.0.1",
            port=9000,
            version="1.3.15",
            package_name="topos-node",
            on_quit=lambda: None,
            attached=attached,
        )

    def _profiles(self, monkeypatch, entries):
        monkeypatch.setattr(tray.ToposTray, "_profiles", lambda self: entries)

    def test_profiles_are_listed_with_names_and_sizes(self, monkeypatch):
        from topos.profiles import ProfileInfo

        self._profiles(
            monkeypatch,
            [
                ProfileInfo("default", "PersonalDB", "/p/a", 2_800_000_000, active=True),
                ProfileInfo("work", "Work", "/p/b", 830_000_000),
            ],
        )
        labels = self._tray()._menu_labels()
        assert "Select Topos" in labels
        assert "PersonalDB (2.8 GB)" in labels
        assert "Work (0.8 GB)" in labels
        assert "New Topos…" in labels

    def test_unnamed_profile_falls_back_to_its_id(self, monkeypatch):
        from topos.profiles import ProfileInfo

        self._profiles(monkeypatch, [ProfileInfo("topos-20260812", None, "/p/a", 5_000_000)])
        assert "topos-20260812 (5 MB)" in self._tray()._menu_labels()

    def test_attached_tray_never_offers_to_switch(self, monkeypatch):
        from topos.profiles import ProfileInfo

        self._profiles(monkeypatch, [ProfileInfo("work", "Work", "/p/b", 1)])
        labels = self._tray(attached=True)._menu_labels()
        # It cannot restart a node it did not start; stopping one and moving
        # its database would leave the machine with nothing running.
        assert "Select Topos" not in labels
        assert "New Topos…" not in labels

    def test_profile_listing_failure_does_not_break_the_menu(self, monkeypatch):
        monkeypatch.setattr(
            tray.ToposTray,
            "_profiles",
            lambda self: (_ for _ in ()).throw(RuntimeError("disk gone")),
        )
        # _profiles swallows its own errors; assert the real one does too.
        t = self._tray()
        monkeypatch.undo()
        monkeypatch.setattr(
            "topos.profiles.list_profiles",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk gone")),
        )
        assert t._profiles() == []
        assert "Quit Topos Node" in t._menu_labels()

    def test_click_queues_the_action_rather_than_doing_it(self, monkeypatch):
        t = self._tray()
        closed = {}
        monkeypatch.setattr(tray.ToposTray, "_close_tray_only", lambda self: closed.setdefault("x", True))
        monkeypatch.setattr(tray.ToposTray, "_notify", lambda self, m: None)
        t._switch_profile("work")
        # The node to stop IS this process, so the click may only queue.
        assert t.pending_profile_action == ("switch", "work")
        assert closed["x"] is True

    def test_new_topos_queues_its_own_action(self, monkeypatch):
        t = self._tray()
        monkeypatch.setattr(tray.ToposTray, "_close_tray_only", lambda self: None)
        monkeypatch.setattr(tray.ToposTray, "_notify", lambda self, m: None)
        t._new_topos()
        assert t.pending_profile_action == ("new", "")


class TestApplyPendingProfileAction:
    def test_switch_asks_for_a_restart(self, monkeypatch):
        monkeypatch.setattr(
            "topos.profiles.switch_profile",
            lambda pid: {"activated": pid, "archived_as": "old"},
        )
        assert tray.apply_pending_profile_action(("switch", "work")) is True

    def test_new_does_not_restart_into_an_unbound_node(self, monkeypatch):
        monkeypatch.setattr(
            "topos.profiles.new_profile", lambda: {"archived": True, "archived_as": "personal"}
        )
        # A node with no key exits in a second; relaunching would bury the
        # pairing instructions under an error.
        assert tray.apply_pending_profile_action(("new", "")) is False

    def test_a_refused_switch_never_restarts(self, monkeypatch):
        from topos.profiles import ProfileError

        monkeypatch.setattr(
            "topos.profiles.switch_profile",
            lambda pid: (_ for _ in ()).throw(ProfileError("node is running")),
        )
        assert tray.apply_pending_profile_action(("switch", "work")) is False
