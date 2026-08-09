"""`uv` must be findable from a GUI-launched node.

Live incident 2026-08-08: a DMG-installed app launches the node with
PATH=/usr/bin:/bin:/usr/sbin:/sbin. `uv` lives in ~/.local/bin, so
`subprocess.run(["uv", ...])` raised FileNotFoundError, the worker's `finally`
stamped last_result="failed", nothing was logged, and the menu silently
reverted to "Update to vX". In-app update had never once worked for an app
install.
"""

from __future__ import annotations

import os
import stat

import pytest

from topos import runtime_update

GUI_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _make_executable(path) -> str:
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def test_explicit_binary_wins(monkeypatch, tmp_path):
    uv = _make_executable(tmp_path / "uv")
    monkeypatch.setenv("TOPOS_UV_BIN", uv)
    monkeypatch.setenv("PATH", GUI_PATH)
    assert runtime_update.resolve_uv_binary() == uv


def test_falls_back_to_the_home_install_under_a_gui_path(monkeypatch, tmp_path):
    monkeypatch.delenv("TOPOS_UV_BIN", raising=False)
    monkeypatch.setenv("PATH", GUI_PATH)
    home_uv = tmp_path / ".local" / "bin" / "uv"
    home_uv.parent.mkdir(parents=True)
    _make_executable(home_uv)
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
    assert runtime_update.resolve_uv_binary() == str(home_uv)


def test_returns_none_when_uv_is_genuinely_absent(monkeypatch, tmp_path):
    monkeypatch.delenv("TOPOS_UV_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))  # empty dir
    monkeypatch.setattr(os.path, "expanduser", lambda p: p.replace("~", str(tmp_path)))
    monkeypatch.setattr(runtime_update.os, "access", lambda p, m: False)
    assert runtime_update.resolve_uv_binary() is None


def test_missing_uv_reports_failure_instead_of_raising(monkeypatch, caplog):
    monkeypatch.setattr(runtime_update, "resolve_uv_binary", lambda: None)
    with caplog.at_level("ERROR"):
        assert runtime_update.apply_package_update("topos-node") is False
    assert "could not find" in caplog.text.lower()


def test_a_failing_upgrade_is_logged_not_swallowed(monkeypatch, caplog, tmp_path):
    uv = _make_executable(tmp_path / "uv")
    monkeypatch.setattr(runtime_update, "resolve_uv_binary", lambda: uv)

    class _Result:
        returncode = 2
        stdout = ""
        stderr = "error: No such tool `topos-node`"

    monkeypatch.setattr(runtime_update.subprocess, "run", lambda *a, **k: _Result())
    with caplog.at_level("ERROR"):
        assert runtime_update.apply_package_update("topos-node") is False
    assert "No such tool" in caplog.text


def test_an_oserror_is_caught_rather_than_killing_the_worker(monkeypatch, caplog, tmp_path):
    uv = _make_executable(tmp_path / "uv")
    monkeypatch.setattr(runtime_update, "resolve_uv_binary", lambda: uv)

    def _boom(*a, **k):
        raise OSError("Bad executable")

    monkeypatch.setattr(runtime_update.subprocess, "run", _boom)
    with caplog.at_level("ERROR"):
        assert runtime_update.apply_package_update("topos-node") is False
    assert "Bad executable" in caplog.text


def test_success_returns_true(monkeypatch, tmp_path):
    uv = _make_executable(tmp_path / "uv")
    monkeypatch.setattr(runtime_update, "resolve_uv_binary", lambda: uv)

    class _Result:
        returncode = 0
        stdout = "Installed 1 executable"
        stderr = ""

    monkeypatch.setattr(runtime_update.subprocess, "run", lambda *a, **k: _Result())
    assert runtime_update.apply_package_update("topos-node") is True
