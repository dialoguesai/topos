"""What the plugin's setup skill (L11) runs: `pair --help`, `service status`, `--version`."""

from __future__ import annotations

import secrets
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from topos.cli import commands, pair_cmd, service_cmd
from topos.cli.commands import main


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TOPOS_ENV_FILE", "")
    monkeypatch.delenv("TOPOS_ENV_FILE")
    (tmp_path / ".topos").mkdir()
    (tmp_path / ".topos" / ".env").write_text("TOPOS_KEY=tpn_" + secrets.token_hex(20) + "\n")
    monkeypatch.setattr(service_cmd, "_run", lambda cmd: (0, ""))
    monkeypatch.setattr(service_cmd, "_platform", lambda: "macos")
    return tmp_path


def test_pair_help_exits_0_without_touching_the_control_plane(home: Path, monkeypatch):
    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("control plane unreachable", request=request)

    monkeypatch.setattr(pair_cmd, "_transport", httpx.MockTransport(_boom))
    result = CliRunner().invoke(main, ["pair", "--help"])
    assert result.exit_code == 0, result.output
    assert "--add-agent" in result.stdout and "--manual" in result.stdout
    assert not (home / ".topos" / "pairing.json").exists()


def test_service_status_under_topos_app_says_so_and_exits_0_when_the_node_runs(home: Path, monkeypatch):
    monkeypatch.setattr(
        service_cmd, "_process_table",
        lambda: [(1, 0, "/sbin/launchd"), (800, 1, "/Applications/Topos.app/Contents/MacOS/ToposShell")],
    )
    monkeypatch.setattr(service_cmd, "_probe_node", lambda port: {"name": "topos-node", "pid": 801, "version": "1.6.0"})
    result = CliRunner().invoke(main, ["service", "status"])
    assert result.exit_code == 0, result.output
    line = next(l for l in result.stdout.splitlines() if "Topos.app" in l)
    assert "managed" in line
    assert "Service not installed" in result.stdout


def test_service_status_exits_2_when_nothing_runs(home: Path, monkeypatch):
    monkeypatch.setattr(service_cmd, "_process_table", lambda: [(1, 0, "/sbin/launchd")])
    monkeypatch.setattr(service_cmd, "_probe_node", lambda port: None)
    result = CliRunner().invoke(main, ["service", "status"])
    assert result.exit_code == 2
    assert "Topos.app" not in result.stdout


def test_version_prints_one_line(home: Path, monkeypatch):
    monkeypatch.setattr(commands, "_get_runtime_version", lambda package_name=None: "9.8.7")
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.stdout == "topos-node 9.8.7\n"
