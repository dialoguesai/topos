"""The machine name a Mac owner recognises, reported alongside the hostname.

`platform.node()` returns the network hostname, which on macOS is either a
mangled form of the computer name ("jonnys-macbook-pro.local") or something
meaningless from DHCP ("mac.lan" on the machine this was written on). The
frontend names a new self-hosted Topos after this, so a bad answer here shows up
as a Topos called "mac.lan".
"""

from __future__ import annotations

import subprocess

import pytest

from topos.core import state


@pytest.fixture(autouse=True)
def _clear_cache():
    state._computer_name_cache = state._UNRESOLVED
    yield
    state._computer_name_cache = state._UNRESOLVED


def _fake_run(stdout: str):
    def run(_cmd, **_kwargs):
        return subprocess.CompletedProcess(args=_cmd, returncode=0, stdout=stdout, stderr="")

    return run


def test_reports_the_computer_name_on_macos(monkeypatch):
    monkeypatch.setattr(state.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(state.subprocess, "run", _fake_run("Jonny's MacBook Pro\n"))

    assert state._macos_computer_name() == "Jonny's MacBook Pro"


def test_absent_off_macos(monkeypatch):
    monkeypatch.setattr(state.platform, "system", lambda: "Linux")

    def explode(*_a, **_k):  # pragma: no cover - must never be reached
        raise AssertionError("scutil is macOS-only and must not be spawned elsewhere")

    monkeypatch.setattr(state.subprocess, "run", explode)
    assert state._macos_computer_name() is None


@pytest.mark.parametrize("stdout", ["", "   \n"])
def test_blank_output_is_no_name(monkeypatch, stdout):
    monkeypatch.setattr(state.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(state.subprocess, "run", _fake_run(stdout))

    assert state._macos_computer_name() is None


def test_failure_is_swallowed_and_never_retried(monkeypatch):
    """A nicer label is never worth a startup failure — nor a stall on every request.

    get_system_info runs inside async handlers, so an uncached spawn blocks the
    event loop for the full timeout. On a Mac with no ComputerName that would be
    a permanent per-request stall, so a miss is cached exactly like a hit and the
    node falls back to the hostname for the rest of its life.
    """
    monkeypatch.setattr(state.platform, "system", lambda: "Darwin")
    calls = {"n": 0}

    def always_times_out(_cmd, **_kwargs):
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd=_cmd, timeout=2.0)

    monkeypatch.setattr(state.subprocess, "run", always_times_out)

    assert state._macos_computer_name() is None
    assert state._macos_computer_name() is None
    assert state._macos_computer_name() is None
    assert calls["n"] == 1


def test_non_macos_never_spawns_twice(monkeypatch):
    monkeypatch.setattr(state.platform, "system", lambda: "Linux")
    assert state._macos_computer_name() is None
    # Cached, so a Linux node does not re-check platform on every request either.
    assert state._computer_name_cache is None


def test_spawns_once_per_process(monkeypatch):
    """get_device_info calls get_system_info twice, and the CP hits it on every connect."""
    monkeypatch.setattr(state.platform, "system", lambda: "Darwin")
    calls = {"n": 0}

    def counting(_cmd, **_kwargs):
        calls["n"] += 1
        return subprocess.CompletedProcess(args=_cmd, returncode=0, stdout="q4\n", stderr="")

    monkeypatch.setattr(state.subprocess, "run", counting)

    for _ in range(5):
        state._macos_computer_name()

    assert calls["n"] == 1


def test_system_info_carries_computer_name_beside_hostname(monkeypatch):
    """The dict is free-form and the control plane forwards it verbatim, so this
    key is the entire wire contract — it lands as system_info.computer_name."""
    monkeypatch.setattr(state, "_macos_computer_name", lambda: "Jonny's MacBook Pro")

    info = state.get_system_info()

    assert info["computer_name"] == "Jonny's MacBook Pro"
    assert "hostname" in info
