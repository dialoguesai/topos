"""`topos-node service install|uninstall|status` — a unit with no secret, declined under Topos.app.

Units are written under a scratch HOME; launchctl/systemctl are recorded, never run
(CONTRACTS §4; spike P0.3: a launchd-run node loses iMessage, so the service says so).
"""

from __future__ import annotations

import json
import plistlib
import secrets
from pathlib import Path

import pytest
from click.testing import CliRunner

from topos.cli import service_cmd
from topos.cli.commands import main

IMESSAGE_NOTE = (
    "Messages (iMessage) sync needs the Topos app; this service runs Topos for everything else."
)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TOPOS_ENV_FILE", raising=False)
    monkeypatch.delenv("TOPOS_NODE_BIN", raising=False)
    key = "tpn_" + secrets.token_hex(20)
    env_path = tmp_path / ".topos" / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(f"TOPOS_KEY={key}\nTOPOS_OWNER_KEY=ok_{secrets.token_hex(24)}\n")
    binary = tmp_path / ".local" / "bin" / "topos-node"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    monkeypatch.setattr(service_cmd, "_process_table", lambda: [(1, 0, "/sbin/launchd"), (500, 1, "/usr/bin/zsh")])
    monkeypatch.setattr(service_cmd, "_probe_node", lambda port: None)
    monkeypatch.setattr(service_cmd, "_uv_bin_dir", lambda: "/fake/uv/bin")
    calls: list[list[str]] = []
    monkeypatch.setattr(service_cmd, "_run", lambda cmd: (calls.append(list(cmd)), (0, ""))[1])
    monkeypatch.setattr(service_cmd, "_calls_for_tests", calls, raising=False)
    return tmp_path


def _secret_values(home: Path) -> list[str]:
    text = (home / ".topos" / ".env").read_text()
    return [line.split("=", 1)[1].strip() for line in text.splitlines() if "=" in line]


def test_launchd_unit_runs_the_absolute_binary_and_holds_no_secret(home: Path, monkeypatch):
    monkeypatch.setattr(service_cmd, "_platform", lambda: "macos")
    result = CliRunner().invoke(main, ["service", "install"])
    assert result.exit_code == 0, result.output
    plist_path = home / "Library" / "LaunchAgents" / "com.dialogues.topos-node.plist"
    raw = plist_path.read_bytes()
    for value in _secret_values(home):
        assert value.encode() not in raw
    plist = plistlib.loads(raw)
    assert plist["Label"] == "com.dialogues.topos-node"
    binary = str(home / ".local" / "bin" / "topos-node")
    assert plist["ProgramArguments"] == [binary, "start", "--headless-foreground", "--port", "8676"]
    assert Path(plist["ProgramArguments"][0]).is_absolute()
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    env = plist["EnvironmentVariables"]
    assert env["HOME"] == str(home)
    assert env["PATH"].split(":")[:2] == [str(home / ".local" / "bin"), "/fake/uv/bin"]
    assert env["PATH"].endswith("/usr/bin:/bin:/usr/sbin:/sbin")
    assert "TOPOS_KEY" not in env and "TOPOS_OWNER_KEY" not in env
    assert plist["StandardOutPath"] == str(home / ".topos" / "logs" / "service.log")
    assert plist["StandardErrorPath"] == str(home / ".topos" / "logs" / "service.log")
    assert IMESSAGE_NOTE in result.output
    calls = service_cmd._calls_for_tests
    assert calls[-1][:2] == ["launchctl", "bootstrap"]
    assert calls[-1][-1] == str(plist_path)


def test_install_declines_when_topos_app_is_running(home: Path, monkeypatch):
    monkeypatch.setattr(service_cmd, "_platform", lambda: "macos")
    monkeypatch.setattr(
        service_cmd, "_process_table",
        lambda: [(1, 0, "/sbin/launchd"), (800, 1, "/Applications/Topos.app/Contents/MacOS/ToposShell")],
    )
    result = CliRunner().invoke(main, ["service", "install"])
    assert result.exit_code == 2
    assert result.stderr.strip() == (
        "Error: Topos.app manages Topos on this computer (Topos.app is running), so no service was "
        "installed. " + IMESSAGE_NOTE + " Quit Topos.app first if you want the service instead."
    )
    assert "Topos.app" in result.stderr  # QA keys on the word
    assert not (home / "Library" / "LaunchAgents").exists()
    assert service_cmd._calls_for_tests == []


def test_install_declines_when_the_running_node_is_a_child_of_topos_app(home: Path, monkeypatch):
    monkeypatch.setattr(service_cmd, "_platform", lambda: "macos")
    monkeypatch.setattr(
        service_cmd, "_process_table",
        lambda: [(1, 0, "/sbin/launchd"), (800, 1, "/Applications/Topos.app/Contents/MacOS/ToposShell"),
                 (801, 800, "/Users/x/.local/share/uv/tools/topos-node/bin/python")],
    )
    monkeypatch.setattr(service_cmd, "_probe_node", lambda port: {"name": "topos-node", "pid": 801})
    result = CliRunner().invoke(main, ["service", "install"])
    assert result.exit_code == 2
    assert "(Topos.app is running)" in result.stderr
    assert not (home / "Library" / "LaunchAgents").exists()


