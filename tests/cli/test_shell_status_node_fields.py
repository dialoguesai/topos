"""The node exposes what `status --json` (L3) and the plugin's SessionStart hook read:
`supervisor`, `owner_key_persisted`, `topos_id`, `user_id_present` on /v1/shell/status (CONTRACTS §4)."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from topos.api import shell
from topos.cli import service_cmd
from topos.core.state import set_engine_config_value, store_user_id


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TOPOS_ENV_FILE", str(tmp_path / ".topos" / ".env"))
    monkeypatch.setenv("TOPOS_SUPERVISOR", "")
    monkeypatch.delenv("TOPOS_SUPERVISOR")
    monkeypatch.setenv("TOPOS_OWNER_KEY", "")
    monkeypatch.delenv("TOPOS_OWNER_KEY")
    (tmp_path / ".topos").mkdir()
    monkeypatch.setattr(shell, "get_update_info", lambda: None)
    monkeypatch.setattr(service_cmd, "_parent_command", lambda pid: "/usr/bin/zsh")
    return tmp_path


@pytest.fixture
def conn(monkeypatch, tmp_path):
    c = sqlite3.connect(str(tmp_path / "node.db"), check_same_thread=False)
    c.row_factory = sqlite3.Row
    monkeypatch.setattr(shell, "_db_connection", lambda: c)
    yield c
    c.close()


def _status() -> dict:
    return asyncio.run(shell.shell_status())


def test_status_reports_owner_key_topos_id_user_id_and_supervisor(home: Path, conn, monkeypatch):
    (home / ".topos" / ".env").write_text("TOPOS_KEY=tpn_" + "a" * 40 + "\n")
    payload = _status()
    assert payload["owner_key_persisted"] is False
    assert payload["topos_id"] is None
    assert payload["user_id_present"] is False
    assert payload["supervisor"] == "terminal"

    (home / ".topos" / ".env").write_text("TOPOS_KEY=tpn_" + "a" * 40 + "\nTOPOS_OWNER_KEY=ok_" + "b" * 48 + "\n")
    set_engine_config_value(conn, "topos_id", "tp_0123")
    store_user_id(conn, "user-1")
    payload = _status()
    assert payload["owner_key_persisted"] is True
    assert payload["topos_id"] == "tp_0123"
    assert payload["user_id_present"] is True
    text = str(payload)
    assert "tpn_" + "a" * 40 not in text and "ok_" + "b" * 48 not in text and "user-1" not in text


def test_status_survives_a_missing_database(home: Path, monkeypatch):
    monkeypatch.setattr(shell, "_db_connection", lambda: None)
    payload = _status()
    assert payload["topos_id"] is None and payload["user_id_present"] is False
    assert payload["name"] == "topos-node"


@pytest.mark.parametrize(
    "env_value, parent, expected",
    [
        ("launchd", "/usr/bin/zsh", "launchd"),
        ("systemd", "/usr/bin/zsh", "systemd"),
        ("headless", "/usr/bin/zsh", "headless"),
        ("", "/Applications/Topos.app/Contents/MacOS/ToposShell", "topos-app"),
        ("", "/usr/bin/zsh", "terminal"),
        ("", None, "terminal"),
    ],
)
def test_current_supervisor(home: Path, monkeypatch, env_value, parent, expected):
    if env_value:
        monkeypatch.setenv("TOPOS_SUPERVISOR", env_value)
    monkeypatch.setattr(service_cmd, "_parent_command", lambda pid: parent)
    assert service_cmd.current_supervisor() == expected


def test_units_name_their_supervisor(home: Path, monkeypatch):
    monkeypatch.setattr(service_cmd, "_uv_bin_dir", lambda: None)
    import plistlib

    plist = plistlib.loads(service_cmd.render_launchd_plist(Path("/x/topos-node"), 8676))
    assert plist["EnvironmentVariables"]["TOPOS_SUPERVISOR"] == "launchd"
    unit = service_cmd.render_systemd_unit(Path("/x/topos-node"), 8676)
    assert "Environment=TOPOS_SUPERVISOR=systemd\n" in unit
