"""The §4 binary resolver: the Topos.app order, `$TOPOS_NODE_BIN` first."""

from __future__ import annotations

from pathlib import Path

import pytest

from topos.cli import binary


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TOPOS_NODE_BIN", raising=False)
    monkeypatch.setattr(binary.shutil, "which", lambda _name: None)
    return tmp_path


def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_candidate_order_is_the_topos_app_order(home: Path, monkeypatch):
    monkeypatch.setenv("TOPOS_NODE_BIN", "/custom/topos-node")
    assert binary.candidate_paths() == [
        Path("/custom/topos-node"),
        home / ".local" / "bin" / "topos-node",
        home / ".topos" / "runtime" / "bin" / "topos-node",
        Path("/opt/homebrew/bin/topos-node"),
        Path("/usr/local/bin/topos-node"),
    ]


def test_env_override_is_dropped_when_unset(home: Path):
    assert binary.candidate_paths()[0] == home / ".local" / "bin" / "topos-node"


def test_resolve_picks_the_first_existing_executable(home: Path):
    runtime = _executable(home / ".topos" / "runtime" / "bin" / "topos-node")
    assert binary.resolve_node_binary() == runtime
    local = _executable(home / ".local" / "bin" / "topos-node")
    assert binary.resolve_node_binary() == local


def test_resolve_skips_a_non_executable_file(home: Path):
    stub = home / ".local" / "bin" / "topos-node"
    stub.parent.mkdir(parents=True)
    stub.write_text("not runnable")
    stub.chmod(0o644)
    assert binary.resolve_node_binary() is None


def test_resolve_falls_back_to_path_lookup_last(home: Path, monkeypatch):
    found = _executable(home / "elsewhere" / "topos-node")
    monkeypatch.setattr(binary.shutil, "which", lambda _name: str(found))
    assert binary.resolve_node_binary() == found
    local = _executable(home / ".local" / "bin" / "topos-node")
    assert binary.resolve_node_binary() == local
