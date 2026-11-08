"""`topos-node import` past its terminal guard: what it asks, what it sends, and that the node serves what it sends.

The release checks could not reach this part (import_cmd.py, from the TTY guard on): it needs a terminal and a person
answering the prompt. Here both are patched (``isatty`` on the module's ``sys``, and ``click.confirm``), and the owner
socket is a fake that records each request. In 1.5.45 the default import and ``--list`` posted ``/ingestion/…`` while
the app mounts those routes under ``/v1``, so both got 404 (7 Nov 2026); a fake socket alone would not have caught
that, so the last tests match every path the command sends against the real app's routes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import click
import pytest
from click.testing import CliRunner

from topos.cli import import_cmd
from topos.mcp_server import NodeDown, NodeRefused

OTHER_SOURCE = "test_file_source"
SCOPE = {"dataset_id": "ds-local", "identity_mode": "local"}


class _Socket:
    """The owner socket, in memory: ``calls`` records each request; ``answers`` maps (method, path) to a reply or an
    exception to raise."""

    def __init__(self, answers: Optional[Dict[tuple, Any]] = None):
        self.calls: List[Dict[str, Any]] = []
        self.answers = {("GET", "/api/local/owner/scope"): SCOPE, **(answers or {})}

    def request_sync(self, method, path, json_body=None, params=None, label=None, headers=None):
        self.calls.append({"method": method, "path": path, "json": json_body, "params": params, "headers": headers})
        for (m, p), answer in self.answers.items():
            if m == method and (p == path or (p.endswith("*") and path.startswith(p[:-1]))):
                if isinstance(answer, BaseException):
                    raise answer
                return answer
        return {"status": "accepted"}


class _Stream:
    def __init__(self, tty: bool):
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """Run the command with a terminal (or not), a scripted answer to the prompt and a fake socket."""
    tokens: List[str] = []
    monkeypatch.setattr(import_cmd, "_file_sources",
                        lambda: {import_cmd.CHATGPT_SOURCE: "ChatGPT", OTHER_SOURCE: "Test file source"})
    import topos.uds as uds

    def _token() -> str:
        tokens.append("confirm-token")
        return "confirm-token"

    monkeypatch.setattr(uds, "write_owner_confirmation", _token)
    export = tmp_path / "conversations.json"
    export.write_text("[]" + " " * 3000, encoding="utf-8")

    def run(*args: str, tty: bool = True, answer: bool = True, socket: Optional[_Socket] = None):
        sock = socket or _Socket()
        prompts: List[str] = []

        def _confirm(text, default=False, err=False, **_):
            prompts.append(text)
            return answer

        monkeypatch.setattr(import_cmd, "_socket", lambda: sock)
        monkeypatch.setattr(import_cmd, "sys", SimpleNamespace(stdin=_Stream(tty), stdout=_Stream(tty), exit=sys.exit))
        monkeypatch.setattr(click, "confirm", _confirm)
        result = CliRunner().invoke(import_cmd.import_command, list(args), catch_exceptions=False)
        return SimpleNamespace(result=result, calls=sock.calls, prompts=prompts, tokens=list(tokens))

    run.export = export
    return run


def test_it_asks_then_sends_the_file_path_with_one_confirmation(cli):
    out = cli(str(cli.export), "--json")
    assert out.result.exit_code == 0, out.result.output
    assert out.prompts == ["Import conversations.json (2 KB) into ChatGPT?"]
    assert [(c["method"], c["path"]) for c in out.calls] == [("GET", "/api/local/owner/scope"),
                                                            ("POST", "/v1/ingestion/upload-local-path")]
    post = out.calls[1]
    assert post["json"] == {"dataset_id": "ds-local", "file_path": str(cli.export.resolve())}
    assert post["headers"] == {"X-Topos-Owner-Confirm": "confirm-token"} and out.tokens == ["confirm-token"]
    doc = json.loads(out.result.output)
    assert doc["status"] == "accepted" and doc["file"] == "conversations.json" and doc["mode"] == "local"


def test_another_file_source_goes_to_its_own_ingest_route(cli):
    out = cli(str(cli.export), "--source", OTHER_SOURCE)
    assert out.result.exit_code == 0, out.result.output
    post = out.calls[-1]
    assert (post["method"], post["path"]) == ("POST", f"/sources/{OTHER_SOURCE}/ingest")
    assert post["params"] == {"dataset_id": "ds-local", "file_path": str(cli.export.resolve())}
    assert post["headers"] == {"X-Topos-Owner-Confirm": "confirm-token"}
    assert "Imported conversations.json into Test file source (accepted)." in out.result.output


def test_a_no_writes_nothing_and_mints_no_confirmation(cli):
    out = cli(str(cli.export), answer=False)
    assert out.result.exit_code == import_cmd.EXIT_OK
    assert "Nothing imported." in out.result.output
    assert [c["method"] for c in out.calls] == ["GET"]           # read its scope to ask; never posted
    assert out.tokens == []


def test_without_a_terminal_it_asks_no_one_and_touches_nothing(cli):
    out = cli(str(cli.export), "--json", tty=False)
    assert out.result.exit_code == import_cmd.EXIT_NEEDS_HUMAN
    assert json.loads(out.result.output)["exit_code"] == import_cmd.EXIT_NEEDS_HUMAN
    assert out.calls == [] and out.prompts == [] and out.tokens == []


def test_a_refused_import_says_so_and_fails(cli):
    sock = _Socket({("POST", "/v1/ingestion/upload-local-path"): NodeRefused(404, "http_404")})
    out = cli(str(cli.export), socket=sock)
    assert out.result.exit_code == import_cmd.EXIT_FAILED
    assert "Topos refused the import (http_404)." in out.result.output


def test_a_node_that_stops_mid_import_is_reported_down(cli):
    sock = _Socket({("POST", "/v1/ingestion/upload-local-path"): NodeDown("down")})
    out = cli(str(cli.export), socket=sock)
    assert out.result.exit_code == import_cmd.EXIT_NODE_DOWN
    assert "Topos stopped while importing." in out.result.output


def test_an_error_status_from_the_node_fails_the_command(cli):
    sock = _Socket({("POST", "/v1/ingestion/upload-local-path"): {"status": "error", "error": "unreadable export"}})
    out = cli(str(cli.export), socket=sock)
    assert out.result.exit_code == import_cmd.EXIT_FAILED
    assert "Import failed: unreadable export" in out.result.output


# ----------------------------------------------------------------------------- the node serves what the CLI sends


def _served(method: str, path: str) -> bool:
    from starlette.routing import Match

    from topos.app import app

    scope = {"type": "http", "method": method, "path": path, "root_path": "", "query_string": b"", "headers": []}
    return any(route.matches(scope)[0] == Match.FULL for route in app.routes)


def test_every_path_the_command_sends_is_a_route_the_node_serves(cli):
    sent = []
    for args in ([str(cli.export)], [str(cli.export), "--source", OTHER_SOURCE], ["--list"]):
        out = cli(*args, socket=_Socket({("GET", "/v1/ingestion/local-exports"): {"exports": []}}))
        assert out.result.exit_code == 0, out.result.output
        sent += [(c["method"], c["path"]) for c in out.calls]
    assert ("POST", "/v1/ingestion/upload-local-path") in sent and ("GET", "/v1/ingestion/local-exports") in sent
    missing = sorted({f"{m} {p}" for m, p in sent if not _served(m, p)})
    assert not missing, f"the CLI sends paths the node does not serve: {missing}"


def test_the_route_check_would_have_caught_1545():
    assert not _served("POST", "/ingestion/upload-local-path")
    assert not _served("GET", "/ingestion/local-exports")
