"""Guards for `topos-node agents add|list|disconnect` (CONTRACTS.md §1.1, §4).

protects: a label is minted only after a human says yes on a TTY, or with
`--approved` backed by the pairing record; a revoked agent is never silently
re-enrolled (rev-1 security finding 12); the label file is 0600 in a 0700
folder and the label itself is never printed; `disconnect` revokes, pauses
every transcript source through the §2.8 door and removes the file; and no
secret reaches stdout, stderr or the log.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.public

from tests.topos.fake_node_socket import FakeNode, serve_fake_node  # noqa: E402
from topos import mcp_clients  # noqa: E402
from topos.cli import agents_cmd  # noqa: E402

LABEL = "tpa_claude-code." + "cd" * 20
ENV_KEY = "-".join(("injected", "topos", "key", "q7" * 8))
ENV_OWNER_KEY = "ok_" + "injected" + "0123456789abcdef" * 3
SECRETS = (LABEL, ENV_KEY, ENV_OWNER_KEY)


class FakeRegistry:
    """L1's four functions (CONTRACTS §1.1) over a dict, so this lane's tests
    run before `topos/mcp_clients.py` grows them."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.minted: list[str] = []
        self.revoked: list[str] = []

    def mint_agent_label(self, conn, *, client: str, approved: bool = False) -> dict:
        assert isinstance(conn, sqlite3.Connection)
        existing = self.rows.get(client)
        if existing and existing.get("revoked_at") and not approved:
            raise ValueError(f"agent {client} was revoked; re-enrolment needs approval")
        self.minted.append(client)
        row = {
            "client": client, "client_id": f"agent-{client}", "display_name": client.title(),
            "principal_class": "owner_agent", "created_at": "2026-10-15T21:00:00+00:00",
            "last_used_at": None, "revoked_at": None,
        }
        self.rows[client] = row
        return {**row, "label": LABEL}

    def revoke_agent(self, conn, *, client: str) -> bool:
        self.revoked.append(client)
        row = self.rows.get(client)
        if not row or row.get("revoked_at"):
            return False
        row["revoked_at"] = "2026-10-15T21:30:00+00:00"
        return True

    def list_agents(self, conn) -> list[dict]:
        return [dict(r) for r in self.rows.values()]


@pytest.fixture
def registry(monkeypatch) -> FakeRegistry:
    reg = FakeRegistry()
    for name in ("mint_agent_label", "revoke_agent", "list_agents"):
        monkeypatch.setattr(mcp_clients, name, getattr(reg, name), raising=False)
    return reg


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
    return tmp_path


def _run(args, *, input_text=None):
    runner = CliRunner()
    return runner.invoke(agents_cmd.agents_group, args, input=input_text, catch_exceptions=False)


def _assert_no_secret(result, caplog):
    blob = result.stdout + result.stderr + caplog.text
    for secret in SECRETS:
        assert secret not in blob


def _socket_path() -> Path:
    return Path(os.environ["TOPOS_UDS_PATH"])


# ------------------------------------------------------------------------ add
def test_add_without_tty_and_without_approved_exits_2(home, registry, monkeypatch, caplog):
    monkeypatch.setattr(agents_cmd, "_can_prompt", lambda: False)
    result = _run(["add", "claude-code"])
    assert result.exit_code == 2
    assert "terminal" in (result.stdout + result.stderr)
    assert registry.minted == []
    assert not (home / ".topos" / "agents" / "claude-code.token").exists()
    _assert_no_secret(result, caplog)


def test_add_with_tty_yes_writes_0600_label_in_0700_dir_and_never_prints_it(home, registry, monkeypatch, caplog):
    monkeypatch.setattr(agents_cmd, "_can_prompt", lambda: True)
    result = _run(["add", "claude-code"], input_text="y\n")
    assert result.exit_code == 0, result.stdout + result.stderr
    token = home / ".topos" / "agents" / "claude-code.token"
    assert token.read_text(encoding="utf-8") == LABEL + "\n"
    assert stat.S_IMODE(token.stat().st_mode) == 0o600
    assert stat.S_IMODE(token.parent.stat().st_mode) == 0o700
    assert registry.minted == ["claude-code"]
    _assert_no_secret(result, caplog)
    written = [p for p in home.rglob("*") if p.is_file() and p != token]
    for path in written:
        assert LABEL not in path.read_text(encoding="utf-8", errors="replace"), path


def test_add_with_tty_no_declines_with_exit_2(home, registry, monkeypatch):
    monkeypatch.setattr(agents_cmd, "_can_prompt", lambda: True)
    result = _run(["add", "claude-code"], input_text="n\n")
    assert result.exit_code == 2
    assert registry.minted == []
    assert not (home / ".topos" / "agents" / "claude-code.token").exists()


def test_add_json_reports_the_client_without_the_label(home, registry, monkeypatch):
    monkeypatch.setattr(agents_cmd, "_can_prompt", lambda: True)
    result = _run(["add", "claude-code", "--json"], input_text="y\n")
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["client"] == "claude-code"
    assert payload["enrolled_at"] == "2026-10-15T21:00:00+00:00"
    assert LABEL not in result.stdout


def test_readd_after_revoke_without_approved_is_refused_even_on_a_tty(home, registry, monkeypatch):
    registry.mint_agent_label(sqlite3.connect(":memory:"), client="claude-code")
    registry.revoke_agent(None, client="claude-code")
    registry.minted.clear()
    monkeypatch.setattr(agents_cmd, "_can_prompt", lambda: True)
    result = _run(["add", "claude-code"], input_text="y\n")
    assert result.exit_code == 2
    assert "disconnected" in (result.stdout + result.stderr)
    assert "pair --add-agent" in (result.stdout + result.stderr)
    assert registry.minted == []
    assert not (home / ".topos" / "agents" / "claude-code.token").exists()


