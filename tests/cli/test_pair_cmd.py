"""`topos-node pair` — the key never shows, pairing.json is private and walks its states.

Guards for CONTRACTS §3/§4 (lane L7). Every test runs against a scratch HOME and a
fake control plane behind ``httpx.MockTransport``; nothing here touches the real
``~/.topos`` or the network.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner

from topos.cli import pair_cmd
from topos.cli.commands import main

DEVICE_CODE = "d" * 43
USER_CODE = "BCDF-GHJK"
VERIFY_URL = "https://topos.dialogues.ai/app/pair"
MASKED = "j•••@dialogues.ai"


def _key() -> str:
    return "tpn_" + secrets.token_hex(20)


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TOPOS_ENV_FILE", raising=False)
    # setenv-then-delenv: delenv alone records nothing for an ABSENT variable, so a
    # value the code under test sets for real would leak into later tests.
    monkeypatch.setenv("TOPOS_KEY", "")
    monkeypatch.delenv("TOPOS_KEY")
    monkeypatch.setattr(pair_cmd, "_computer_name", lambda: "Test Mac")
    monkeypatch.setattr(pair_cmd, "_spawn_poller", lambda: 4242)
    monkeypatch.setattr(pair_cmd, "_pid_alive", lambda pid: True)
    return tmp_path


class _FakeCP:
    """The §3 routes, in memory. ``calls`` records (path, body) in order; the ack
    handler also records whether ``.env`` already held the key at that moment."""

    def __init__(self, *, key: str, env_path: Path, approve_after: int = 1,
                 slow_down_once: bool = False, mode_result: str = "approved",
                 start_status: int = 200, agent: dict | None = None,
                 poll_script: list | None = None):
        self.key = key
        #: Optional scripted poll answers consumed first: (status_code, json) per poll.
        self.poll_script = list(poll_script or [])
        self.starts = 0
        self.env_path = env_path
        self.approve_after = approve_after
        self.slow_down_once = slow_down_once
        self.mode_result = mode_result
        self.start_status = start_status
        self.agent = agent
        self.calls: list[tuple] = []
        self.polls = 0
        self.start_mode: str | None = None
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content or b"{}")
        self.calls.append((path, body))
        if path.endswith("/device-pairing/start"):
            if self.start_status != 200:
                return httpx.Response(self.start_status, json={"error": "not_found"})
            self.start_mode = body.get("mode")
            if self.agent is None and body.get("client") not in (None, "cli"):
                self.agent = {"client": body["client"]}
            self.starts += 1
            expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
            # A restarted pairing gets a NEW code (and device code).
            suffix = "" if self.starts == 1 else str(self.starts)
            return httpx.Response(200, json={
                "device_code": DEVICE_CODE[: len(DEVICE_CODE) - len(suffix)] + suffix,
                "user_code": USER_CODE if self.starts == 1 else f"QRST-VWXZ",
                "verification_url": VERIFY_URL, "interval": 5, "expires_at": expires,
            })
        if path.endswith("/device-pairing/poll"):
            self.polls += 1
            if self.poll_script:
                code, payload = self.poll_script.pop(0)
                return httpx.Response(code, json=payload)
            if self.slow_down_once and self.polls == 1:
                return httpx.Response(429, json={"error": "slow_down"})
            if self.polls <= self.approve_after:
                return httpx.Response(200, json={"status": "pending"})
            if self.mode_result == "denied":
                return httpx.Response(200, json={"status": "denied"})
            payload = {"status": "approved", "masked_account": MASKED,
                       "topos_id": "tp_0123", "agent": self.agent}
            if self.start_mode == "create":
                payload["topos_key"] = self.key
            return httpx.Response(200, json=payload)
        if path.endswith("/device-pairing/ack"):
            has_key = self.env_path.is_file() and self.key in self.env_path.read_text()
            self.calls.append(("ack_env_has_key", has_key))
            return httpx.Response(204)
        return httpx.Response(500, json={"error": "unexpected"})


def _scan_for(home: Path, needle: str) -> list[str]:
    hits = []
    for p in home.rglob("*"):
        if p.is_file():
            try:
                if needle in p.read_text(errors="ignore"):
                    hits.append(str(p.relative_to(home)))
            except OSError:
                pass
    return hits


def test_pair_never_writes_the_key_anywhere_but_env(home: Path, monkeypatch):
    key = _key()
    env_path = home / ".topos" / ".env"
    cp = _FakeCP(key=key, env_path=env_path)
    monkeypatch.setattr(pair_cmd, "_transport", cp.transport)

    result = CliRunner().invoke(main, ["pair", "--json"])
    assert result.exit_code == 2, result.output + result.stderr
    assert key not in result.output and key not in result.stderr
    printed = json.loads(result.stdout)
    assert printed["user_code"] == USER_CODE
    assert printed["verification_url"] == VERIFY_URL
    assert printed["state"] == "pending"
    assert "device_code" not in printed and "topos_key" not in printed

    pairing = home / ".topos" / "pairing.json"
    assert stat.S_IMODE(pairing.stat().st_mode) == 0o600
    assert json.loads(pairing.read_text())["state"] == "pending"

    # The detached poller, run inline: pending → approved (key saved) → ack → delivered.
    sleeps: list[float] = []
    pair_cmd.run_poller(sleep=sleeps.append)
    states = json.loads(pairing.read_text())
    assert states["state"] == "delivered"
    assert states["masked_account"] == MASKED
    assert states["topos_id"] == "tp_0123"
    assert states["poller_pid"] == os.getpid()
    assert stat.S_IMODE(pairing.stat().st_mode) == 0o600
    assert stat.S_IMODE(env_path.stat().st_mode) == 0o600
    assert f"TOPOS_KEY={key}" in env_path.read_text()

    # ack arrives only after the key is on disk, and exactly once.
    assert ("ack_env_has_key", True) in cp.calls
    assert ("ack_env_has_key", False) not in cp.calls
    assert [c[0] for c in cp.calls if c[0].endswith("/ack")] == ["/v1/topos/device-pairing/ack"]

    # The key is in .env and nowhere else under HOME.
    assert _scan_for(home, key) == [".topos/.env"]
    # pairing.json never carried it through any state (the file is rewritten per state;
    # the delivered record is what remains, so also check the recorded history).
    assert key not in pairing.read_text()


def test_pairing_json_walks_pending_approved_delivered(home: Path, monkeypatch):
    key = _key()
    env_path = home / ".topos" / ".env"
    cp = _FakeCP(key=key, env_path=env_path, approve_after=2)
    monkeypatch.setattr(pair_cmd, "_transport", cp.transport)
    seen: list[str] = []
    real_write = pair_cmd.write_pairing

    def _recording_write(record):
        seen.append(record["state"])
        assert "topos_key" not in json.dumps(record)
        return real_write(record)

    monkeypatch.setattr(pair_cmd, "write_pairing", _recording_write)
    result = CliRunner().invoke(main, ["pair"])
    assert result.exit_code == 2, result.output + result.stderr
    assert USER_CODE in result.output and VERIFY_URL in result.output
    pair_cmd.run_poller(sleep=lambda _s: None)
    assert seen[0] == "pending"
    assert seen[-2:] == ["approved", "delivered"]
    assert "denied" not in seen and "error" not in seen


def test_poller_honours_slow_down(home: Path, monkeypatch):
    key = _key()
    env_path = home / ".topos" / ".env"
    cp = _FakeCP(key=key, env_path=env_path, slow_down_once=True, approve_after=1)
    monkeypatch.setattr(pair_cmd, "_transport", cp.transport)
    assert CliRunner().invoke(main, ["pair"]).exit_code == 2
    sleeps: list[float] = []
    pair_cmd.run_poller(sleep=sleeps.append)
    # First poll at the advertised interval (5), 429 slow_down adds 5 (RFC 8628 §3.5),
    # so every later wait is 10.
    assert sleeps[0] == 5
    assert sleeps[1] == 10
    assert json.loads((home / ".topos" / "pairing.json").read_text())["interval"] == 10
    assert json.loads((home / ".topos" / "pairing.json").read_text())["state"] == "delivered"


def test_manual_without_a_tty_exits_2_with_the_exact_message(home: Path, monkeypatch):
    monkeypatch.setattr(pair_cmd, "_can_prompt_for_input", lambda: False)
    result = CliRunner().invoke(main, ["pair", "--manual"])
    assert result.exit_code == 2
    assert result.stderr.strip() == (
        "Error: `topos-node pair --manual` needs a terminal. "
        "Open your own terminal (not the assistant's) and run it there."
    )
    assert not (home / ".topos" / ".env").exists()


def test_manual_with_a_tty_saves_the_key_without_echoing_it(home: Path, monkeypatch):
    key = _key()
    monkeypatch.setattr(pair_cmd, "_can_prompt_for_input", lambda: True)
    result = CliRunner().invoke(main, ["pair", "--manual"], input=key + "\n")
    assert result.exit_code == 0, result.output + result.stderr
    assert key not in result.output and key not in result.stderr
    assert f"TOPOS_KEY={key}" in (home / ".topos" / ".env").read_text()


def test_cp_404_prints_the_exact_fallback_message(home: Path, monkeypatch):
    cp = _FakeCP(key=_key(), env_path=home / ".topos" / ".env", start_status=404)
    monkeypatch.setattr(pair_cmd, "_transport", cp.transport)
    result = CliRunner().invoke(main, ["pair"])
    assert result.exit_code == 2
    assert result.stderr.strip() == (
        "Error: pairing isn't available on this control plane yet; "
        "run `topos-node pair --manual` in your own terminal"
    )
    assert not (home / ".topos" / "pairing.json").exists()


def test_wait_prints_masked_account_and_exit_0(home: Path, monkeypatch):
    key = _key()
    cp = _FakeCP(key=key, env_path=home / ".topos" / ".env")
    monkeypatch.setattr(pair_cmd, "_transport", cp.transport)
    # The poller runs the moment it is "spawned", so --wait finds a terminal state.
    monkeypatch.setattr(pair_cmd, "_spawn_poller", lambda: (pair_cmd.run_poller(sleep=lambda _s: None), 4242)[1])
    result = CliRunner().invoke(main, ["pair", "--wait"])
    assert result.exit_code == 0, result.output + result.stderr
    assert f"Paired with {MASKED}." in result.output
    assert "Is that you?" in result.output
    assert key not in result.output and key not in result.stderr


def test_wait_exit_5_when_denied(home: Path, monkeypatch):
    cp = _FakeCP(key=_key(), env_path=home / ".topos" / ".env", mode_result="denied")
    monkeypatch.setattr(pair_cmd, "_transport", cp.transport)
    monkeypatch.setattr(pair_cmd, "_spawn_poller", lambda: (pair_cmd.run_poller(sleep=lambda _s: None), 4242)[1])
    result = CliRunner().invoke(main, ["pair", "--wait"])
    assert result.exit_code == 5
    assert json.loads((home / ".topos" / "pairing.json").read_text())["state"] == "denied"
    assert not (home / ".topos" / ".env").exists()


def test_add_agent_mode_records_approval_and_moves_no_key(home: Path, monkeypatch):
    existing = _key()
    env_path = home / ".topos" / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(f"TOPOS_KEY={existing}\n")
    cp = _FakeCP(key="unused", env_path=env_path, agent={"client": "claude-code"})
    monkeypatch.setattr(pair_cmd, "_transport", cp.transport)
    result = CliRunner().invoke(main, ["pair", "--add-agent", "--client", "claude-code"])
    assert result.exit_code == 2, result.output + result.stderr
    start_body = next(b for p, b in cp.calls if p.endswith("/start"))
    assert start_body["client"] == "claude-code"
    assert start_body["platform"] in ("macos", "linux", "windows")
    assert start_body["computer_name"] == "Test Mac"
    assert re.fullmatch(r"[0-9a-f-]{36}", start_body["node_install_id"])
    pair_cmd.run_poller(sleep=lambda _s: None)
    rec = json.loads((home / ".topos" / "pairing.json").read_text())
    assert rec["mode"] == "add_agent" and rec["client"] == "claude-code"
    assert rec["state"] == "delivered" and rec["agent"] == {"client": "claude-code"}
    assert env_path.read_text() == f"TOPOS_KEY={existing}\n"


def test_install_id_is_created_once_and_private(home: Path, monkeypatch):
    cp = _FakeCP(key=_key(), env_path=home / ".topos" / ".env")
    monkeypatch.setattr(pair_cmd, "_transport", cp.transport)
    CliRunner().invoke(main, ["pair"])
    first = (home / ".topos" / "install-id").read_text().strip()
    assert stat.S_IMODE((home / ".topos" / "install-id").stat().st_mode) == 0o600
    (home / ".topos" / "pairing.json").unlink()
    CliRunner().invoke(main, ["pair"])
    assert (home / ".topos" / "install-id").read_text().strip() == first
    ids = {b["node_install_id"] for p, b in cp.calls if p.endswith("/start")}
    assert ids == {first}


def test_rerun_while_pending_reuses_the_code(home: Path, monkeypatch):
    cp = _FakeCP(key=_key(), env_path=home / ".topos" / ".env")
    monkeypatch.setattr(pair_cmd, "_transport", cp.transport)
    runner = CliRunner()
    assert runner.invoke(main, ["pair"]).exit_code == 2
    second = runner.invoke(main, ["pair"])
    assert second.exit_code == 2
    assert USER_CODE in second.output
    assert len([p for p, _ in cp.calls if p.endswith("/start")]) == 1


def test_start_body_carries_the_mode_and_create_approval_records_the_agent(home: Path, monkeypatch):
    """Contract change 22:05Z: one approval covers the Topos AND the agent. The start body
    says which mode; in create mode the poller records `agent` exactly as add-agent mode
    does, so `agents add --approved` works after a fresh pairing."""
    key = _key()
    env_path = home / ".topos" / ".env"
    cp = _FakeCP(key=key, env_path=env_path)
    monkeypatch.setattr(pair_cmd, "_transport", cp.transport)
    result = CliRunner().invoke(main, ["pair", "--client", "claude-code"])
    assert result.exit_code == 2, result.output
    start_body = next(b for p, b in cp.calls if p.endswith("/start"))
    assert start_body["mode"] == "create"
    pair_cmd.run_poller(sleep=lambda _s: None)
    rec = json.loads((home / ".topos" / "pairing.json").read_text())
    assert rec["state"] == "delivered"
    assert rec["mode"] == "create"
    assert rec["agent"] == {"client": "claude-code"}
    assert f"TOPOS_KEY={key}" in env_path.read_text()
    assert key not in (home / ".topos" / "pairing.json").read_text()


def test_add_agent_start_body_carries_the_mode(home: Path, monkeypatch):
    env_path = home / ".topos" / ".env"
    env_path.parent.mkdir(parents=True)
    env_path.write_text("TOPOS_KEY=" + _key() + "\n")
    cp = _FakeCP(key="unused", env_path=env_path)
    monkeypatch.setattr(pair_cmd, "_transport", cp.transport)
    assert CliRunner().invoke(main, ["pair", "--add-agent", "--client", "codex"]).exit_code == 2
    start_body = next(b for p, b in cp.calls if p.endswith("/start"))
    assert start_body["mode"] == "add_agent"
    assert start_body["client"] == "codex"


def test_poll_409_cap_is_recorded_and_polling_continues(home: Path, monkeypatch):
    """L8: a 409 from poll is the five-Topos cap; its `detail` is shown and polling goes on at
    `interval` (the owner may archive a Topos and the next poll creates it)."""
    key = _key()
    env_path = home / ".topos" / ".env"
    cp = _FakeCP(key=key, env_path=env_path, approve_after=0,
                 poll_script=[(409, {"detail": "Topos limit reached (5 active). Archive one to continue."})])
    monkeypatch.setattr(pair_cmd, "_transport", cp.transport)
    assert CliRunner().invoke(main, ["pair"]).exit_code == 2
    sleeps: list[float] = []
    assert pair_cmd.run_poller(sleep=sleeps.append) == "delivered"
    rec = json.loads((home / ".topos" / "pairing.json").read_text())
    assert rec["notice"] == "Topos limit reached (5 active). Archive one to continue."
    assert rec["state"] == "delivered"
    assert sleeps == [5, 5]  # the 409 cost one interval, no backoff, no error state
    assert f"TOPOS_KEY={key}" in env_path.read_text()


def test_expired_poll_restarts_the_pairing_from_start(home: Path, monkeypatch):
    key = _key()
    env_path = home / ".topos" / ".env"
    cp = _FakeCP(key=key, env_path=env_path, approve_after=0, poll_script=[(200, {"status": "expired"})])
    monkeypatch.setattr(pair_cmd, "_transport", cp.transport)
    assert CliRunner().invoke(main, ["pair"]).exit_code == 2
    seen: list[tuple] = []
    real_write = pair_cmd.write_pairing
    monkeypatch.setattr(pair_cmd, "write_pairing", lambda r: (seen.append((r["state"], r["user_code"])), real_write(r))[1])
    assert pair_cmd.run_poller(sleep=lambda _s: None) == "delivered"
    assert cp.starts == 2
    rec = json.loads((home / ".topos" / "pairing.json").read_text())
    assert rec["user_code"] == "QRST-VWXZ" and rec["restarts"] == 1 and rec["state"] == "delivered"
    assert ("pending", "QRST-VWXZ") in seen  # the new code was written as pending before polling resumed
    assert "expired" not in [state for state, _ in seen]
    assert f"TOPOS_KEY={key}" in env_path.read_text()


def test_restarts_are_bounded(home: Path, monkeypatch):
    cp = _FakeCP(key=_key(), env_path=home / ".topos" / ".env", approve_after=0,
                 poll_script=[(200, {"status": "expired"})] * 10)
    monkeypatch.setattr(pair_cmd, "_transport", cp.transport)
    assert CliRunner().invoke(main, ["pair"]).exit_code == 2
    assert pair_cmd.run_poller(sleep=lambda _s: None) == "expired"
    assert cp.starts == 1 + pair_cmd.MAX_RESTARTS


def test_poll_delivered_is_terminal_ok(home: Path, monkeypatch):
    cp = _FakeCP(key=_key(), env_path=home / ".topos" / ".env",
                 poll_script=[(200, {"status": "delivered", "masked_account": MASKED, "topos_id": "tp_0123"})])
    monkeypatch.setattr(pair_cmd, "_transport", cp.transport)
    assert CliRunner().invoke(main, ["pair"]).exit_code == 2
    assert pair_cmd.run_poller(sleep=lambda _s: None) == "delivered"
    rec = json.loads((home / ".topos" / "pairing.json").read_text())
    assert rec["state"] == "delivered" and rec["masked_account"] == MASKED
    assert not [c for c in cp.calls if c[0].endswith("/ack")]  # already delivered: nothing to ack


def test_wait_prints_the_cap_notice_while_pending(home: Path, monkeypatch):
    cp = _FakeCP(key=_key(), env_path=home / ".topos" / ".env")
    monkeypatch.setattr(pair_cmd, "_transport", cp.transport)
    assert CliRunner().invoke(main, ["pair"]).exit_code == 2
    rec = pair_cmd.read_pairing()
    rec["notice"] = "Topos limit reached (5 active). Archive one to continue."
    pair_cmd.write_pairing(rec)
    # One pending look (code + notice shown), then the poller "expires" it: --wait exits 2.
    def _expire(_seconds: float) -> None:
        current = pair_cmd.read_pairing()
        current["state"] = "expired"
        pair_cmd.write_pairing(current)

    monkeypatch.setattr(pair_cmd, "_wait_sleep", _expire)
    result = CliRunner().invoke(main, ["pair", "--wait"])
    assert result.exit_code == 2
    assert result.stdout.count("Topos limit reached (5 active). Archive one to continue.") == 1
    assert result.stdout.count(USER_CODE) == 1
