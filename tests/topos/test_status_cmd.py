"""Guards for `topos-node status --json` and `topos-node doctor` (CONTRACTS.md §4).

protects: the JSON schema is exactly the contract's; exit codes 0/3/4/5 each
come from a fixture state; doctor names a fix per finding and exits 2 when
there is one; the node's supervisor is read off the process tree; and no
secret or HTTP body reaches stdout, stderr or the log — the fake node plants a
secret in every body it serves.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.public

from tests.topos.fake_node_socket import FakeNode, serve_fake_node  # noqa: E402
from topos import mcp_clients  # noqa: E402
from topos.cli import status_cmd  # noqa: E402

ENV_KEY = "-".join(("injected", "topos", "key", "q7" * 8))
ENV_OWNER_KEY = "ok_" + "injected" + "0123456789abcdef" * 3
LABEL = "tpa_claude-code." + "ef" * 20
PLANTED = "tpn_" + "planted" + "0123456789abcdef" * 2
SECRETS = (ENV_KEY, ENV_OWNER_KEY, LABEL, PLANTED)

STATUS_KEYS = {"node", "paired", "topos_id", "user_id_present", "control_plane", "outdated",
               "latest_version", "agents", "sources", "pairing", "exit_code"}
NODE_KEYS = {"running", "version", "pid", "socket", "owner_key_persisted", "supervisor"}


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    topos_dir = tmp_path / ".topos"
    topos_dir.mkdir(mode=0o700)
    env = topos_dir / ".env"
    env.write_text(f"TOPOS_KEY={ENV_KEY}\nTOPOS_OWNER_KEY={ENV_OWNER_KEY}\n", encoding="utf-8")
    monkeypatch.setenv("TOPOS_ENV_FILE", str(env))
    monkeypatch.setenv("TOPOS_KEY", ENV_KEY)
    monkeypatch.setenv("TOPOS_OWNER_KEY", ENV_OWNER_KEY)
    db = topos_dir / "database.db"
    sqlite3.connect(db).close()
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(db))
    try:
        from topos.config.settings import settings as runtime_settings

        monkeypatch.setattr(runtime_settings, "topos_database_path", str(db), raising=False)
    except Exception:  # noqa: BLE001
        pass
    agents = topos_dir / "agents"
    agents.mkdir(mode=0o700)
    (agents / "claude-code.token").write_text(LABEL + "\n", encoding="utf-8")
    (agents / "claude-code.token").chmod(0o600)
    monkeypatch.setattr(
        mcp_clients, "list_agents",
        lambda conn: [{"client_id": "agent-claude-code", "display_name": "Claude Code",
                       "principal_class": "owner_agent", "created_at": "2026-10-15T21:00:00+00:00",
                       "last_used_at": None, "revoked_at": None}],
        raising=False,
    )
    # No process-tree walk in tests: a fixed answer the mapping guard checks separately.
    monkeypatch.setattr(status_cmd, "_supervisor_for_pid", lambda pid: "manual" if pid else None)
    # Whether this machine has a topos-node binary is not what these guards measure.
    monkeypatch.setattr(status_cmd, "_resolve_binary", lambda: "/fake/bin/topos-node")
    return tmp_path


def _socket_path() -> Path:
    return Path(os.environ["TOPOS_UDS_PATH"])


def _run(command, args):
    return CliRunner().invoke(command, args, catch_exceptions=False)


def _assert_no_secret(result, caplog):
    blob = result.stdout + result.stderr + caplog.text
    for secret in SECRETS:
        assert secret not in blob, secret[:12]


def _fake_node() -> FakeNode:
    node = FakeNode()
    node.planted_secret = PLANTED
    return node


# ----------------------------------------------------------------- status 0
def test_status_json_schema_and_exit_0_when_connected_and_paired(home, caplog):
    with serve_fake_node(_fake_node(), _socket_path()):
        result = _run(status_cmd.status_command, ["--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert set(payload) == STATUS_KEYS
    assert set(payload["node"]) == NODE_KEYS
    assert payload["node"]["running"] is True
    assert payload["node"]["version"] == "9.9.9"
    assert payload["node"]["pid"] == os.getpid()
    assert payload["node"]["socket"] is True
    assert payload["node"]["owner_key_persisted"] is True
    assert payload["node"]["supervisor"] == "manual"
    assert payload["paired"] is True
    assert payload["topos_id"] == "topos-1"
    assert payload["user_id_present"] is True
    assert payload["control_plane"] == "connected"
    assert payload["outdated"] is False and payload["latest_version"] is None
    assert payload["agents"] == [{"client": "claude-code", "enrolled_at": "2026-10-15T21:00:00+00:00", "revoked": False}]
    assert payload["sources"] == [{
        "source_id": "claude_code_sessions", "enabled": True, "paused": False,
        "last_sync_at": "2026-10-15T20:00:00+00:00", "last_error": None,
    }]
    assert payload["pairing"] is None
    assert payload["exit_code"] == 0
    _assert_no_secret(result, caplog)


def test_status_human_output_has_no_secret(home, caplog):
    with serve_fake_node(_fake_node(), _socket_path()):
        result = _run(status_cmd.status_command, [])
    assert result.exit_code == 0
    assert "running" in result.stdout.lower()
    _assert_no_secret(result, caplog)


# ------------------------------------------------------------------ exit 4
def test_exit_4_when_the_node_is_down(home, caplog):
    path = _socket_path()
    if path.exists():
        path.unlink()
    result = _run(status_cmd.status_command, ["--json"])
    assert result.exit_code == 4
    payload = json.loads(result.stdout)
    assert payload["node"]["running"] is False
    assert payload["node"]["pid"] is None
    assert payload["node"]["socket"] is False
    assert payload["node"]["supervisor"] is None
    assert payload["control_plane"] == "unknown"
    assert payload["exit_code"] == 4
    assert payload["agents"][0]["client"] == "claude-code", "the registry is readable without the node"
    _assert_no_secret(result, caplog)


# ------------------------------------------------------------------ exit 3
def test_exit_3_when_not_paired(home):
    node = _fake_node()
    node.paired = False
    node.control_plane = "offline"
    with serve_fake_node(node, _socket_path()):
        result = _run(status_cmd.status_command, ["--json"])
    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["paired"] is False and payload["topos_id"] is None
    assert payload["exit_code"] == 3


# ------------------------------------------------------------------ exit 5
def test_exit_5_when_the_key_is_rejected(home):
    node = _fake_node()
    node.control_plane = "rejected"
    with serve_fake_node(node, _socket_path()):
        result = _run(status_cmd.status_command, ["--json"])
    assert result.exit_code == 5
    assert json.loads(result.stdout)["control_plane"] == "rejected"


def test_outdated_and_pairing_block(home):
    node = _fake_node()
    node.update = {"available": True, "installed": "9.9.9", "latest": "9.9.10"}
    (home / ".topos" / "pairing.json").write_text(json.dumps({"state": "pending", "user_code": "BCDF-GHJK"}))
    with serve_fake_node(node, _socket_path()):
        payload = json.loads(_run(status_cmd.status_command, ["--json"]).stdout)
    assert payload["outdated"] is True and payload["latest_version"] == "9.9.10"
    assert payload["pairing"] == {"state": "pending", "user_code": "BCDF-GHJK"}


# ------------------------------------------------------------------ doctor
def test_doctor_clean_exits_0(home, caplog):
    with serve_fake_node(_fake_node(), _socket_path()):
        result = _run(status_cmd.doctor_command, ["--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["findings"] == []
    _assert_no_secret(result, caplog)


def test_doctor_names_a_fix_per_finding(home, monkeypatch, caplog):
    path = _socket_path()
    if path.exists():
        path.unlink()
    monkeypatch.setenv("TOPOS_KEY", "a-different-exported-key-0000000000000000")
    (home / ".topos" / "agents" / "claude-code.token").unlink()
    result = _run(status_cmd.doctor_command, ["--json"])
    assert result.exit_code == 2
    findings = {f["code"]: f for f in json.loads(result.stdout)["findings"]}
    assert "topos-node start" in findings["node_down"]["fix"]
    assert "topos-node agents add claude-code" in findings["label_missing"]["fix"]
    assert "TOPOS_KEY" in findings["exported_key_differs"]["fix"]
    assert "socket_missing" in findings
    for finding in findings.values():
        assert finding["message"] and finding["fix"]
    _assert_no_secret(result, caplog)
    assert "a-different-exported-key" not in result.stdout + result.stderr + caplog.text

    human = _run(status_cmd.doctor_command, [])
    assert human.exit_code == 2
    assert "topos-node start" in human.stdout
    _assert_no_secret(human, caplog)


def test_doctor_pairing_and_upgrade_fixes(home):
    node = _fake_node()
    node.paired = False
    node.update = {"available": True, "installed": "9.9.9", "latest": "9.9.10"}
    node.owner_key_persisted = False
    with serve_fake_node(node, _socket_path()):
        result = _run(status_cmd.doctor_command, ["--json"])
    assert result.exit_code == 2
    findings = {f["code"]: f for f in json.loads(result.stdout)["findings"]}
    assert "topos-node pair" in findings["not_paired"]["fix"]
    assert "topos-node upgrade" in findings["outdated"]["fix"]
    assert "owner_key_not_persisted" in findings


def test_doctor_rejected_key_points_at_pair(home):
    node = _fake_node()
    node.control_plane = "rejected"
    with serve_fake_node(node, _socket_path()):
        result = _run(status_cmd.doctor_command, ["--json"])
    findings = {f["code"]: f for f in json.loads(result.stdout)["findings"]}
    assert "topos-node pair" in findings["key_rejected"]["fix"]


# ------------------------------------------------------------- supervisor
def test_supervisor_is_read_off_the_process_tree(monkeypatch):
    tree = {
        100: (50, "python3.10"),
        50: (10, "ToposShell"),
        10: (1, "launchd"),
        200: (1, "python3.10"),
        300: (301, "python3.10"),
        301: (1, "zsh"),
    }
    monkeypatch.setattr(status_cmd, "_process_parent", lambda pid: tree.get(pid))
    assert status_cmd._supervisor_for_pid(100) == "topos_app"
    assert status_cmd._supervisor_for_pid(200) == "service"
    assert status_cmd._supervisor_for_pid(300) == "manual"
    assert status_cmd._supervisor_for_pid(None) is None


def test_binary_resolution_order():
    assert status_cmd.BINARY_CANDIDATES == (
        "$TOPOS_NODE_BIN", "~/.local/bin/topos-node", "~/.topos/runtime/bin/topos-node",
        "/opt/homebrew/bin/topos-node", "/usr/local/bin/topos-node",
    )


@pytest.mark.parametrize(
    "word, expected",
    [("topos-app", "topos_app"), ("launchd", "service"), ("systemd", "service"), ("headless", "manual"), ("terminal", "manual")],
)
def test_supervisor_prefers_the_nodes_own_word(home, monkeypatch, word, expected):
    """Lane L7's /v1/shell/status says who supervises the node; the tree walk is the fallback."""
    monkeypatch.setattr(status_cmd, "_supervisor_for_pid", lambda pid: "manual")
    node = _fake_node()
    node.extra_status = {"supervisor": word, "topos_id": "topos-1"}
    with serve_fake_node(node, _socket_path()):
        payload = json.loads(_run(status_cmd.status_command, ["--json"]).stdout)
    assert payload["node"]["supervisor"] == expected


