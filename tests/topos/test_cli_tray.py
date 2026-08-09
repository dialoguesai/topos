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
