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


class TestToposTray:
    def test_poll_host_rewrites_wildcard_bind(self):
        t = tray.ToposTray(
            host="0.0.0.0", port=9000, version="1.0.0", package_name="topos-node", on_quit=lambda: None
        )
        assert t.health_url == "http://127.0.0.1:9000/healthcheck"
        assert t.docs_url == "http://127.0.0.1:9000/docs"

    def test_specific_host_kept(self):
        t = tray.ToposTray(
            host="192.168.1.5", port=9100, version="1.0.0", package_name="topos-node", on_quit=lambda: None
        )
        assert t.health_url == "http://192.168.1.5:9100/healthcheck"
