from __future__ import annotations

import os

from topos.cli import commands
from topos.defaults import DEFAULT_NODE_PORT


def test_get_runtime_version_prefers_module_version(monkeypatch):
    monkeypatch.setattr(commands, "_get_module_version", lambda: "0.1.3")
    monkeypatch.setattr(commands, "_get_installed_package_version", lambda *_args, **_kwargs: "9.9.9")

    assert commands._get_runtime_version() == "0.1.3"


def test_get_runtime_version_falls_back_to_package_version(monkeypatch):
    monkeypatch.setattr(commands, "_get_module_version", lambda: None)
    monkeypatch.setattr(commands, "_get_installed_package_version", lambda *_args, **_kwargs: "1.2.3")

    assert commands._get_runtime_version() == "1.2.3"


def test_get_runtime_version_returns_unknown_without_any_source(monkeypatch):
    monkeypatch.setattr(commands, "_get_module_version", lambda: None)
    monkeypatch.setattr(commands, "_get_installed_package_version", lambda *_args, **_kwargs: None)

    assert commands._get_runtime_version() == "unknown"


def test_emit_startup_banner_includes_software_and_version(monkeypatch):
    monkeypatch.delenv("TOPOS_STARTUP_BANNER_EMITTED", raising=False)
    monkeypatch.setattr(commands, "_get_runtime_version", lambda *_args, **_kwargs: "2.4.6")

    messages: list[str] = []
    monkeypatch.setattr(commands.click, "echo", lambda msg: messages.append(msg))

    commands._emit_startup_banner(host="127.0.0.1", port=DEFAULT_NODE_PORT)

    assert messages
    assert any("TTTTTTTT    OOOOOOO    PPPPPPPP" in line for line in messages)
    assert any("Topos Node (topos-node)" in line for line in messages)
    assert any("Version : v2.4.6" in line for line in messages)
    assert any("Mode    : cli" in line for line in messages)
    assert any(f"Bind    : 127.0.0.1:{DEFAULT_NODE_PORT}" in line for line in messages)
    assert os.environ["TOPOS_STARTUP_BANNER_EMITTED"] == "1"
