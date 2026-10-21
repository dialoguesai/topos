"""`topos-node setup` drives the whole on-ramp and stops only where a person must act.

Each test runs the real step logic against a fake computer: no node, no
control plane, no Terminal window. The fake records every side effect so the
tests can assert what setup did NOT do as well as what it did.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from topos.cli import agents_cmd, pair_cmd, setup_cmd


class Computer:
    def __init__(self) -> None:
        self.key = False
        self.running = False
        self.app_installed = False
        self.agents: list[dict] = []
        self.sources: list[dict] = []
        self.pairing: dict | None = None
        self.calls: list[tuple] = []
        self.enable_refusal: str | None = None
        self.approval_window_opens = True

    # --- what setup reads
    def status(self) -> dict:
        return {"node": {"running": self.running}, "agents": list(self.agents), "sources": list(self.sources)}

    def reusable_pending(self, record) -> bool:
        return bool(record) and record.get("state") == "pending"

    def approves(self, client) -> bool:
        rec = self.pairing or {}
        return rec.get("state") in ("approved", "delivered") and (rec.get("agent") or {}).get("client") == client \
            and not rec.get("approval_spent")

    # --- what setup does
    def start_pairing(self, client, mode) -> dict:
        self.calls.append(("start_pairing", client, mode))
        self.pairing = {"mode": mode, "client": client, "state": "pending", "user_code": "ABCD-EFGH",
                        "verification_url": "https://topos.example/app/pair", "device_code": "SECRET-DEVICE",
                        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}
        return dict(self.pairing)

    def run_self(self, args, timeout=0.0):
        self.calls.append(("run_self", *args))
        if args[:2] in (["service", "install"], ["start", "--headless"]):
            self.running = True
            return 0, {}
        if args[:2] == ["agents", "add"] and "--approved" in args:
            self.agents.append({"client": args[2], "revoked": False})
            self.pairing["approval_spent"] = True
            return 0, {"client": args[2]}
        if args[:2] == ["agents", "add"] and "--open-terminal" in args:
            return (0, {"status": "approval_pending"}) if self.approval_window_opens else (2, None)
        return 1, None

    def owner_door(self, method, path, body=None):
        self.calls.append(("door", method, path))
        if path.endswith("/enable"):
            if self.enable_refusal:
                return None, self.enable_refusal
            sid = path.split("/")[-2]
            self.sources.append({"source_id": sid, "enabled": True, "paused": False})
            return {"status": "enabled"}, None
        if path.endswith("/preview"):
            return {"sessions": 3, "bytes_text": 100, "folders": ["proj"], "oldest": "a", "newest": "b",
                    "secret_field": "x"}, None
        return {"processing": {"jobs": []}, "last_sync_at": None}, None

    def open_app(self):
        self.calls.append(("open_app",))
        self.running = True

    # --- what a person or the poller does
    def approve(self, client="claude-code", masked="j•••@example.com"):
        self.key = True
        self.pairing.update(state="delivered", masked_account=masked, agent={"client": client})


@pytest.fixture
def pc(monkeypatch, tmp_path) -> Computer:
    c = Computer()
    monkeypatch.setattr(setup_cmd, "_has_key", lambda: c.key)
    monkeypatch.setattr(setup_cmd, "_status", c.status)
    monkeypatch.setattr(setup_cmd, "_start_pairing", c.start_pairing)
    monkeypatch.setattr(setup_cmd, "_run_self", c.run_self)
    monkeypatch.setattr(setup_cmd, "_owner_door", c.owner_door)
    monkeypatch.setattr(setup_cmd, "_topos_app_installed", lambda: c.app_installed)
    monkeypatch.setattr(setup_cmd, "_open_topos_app", c.open_app)
    monkeypatch.setattr(setup_cmd, "_wait_for_node", lambda timeout: c.running)
    monkeypatch.setattr(setup_cmd.sys, "platform", "darwin")
    monkeypatch.setattr(pair_cmd, "read_pairing", lambda: dict(c.pairing) if c.pairing else None)
    monkeypatch.setattr(pair_cmd, "write_pairing", lambda rec: setattr(c, "pairing", dict(rec)))
    monkeypatch.setattr(pair_cmd, "_reusable_pending", c.reusable_pending)
    monkeypatch.setattr(pair_cmd, "topos_dir", lambda: tmp_path)
    monkeypatch.setattr(agents_cmd, "_pairing_record_approves", c.approves)
    return c


def run(**kw) -> dict:
    return setup_cmd.run_setup(client=kw.pop("client", "claude-code"), **kw)


def side_effects(c: Computer) -> list[tuple]:
    return [call for call in c.calls if call[0] != "door" or call[1] != "GET"]


def test_a_new_computer_is_set_up_with_two_human_stops(pc) -> None:
    first = run()
    assert (first["step"], first["exit_code"], first["human"]["action"]) == ("key", 2, "approve_code")
    assert first["human"]["code"] == "ABCD-EFGH"
    assert pc.running is False  # a node with no key exits at once: pairing comes first

    again = run()  # the agent re-runs while the person is still in the browser
    assert again["step"] == "key" and [c for c in pc.calls if c[0] == "start_pairing"] == [
        ("start_pairing", "claude-code", "create")]

    pc.approve()
    confirm = run()
    assert (confirm["step"], confirm["exit_code"], confirm["human"]["action"]) == ("account", 2, "confirm_account")
    assert confirm["human"]["say"] == "Approved by j•••@example.com — is that you?"
    assert pc.running is False  # nothing reads the account before the person says yes

    done = run(confirm_account=True)
    assert (done["state"], done["exit_code"]) == ("done", 0)
    assert done["done"] == ["key", "account", "node", "agent", "source"]
    assert ("run_self", "service", "install", "--port", "8676") in pc.calls
    assert ("run_self", "agents", "add", "claude-code", "--approved", "--json") in pc.calls
    assert pc.sources == [{"source_id": "claude_code_sessions", "enabled": True, "paused": False}]


def test_a_finished_setup_is_a_no_op(pc) -> None:
    pc.key, pc.running = True, True
    pc.agents = [{"client": "claude-code", "revoked": False}]
    pc.sources = [{"source_id": "claude_code_sessions", "enabled": True, "paused": False}]

    doc = run()

    assert (doc["state"], doc["exit_code"]) == ("done", 0)
    assert side_effects(pc) == []


def test_plan_changes_nothing(pc) -> None:
    doc = run(plan=True)

    assert (doc["state"], doc["step"], doc["exit_code"]) == ("planned", "key", 2)
    assert pc.calls == [] and pc.pairing is None


def test_plan_on_a_running_node_reports_preview_counts_only(pc) -> None:
    pc.key, pc.running = True, True
    pc.agents = [{"client": "claude-code", "revoked": False}]

    doc = run(plan=True)

    assert doc["step"] == "source"
    assert doc["preview"] == {"sessions": 3, "bytes_text": 100, "folders": ["proj"], "oldest": "a", "newest": "b"}
    assert not any(c[0] == "door" and c[2].endswith("/enable") for c in pc.calls)


def test_an_existing_topos_opens_the_terminal_prompt_once(pc) -> None:
    pc.key, pc.running = True, True

    first = run()
    second = run()

    assert first["human"]["action"] == second["human"]["action"] == "approve_in_terminal"
    opened = [c for c in pc.calls if c[:3] == ("run_self", "agents", "add") and "--open-terminal" in c]
    assert len(opened) == 1
    assert not any(c[0] == "start_pairing" for c in pc.calls)


def test_a_revoked_agent_needs_a_browser_approval_not_a_terminal_prompt(pc) -> None:
    pc.key, pc.running = True, True
    pc.agents = [{"client": "claude-code", "revoked": True}]

    doc = run()

    assert doc["human"]["action"] == "approve_code"
    assert ("start_pairing", "claude-code", "add_agent") in pc.calls
    assert not any("--open-terminal" in c for c in pc.calls)


def test_topos_app_runs_the_node_when_it_is_installed(pc) -> None:
    pc.key, pc.app_installed = True, True

    run()

    assert ("open_app",) in pc.calls
    assert not any(c[:3] == ("run_self", "service", "install") for c in pc.calls)


def test_disk_floor_stops_with_the_plain_sentence(pc) -> None:
    pc.key, pc.running = True, True
    pc.agents = [{"client": "claude-code", "revoked": False}]
    pc.enable_refusal = "disk_floor"

    doc = run()

    assert (doc["step"], doc["exit_code"], doc["human"]["action"]) == ("source", 2, "free_disk")


def test_a_first_connection_in_progress_asks_for_a_re_run(pc) -> None:
    pc.key, pc.running = True, True
    pc.agents = [{"client": "claude-code", "revoked": False}]
    pc.enable_refusal = "not_paired"

    doc = run()

    assert (doc["step"], doc["exit_code"], doc["human"]["action"]) == ("source", 2, "wait")


def test_a_denied_pairing_exits_5(pc) -> None:
    pc.pairing = {"mode": "create", "state": "denied"}

    doc = run()

    assert (doc["state"], doc["exit_code"]) == ("failed", 5)


def test_a_paused_source_is_left_paused(pc) -> None:
    pc.key, pc.running = True, True
    pc.agents = [{"client": "claude-code", "revoked": False}]
    pc.sources = [{"source_id": "claude_code_sessions", "enabled": True, "paused": True}]

    doc = run()

    assert doc["exit_code"] == 0
    assert not any(c[0] == "door" and c[2].endswith("/enable") for c in pc.calls)
    assert any("paused" in note for note in doc["notes"])


def test_cli_output_never_carries_the_device_code(pc) -> None:
    result = CliRunner().invoke(setup_cmd.setup_command, ["--client", "claude-code", "--json"])

    assert result.exit_code == 2
    assert "SECRET-DEVICE" not in result.output
    assert json.loads(result.output)["human"]["code"] == "ABCD-EFGH"


def test_a_non_default_port_never_hands_the_node_to_topos_app(pc) -> None:
    # Topos.app runs the default-port node; a second node on another port is setup's own.
    pc.key, pc.app_installed = True, True

    run(port=9123, service=False)

    assert ("open_app",) not in pc.calls
    assert ("run_self", "start", "--headless", "--port", "9123") in pc.calls