def test_approved_needs_the_pairing_record(home, registry, monkeypatch):
    monkeypatch.setattr(agents_cmd, "_can_prompt", lambda: False)
    result = _run(["add", "claude-code", "--approved"])
    assert result.exit_code == 2
    assert registry.minted == []

    pairing = home / ".topos" / "pairing.json"
    pairing.write_text(json.dumps({"state": "approved", "mode": "create", "agent": {"client": "codex"}}))
    result = _run(["add", "claude-code", "--approved"])
    assert result.exit_code == 2, "a record for another client is not this client's approval"
    assert registry.minted == []

    pairing.write_text(json.dumps({"state": "approved", "mode": "add_agent", "agent": {"client": "claude-code"}}))
    result = _run(["add", "claude-code", "--approved"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert registry.minted == ["claude-code"]


def test_approved_with_the_record_re_enrols_a_revoked_agent(home, registry, monkeypatch):
    registry.mint_agent_label(sqlite3.connect(":memory:"), client="claude-code")
    registry.revoke_agent(None, client="claude-code")
    registry.minted.clear()
    monkeypatch.setattr(agents_cmd, "_can_prompt", lambda: False)
    (home / ".topos" / "pairing.json").write_text(
        json.dumps({"state": "delivered", "mode": "create", "agent": {"client": "claude-code"}})
    )
    result = _run(["add", "claude-code", "--approved"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert registry.minted == ["claude-code"]


def test_unknown_client_name_exits_2(home, registry, monkeypatch):
    monkeypatch.setattr(agents_cmd, "_can_prompt", lambda: True)
    result = _run(["add", "chatgpt"], input_text="y\n")
    assert result.exit_code == 2
    assert registry.minted == []


# ----------------------------------------------------------------------- list
def test_list_json_shape_without_hashes(home, registry, caplog):
    registry.mint_agent_label(sqlite3.connect(":memory:"), client="claude-code")
    registry.mint_agent_label(sqlite3.connect(":memory:"), client="codex")
    registry.revoke_agent(None, client="codex")
    result = _run(["list", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == [
        {"client": "claude-code", "enrolled_at": "2026-10-15T21:00:00+00:00", "revoked": False},
        {"client": "codex", "enrolled_at": "2026-10-15T21:00:00+00:00", "revoked": True},
    ]
    _assert_no_secret(result, caplog)
    human = _run(["list"])
    assert "claude-code" in human.stdout and "codex" in human.stdout
    _assert_no_secret(human, caplog)


def test_list_empty(home, registry):
    result = _run(["list", "--json"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == []


# ----------------------------------------------------------------- disconnect
def test_disconnect_revokes_pauses_every_transcript_source_and_removes_the_file(home, registry, monkeypatch, caplog):
    monkeypatch.setattr(agents_cmd, "_can_prompt", lambda: True)
    assert _run(["add", "claude-code"], input_text="y\n").exit_code == 0
    token = home / ".topos" / "agents" / "claude-code.token"
    assert token.exists()
    node = FakeNode()
    with serve_fake_node(node, _socket_path()):
        result = _run(["disconnect", "claude-code"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert registry.revoked == ["claude-code"]
    paused = sorted((c["source_id"], c["action"]) for c in node.calls if c["route"] == "door")
    assert paused == [
        ("claude_code_sessions", "pause"), ("codex_sessions", "pause"), ("cursor_sessions", "pause"),
    ]
    assert not token.exists()
    _assert_no_secret(result, caplog)


def test_disconnect_with_node_down_still_revokes_and_removes_but_exits_2(home, registry, monkeypatch):
    monkeypatch.setattr(agents_cmd, "_can_prompt", lambda: True)
    assert _run(["add", "claude-code"], input_text="y\n").exit_code == 0
    path = _socket_path()
    if path.exists():
        path.unlink()
    result = _run(["disconnect", "claude-code"])
    assert result.exit_code == 2
    assert registry.revoked == ["claude-code"]
    assert not (home / ".topos" / "agents" / "claude-code.token").exists()
    assert "start topos" in (result.stdout + result.stderr).lower()


def test_disconnect_of_an_unknown_agent_exits_2(home, registry):
    node = FakeNode()
    with serve_fake_node(node, _socket_path()):
        result = _run(["disconnect", "codex"])
    assert result.exit_code == 2


def test_add_passes_the_approval_verdict_to_the_registry(home, registry, monkeypatch):
    """L1's mint refuses a revoked row unless approved=True; the CLI's gates decide, the call carries it."""
    seen: list[bool] = []
    real = registry.mint_agent_label

    def spy(conn, *, client, approved=False):
        seen.append(approved)
        return real(conn, client=client, approved=approved)

    monkeypatch.setattr(mcp_clients, "mint_agent_label", spy, raising=False)
    monkeypatch.setattr(agents_cmd, "_can_prompt", lambda: True)
    assert _run(["add", "claude-code"], input_text="y\n").exit_code == 0
    (home / ".topos" / "pairing.json").write_text(
        json.dumps({"state": "approved", "mode": "add_agent", "agent": {"client": "claude-code"}})
    )
    assert _run(["add", "claude-code", "--approved"]).exit_code == 0
    assert seen == [False, True]


def test_pending_or_denied_record_is_not_an_approval(home, registry, monkeypatch):
    monkeypatch.setattr(agents_cmd, "_can_prompt", lambda: False)
    pairing = home / ".topos" / "pairing.json"
    for state in ("pending", "denied", "expired", "error"):
        pairing.write_text(json.dumps({"state": state, "mode": "add_agent", "agent": {"client": "claude-code"}}))
        assert _run(["add", "claude-code", "--approved"]).exit_code == 2, state
    assert registry.minted == []
