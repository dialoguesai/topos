"""Agent preparation never becomes installation without the human terminal handoff."""
import json

import click
import pytest

pytestmark = pytest.mark.public
from click.testing import CliRunner

from topos.cli import source_create_cmd as cmd
from topos.mcp_server import NodeRefused


@pytest.fixture
def flow(tmp_path, monkeypatch):
    definition = tmp_path / "definition.json"
    definition.write_text('{"source_id":"personal_notes"}')
    sample = tmp_path / "sample.json"
    sample.write_text('[{"id":"one","text":"Synthetic"}]')
    calls, tokens = [], []
    plan = {"status": "ready", "source_id": "personal_notes", "plan_sha256": "approved-plan",
            "source_definition_json": {"source_id": "personal_notes", "display_name": "Personal notes",
                "source_type": "file", "delivery": "owner_upload", "posture": "mixed", "enrichment_trigger": "manual"},
            "sample": {"file_name": "sample.json", "bytes": 33, "records_checked": 1, "truncated": False}}
    receipt = {"status": "ingested", "installed": True, "registered": False}

    class Socket:
        def request_sync(self, method, path, **kwargs):
            calls.append((method, path, kwargs))
            return plan if path == cmd.PREPARE else receipt

    monkeypatch.setattr(cmd, "_socket", Socket)
    monkeypatch.setattr(cmd, "_tty", lambda: False)
    monkeypatch.setattr("topos.uds.write_owner_confirmation", lambda: tokens.append("token") or "token")
    def run(*args):
        return CliRunner().invoke(cmd.add_command, [str(definition), "--sample", str(sample), *args], catch_exceptions=False)
    return definition, sample, plan, receipt, calls, tokens, run


def test_check_returns_preview_without_install_or_token(flow):
    *_, calls, tokens, run = flow
    result = run("--check", "--json")
    assert result.exit_code == 0 and json.loads(result.output)["status"] == "ready"
    assert [path for _, path, _ in calls] == [cmd.PREPARE] and tokens == []


def test_agent_gets_handoff_never_installs(flow):
    *_, calls, tokens, run = flow
    result = run("--json")
    assert result.exit_code == 2
    doc = json.loads(result.output)
    assert doc["status"] == "needs_human" and doc["installed"] is False
    assert "--expected-input-sha256" in doc["command"]
    assert len(calls) == 1 and not tokens


def test_open_terminal_carries_digest_and_actual_paths(flow, monkeypatch):
    definition, sample, *_, calls, tokens, run = flow
    opened = []
    monkeypatch.setattr("topos.cli.terminal_window.open_terminal_window", lambda **kw: opened.append(kw) or True)
    result = run("--open-terminal", "--json")
    assert result.exit_code == 2 and json.loads(result.output)["status"] == "approval_pending"
    assert str(definition) in opened[0]["argv"] and str(sample) in opened[0]["argv"]
    assert "--expected-input-sha256" in opened[0]["argv"] and not tokens


def test_changed_handoff_files_refused_before_socket(flow):
    *_, calls, tokens, run = flow
    result = run("--expected-input-sha256", "different")
    assert result.exit_code == 1 and "changed after preparation" in result.output
    assert not calls and not tokens


def test_decline_does_not_create_confirmation(flow, monkeypatch):
    *_, calls, tokens, run = flow
    monkeypatch.setattr(cmd, "_tty", lambda: True)
    monkeypatch.setattr(click, "confirm", lambda *a, **k: False)
    result = run()
    assert result.exit_code == 0 and "Nothing changed" in result.output
    assert len(calls) == 1 and not tokens


def test_approval_uses_previewed_bytes_even_if_file_changes(flow, monkeypatch):
    definition, sample, *_, calls, tokens, run = flow
    original = sample.read_text()
    monkeypatch.setattr(cmd, "_tty", lambda: True)
    def confirm(*a, **k):
        sample.write_text('[{"text":"changed"}]')
        return True
    monkeypatch.setattr(click, "confirm", confirm)
    result = run()
    assert result.exit_code == 0, result.output
    method, path, sent = calls[-1]
    assert path == cmd.INSTALL and sent["json_body"]["sample"]["content"] == original
    assert sent["json_body"]["plan_sha256"] == "approved-plan"
    assert sent["headers"] == {"X-Topos-Owner-Confirm": "token"} and tokens == ["token"]


def test_partial_import_exits_nonzero(flow, monkeypatch):
    _, _, _, receipt, _, _, run = flow
    receipt["status"] = "import_failed"
    monkeypatch.setattr(cmd, "_tty", lambda: True)
    monkeypatch.setattr(click, "confirm", lambda *a, **k: True)
    result = run()
    assert result.exit_code == 1 and '"installed": true' in result.output


def test_inference_never_needs_node(flow, monkeypatch):
    _, sample, *_ = flow
    monkeypatch.setattr(cmd, "_socket", lambda: pytest.fail("inference contacted node"))
    result = CliRunner().invoke(cmd.infer_command, [str(sample), "--json"])
    assert result.exit_code == 0 and json.loads(result.output)["records_total"] == 1


def test_old_node_returns_upgrade_instruction_without_server_body(flow, monkeypatch):
    class OldNode:
        def request_sync(self, *a, **k):
            raise NodeRefused(404, "http_404")
    monkeypatch.setattr(cmd, "_socket", OldNode)
    result = flow[-1]("--check")
    assert result.exit_code == 1 and "Update Topos first" in result.output


def test_import_catalog_comes_from_running_node(monkeypatch):
    from topos.cli import import_cmd
    class Node:
        def request_sync(self, method, path):
            assert (method, path) == ("GET", "/sources")
            return {"sources": [{"source_id": "custom_file", "display_name": "Custom", "delivery": "owner_upload"},
                                {"source_id": "stream", "delivery": "client_push"}]}
    monkeypatch.setattr(import_cmd, "_socket", Node)
    assert import_cmd._file_sources() == {"custom_file": "Custom"}


def test_custom_stream_is_available_for_pusher_grant(monkeypatch):
    from topos.cli import pushers_cmd
    from topos.local_pushers import parse_sources
    class Node:
        def request_sync(self, method, path):
            assert (method, path) == ("GET", "/sources")
            return {"sources": [{"source_id": "custom_stream", "display_name": "Stream",
                "source_type": "ui_stream", "delivery": "client_push",
                "schema_id": "stream.v1", "parser_id": "stream.v1"}]}
    monkeypatch.setattr("topos.mcp_server.NodeSocket", Node)
    registry = pushers_cmd._source_registry()
    assert parse_sources(["custom_stream"], registry=registry) == ("custom_stream",)