def test_install_declines_when_only_the_node_child_marks_the_app(home: Path, monkeypatch):
    """No ToposShell row in the table (e.g. ps truncated), but the node's parent is it."""
    monkeypatch.setattr(service_cmd, "_platform", lambda: "macos")
    monkeypatch.setattr(
        service_cmd, "_process_table",
        lambda: [(1, 0, "/sbin/launchd"), (801, 800, "python")],
    )
    monkeypatch.setattr(service_cmd, "_parent_command", lambda pid: "/Applications/Topos.app/Contents/MacOS/ToposShell" if pid == 801 else None)
    monkeypatch.setattr(service_cmd, "_probe_node", lambda port: {"name": "topos-node", "pid": 801})
    result = CliRunner().invoke(main, ["service", "install"])
    assert result.exit_code == 2
    assert "(the running node was started by Topos.app)" in result.stderr
    assert IMESSAGE_NOTE in result.stderr
    assert not (home / "Library" / "LaunchAgents").exists()
    assert service_cmd._calls_for_tests == []


def test_systemd_unit_runs_the_absolute_binary_and_holds_no_secret(home: Path, monkeypatch):
    monkeypatch.setattr(service_cmd, "_platform", lambda: "linux")
    result = CliRunner().invoke(main, ["service", "install", "--port", "8676"])
    assert result.exit_code == 0, result.output
    unit_path = home / ".config" / "systemd" / "user" / "topos-node.service"
    text = unit_path.read_text()
    for value in _secret_values(home):
        assert value not in text
    binary = str(home / ".local" / "bin" / "topos-node")
    assert f"ExecStart={binary} start --headless-foreground --port 8676\n" in text
    assert f"Environment=HOME={home}\n" in text
    assert "Restart=always\n" in text
    assert "WantedBy=default.target" in text
    calls = service_cmd._calls_for_tests
    assert ["systemctl", "--user", "daemon-reload"] in calls
    assert ["systemctl", "--user", "enable", "--now", "topos-node.service"] in calls


def test_install_without_a_binary_exits_2(home: Path, monkeypatch):
    monkeypatch.setattr(service_cmd, "_platform", lambda: "macos")
    (home / ".local" / "bin" / "topos-node").unlink()
    monkeypatch.setattr(service_cmd, "resolve_node_binary", lambda: None)
    result = CliRunner().invoke(main, ["service", "install"])
    assert result.exit_code == 2
    assert "topos-node isn't installed where the service can find it" in result.stderr
    assert service_cmd._calls_for_tests == []


def test_uninstall_removes_the_unit(home: Path, monkeypatch):
    monkeypatch.setattr(service_cmd, "_platform", lambda: "macos")
    runner = CliRunner()
    assert runner.invoke(main, ["service", "install"]).exit_code == 0
    plist_path = home / "Library" / "LaunchAgents" / "com.dialogues.topos-node.plist"
    assert plist_path.exists()
    result = runner.invoke(main, ["service", "uninstall"])
    assert result.exit_code == 0, result.output
    assert not plist_path.exists()
    assert service_cmd._calls_for_tests[-1][:2] == ["launchctl", "bootout"]
    again = runner.invoke(main, ["service", "uninstall"])
    assert again.exit_code == 0
    assert "not installed" in again.output


def test_status_json_reports_unit_and_node(home: Path, monkeypatch):
    monkeypatch.setattr(service_cmd, "_platform", lambda: "macos")
    runner = CliRunner()
    before = runner.invoke(main, ["service", "status", "--json"])
    assert before.exit_code == 2
    assert json.loads(before.stdout)["installed"] is False
    assert runner.invoke(main, ["service", "install"]).exit_code == 0
    monkeypatch.setattr(service_cmd, "_probe_node", lambda port: {"name": "topos-node", "pid": 5, "version": "1.6.0"})
    after = runner.invoke(main, ["service", "status", "--json"])
    assert after.exit_code == 0, after.output
    payload = json.loads(after.stdout)
    assert payload["installed"] is True
    assert payload["node_running"] is True
    assert payload["unit"] == str(home / "Library" / "LaunchAgents" / "com.dialogues.topos-node.plist")
    assert payload["exit_code"] == 0
    for value in _secret_values(home):
        assert value not in after.stdout


def test_unsupported_platform_exits_2(home: Path, monkeypatch):
    monkeypatch.setattr(service_cmd, "_platform", lambda: "windows")
    result = CliRunner().invoke(main, ["service", "install"])
    assert result.exit_code == 2
    assert "isn't available on this platform" in result.stderr