def test_topos_id_falls_back_to_the_shell_status(home):
    node = _fake_node()
    node.scope_door = False
    node.extra_status = {"topos_id": "topos-from-shell"}
    with serve_fake_node(node, _socket_path()):
        payload = json.loads(_run(status_cmd.status_command, ["--json"]).stdout)
    assert payload["paired"] is True and payload["topos_id"] == "topos-from-shell"
    assert payload["exit_code"] == 0


def test_doctor_counts_secret_sightings_without_values(home, caplog):
    node = _fake_node()
    node.settings["claude_code_sessions"]["secret_sightings"] = 2
    with serve_fake_node(node, _socket_path()):
        result = _run(status_cmd.doctor_command, ["--json"])
    assert result.exit_code == 2
    findings = {f["code"]: f for f in json.loads(result.stdout)["findings"]}
    finding = findings["secrets_in_session_files"]
    assert "2 of your coding-tool session files" in finding["message"]
    assert "topos-node pair" in finding["fix"]
    _assert_no_secret(result, caplog)

    node.settings["claude_code_sessions"].pop("secret_sightings")
    with serve_fake_node(node, _socket_path()):
        clean = _run(status_cmd.doctor_command, ["--json"])
    assert "secrets_in_session_files" not in {f["code"] for f in json.loads(clean.stdout)["findings"]}
