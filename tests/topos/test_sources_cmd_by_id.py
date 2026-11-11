"""Guards for `topos-node sources` on the sources that are not a coding tool's transcripts.

protects: a setup prompt can name a source by the app's name or its id and reach the same door; an
agent's shell (no terminal) can never turn on iMessage or Signal by itself — it is told a person is
needed (exit 2, ``human.say``) or opens the window where the question waits; the yes a person gives on
a terminal is what puts the one-change confirmation on the request; a browser extension's source is
answered with the steps that are the person's, never a pretend "enabled"; and, as for every `sources`
verb, nothing the node's body says is printed (the fake node plants a secret in each one).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.public

from tests.topos.fake_node_socket import FakeNode, serve_fake_node  # noqa: E402
from topos.cli import sources_cmd  # noqa: E402

PLANTED = "tpn_" + "planted" + "0123456789abcdef" * 2
PREVIEW = {"source_id": "imessage", "backfill": "30d", "messages": 12345, "in_window": 812, "conversations": None,
           "skipped_spam": 40, "oldest": "2019-03-02T10:00:00+00:00", "newest": "2026-11-10T09:00:00+00:00"}


def _socket_path() -> Path:
    return Path(os.environ["TOPOS_UDS_PATH"])


def _run(args, input=None):
    return CliRunner().invoke(sources_cmd.sources_group, args, input=input, catch_exceptions=False)


def _node() -> FakeNode:
    node = FakeNode()
    node.planted_secret = PLANTED
    node.source_status = {
        "imessage": {"source_id": "imessage", "enabled": False, "readiness": {"ready": True, "code": None}},
        "signal": {"source_id": "signal", "enabled": False, "readiness": {"ready": True, "code": None}},
        "browser_visits": {"source_id": "browser_visits", "connected": False},
        "chatgpt_ui_conversation": {"source_id": "chatgpt_ui_conversation", "connected": False},
    }
    node.door_results[("imessage", "preview")] = (200, dict(PREVIEW))
    node.door_results[("signal", "preview")] = (200, {**PREVIEW, "source_id": "signal"})
    return node


def _doors(node: FakeNode):
    return [(c["source_id"], c["action"], c["body"], bool(c["confirm"])) for c in node.calls if c["route"] == "door"]


def _clean(*results) -> None:
    for result in results:
        assert PLANTED not in result.stdout + result.stderr


# --- names --------------------------------------------------------------------------


@pytest.mark.parametrize("word, source_id", [
    ("iMessage Sync", "imessage"), ("Signal Sync", "signal"), ("IMESSAGE", "imessage"),
])
def test_a_name_reaches_the_same_door_as_the_id(word: str, source_id: str, monkeypatch):
    node = _node()
    monkeypatch.setattr(sources_cmd, "_can_prompt", lambda: True)
    with serve_fake_node(node, _socket_path()):
        result = _run(["preview", word, "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _doors(node) == [(source_id, "preview", {"backfill": "30d"}, True)]
    assert json.loads(result.stdout)["source_id"] == source_id


def test_a_word_that_names_no_source_is_refused_before_the_node_is_asked():
    node = _node()
    with serve_fake_node(node, _socket_path()):
        result = _run(["enable", "My Whatsapp Chats"])
    assert result.exit_code == 2 and "sources list" in result.stderr
    assert node.calls == []


# --- list ---------------------------------------------------------------------------


def test_list_prints_this_programs_words_and_the_nodes_flags_only():
    node = _node()
    node.sources_listing = [
        {"source_id": "claude_code_sessions", "enabled": True, "paused": False,
         "last_sync_at": "2026-11-10T19:00:00+00:00", "name": f"evil {PLANTED}"},
        {"source_id": "imessage", "enabled": False, "ready": False, "needs": "full_disk_access_needed",
         "reads": PLANTED},
        {"source_id": "signal", "enabled": False, "ready": True, "needs": None},
        {"source_id": "browser_visits", "connected": True, "last_write_at": "not a stamp " + PLANTED},
    ]
    with serve_fake_node(node, _socket_path()):
        as_json = _run(["list", "--json"])
        human = _run(["list"])
    assert as_json.exit_code == 0 and human.exit_code == 0
    rows = {row["source_id"]: row for row in json.loads(as_json.stdout)["sources"]}
    assert rows["claude_code_sessions"]["enabled"] is True and rows["claude_code_sessions"]["name"] == "Claude Code"
    assert rows["imessage"]["needs"] == "full_disk_access_needed" and rows["imessage"]["ready"] is False
    assert rows["signal"]["ready"] is True and rows["signal"]["needs"] is None
    assert rows["browser_visits"]["connected"] is True and rows["browser_visits"]["last_write_at"] is None
    assert rows["chatgpt_ui_conversation"]["connected"] is False, "a source the node did not list is not connected"
    assert "iMessage Sync" in rows["imessage"]["aliases"]
    assert "imessage" in human.stdout and "off (needs: full_disk_access_needed)" in human.stdout
    assert "not connected (extension: chatgpt-shadow-extension)" in human.stdout
    _clean(as_json, human)


def test_list_on_a_node_that_predates_it_says_to_update():
    node = _node()
    with serve_fake_node(node, _socket_path()):
        result = _run(["list"])
    assert result.exit_code == 1 and "uv tool upgrade topos-node" in result.stderr


# --- iMessage and Signal ----------------------------------------------------------------


def test_preview_is_the_persons_to_ask_for_and_prints_counts_only(monkeypatch):
    node = _node()
    with serve_fake_node(node, _socket_path()):
        unasked = _run(["preview", "imessage", "--json"])
        assert unasked.exit_code == 2 and json.loads(unasked.stdout)["code"] == "owner_confirmation_required"
        assert _doors(node) == [], "without a terminal the store is not counted"
        monkeypatch.setattr(sources_cmd, "_can_prompt", lambda: True)
        human = _run(["preview", "imessage"])
        everything = _run(["preview", "imessage", "--backfill", "all", "--json"])
    assert human.exit_code == 0
    assert "12,345 messages" in human.stdout and "2019-03-02 to 2026-11-10" in human.stdout and "812" in human.stdout
    assert _doors(node)[1] == ("imessage", "preview", {"backfill": "all"}, True)
    assert json.loads(everything.stdout) == {
        "source_id": "imessage", "messages": 12345, "in_window": 812, "skipped_spam": 40, "backfill": "all",
        "oldest": "2019-03-02T10:00:00+00:00", "newest": "2026-11-10T09:00:00+00:00",
    }
    _clean(human, everything)


def test_without_a_terminal_enable_says_a_person_is_needed_and_posts_nothing():
    node = _node()
    with serve_fake_node(node, _socket_path()):
        result = _run(["enable", "imessage", "--json"])
    assert result.exit_code == 2, result.stdout + result.stderr
    body = json.loads(result.stdout)
    assert body["status"] == "needs_human" and body["code"] == "owner_confirmation_required"
    assert "topos-node sources enable imessage" in body["human"]["say"] and "--open-terminal" in body["hint"]
    assert _doors(node) == [], "no preview, no enable: an agent's shell learns nothing and changes nothing"
    _clean(result)


def test_without_a_terminal_a_source_that_cannot_be_read_is_reported_before_any_window(monkeypatch):
    node = _node()
    node.source_status["imessage"]["readiness"] = {"ready": False, "code": "full_disk_access_needed",
                                                   "message": f"door text {PLANTED}"}
    opened = []
    monkeypatch.setattr("topos.cli.terminal_window.open_terminal_window", lambda **kw: opened.append(kw) or True)
    with serve_fake_node(node, _socket_path()):
        result = _run(["enable", "imessage", "--open-terminal", "--json"])
    assert result.exit_code == 2
    body = json.loads(result.stdout)
    assert body["code"] == "full_disk_access_needed" and "Full Disk Access" in body["human"]["say"]
    assert sources_cmd._node_program() in body["human"]["say"]
    assert opened == [] and _doors(node) == []
    _clean(result)


def test_open_terminal_opens_the_question_and_grants_nothing(monkeypatch):
    node = _node()
    opened = []
    monkeypatch.setattr("topos.cli.terminal_window.open_terminal_window", lambda **kw: opened.append(kw) or True)
    with serve_fake_node(node, _socket_path()):
        result = _run(["enable", "Signal Sync", "--open-terminal", "--backfill", "all", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == {"source_id": "signal", "status": "approval_pending"}
    assert opened[0]["argv"] == ["sources", "enable", "signal", "--backfill", "all"]
    assert _doors(node) == []

    monkeypatch.setattr("topos.cli.terminal_window.open_terminal_window", lambda **kw: False)
    with serve_fake_node(node, _socket_path()):
        failed = _run(["enable", "signal", "--open-terminal", "--json"])
    assert failed.exit_code == 2 and json.loads(failed.stdout)["code"] == "owner_confirmation_required"


def test_a_source_that_is_already_on_is_reported_without_asking_anyone(monkeypatch):
    node = _node()
    node.source_status["imessage"]["enabled"] = True
    monkeypatch.setattr("topos.cli.terminal_window.open_terminal_window",
                        lambda **kw: pytest.fail("no window for a source that is already on"))
    with serve_fake_node(node, _socket_path()):
        result = _run(["enable", "imessage", "--open-terminal", "--json"])
    assert result.exit_code == 0 and json.loads(result.stdout) == {"source_id": "imessage", "status": "enabled"}


def test_on_a_terminal_a_yes_previews_then_enables_with_the_confirmation(monkeypatch):
    node = _node()
    node.door_results[("imessage", "enable")] = (200, {"status": "ok", "source_id": "imessage", "scheduled": True})
    monkeypatch.setattr(sources_cmd, "_can_prompt", lambda: True)
    with serve_fake_node(node, _socket_path()):
        result = _run(["enable", "imessage", "--json"], input="y\n")
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _doors(node) == [
        ("imessage", "preview", {"backfill": "30d"}, True),
        ("imessage", "enable", {"backfill": "30d"}, True),
    ]
    confirmations = [c["confirm"] for c in node.calls if c["route"] == "door"]
    assert confirmations[0] != confirmations[1], "a confirmation is spent by one request; each gets its own"
    assert json.loads(result.stdout) == {"source_id": "imessage", "status": "enabled", "backfill": "30d", "scheduled": True}
    # The person saw the counts and what is read before the question.
    assert "12,345 messages" in result.stderr and "other people sent you" in result.stderr
    assert "sent to its AI company" in result.stderr
    _clean(result)


def test_on_a_terminal_anything_but_yes_enables_nothing(monkeypatch):
    node = _node()
    monkeypatch.setattr(sources_cmd, "_can_prompt", lambda: True)
    with serve_fake_node(node, _socket_path()):
        declined = _run(["enable", "imessage"], input="n\n")
        silent = _run(["enable", "imessage"], input="\n")
    assert declined.exit_code == 2 and silent.exit_code == 2
    assert [d[1] for d in _doors(node)] == ["preview", "preview"]
    assert "Not turned on." in declined.stderr


def test_signal_says_it_is_not_read_on_a_schedule(monkeypatch):
    node = _node()
    node.door_results[("signal", "enable")] = (200, {"status": "ok", "source_id": "signal", "scheduled": False})
    monkeypatch.setattr(sources_cmd, "_can_prompt", lambda: True)
    with serve_fake_node(node, _socket_path()):
        result = _run(["enable", "signal", "--backfill", "all"], input="y\n")
    assert result.exit_code == 0, result.stdout + result.stderr
    assert "everything on this computer" in result.stdout and "topos-node sources sync signal" in result.stdout


@pytest.mark.parametrize("code, exit_code, words", [
    ("signal_key_unavailable", 2, "Signal Safe Storage"),
    ("signal_reader_missing", 2, "topos-node[signal]"),
    ("messages_not_found", 1, "no Messages history"),
])
def test_a_refusal_is_said_in_this_programs_words_with_its_exit_code(monkeypatch, code, exit_code, words):
    node = _node()
    node.door_results[("signal", "preview")] = (409, {"error": code, "message": f"door text {PLANTED}"})
    monkeypatch.setattr(sources_cmd, "_can_prompt", lambda: True)
    with serve_fake_node(node, _socket_path()):
        result = _run(["enable", "signal", "--json"], input="y\n")
    assert result.exit_code == exit_code
    body = json.loads(result.stdout)
    assert body["code"] == code and words in body["human"]["say"]
    assert body["status"] == ("needs_human" if exit_code == 2 else "refused")
    assert [d[1] for d in _doors(node)] == ["preview"]
    _clean(result)


def test_pause_is_plain_and_resume_and_sync_ask_first(monkeypatch):
    node = _node()
    node.door_results[("imessage", "sync")] = (200, {"status": "ok", "job_id": "job-9", "already_running": False})
    with serve_fake_node(node, _socket_path()):
        pause = _run(["pause", "imessage", "--json"])
        resume = _run(["resume", "imessage", "--json"])
        sync = _run(["sync", "imessage"])
        monkeypatch.setattr(sources_cmd, "_can_prompt", lambda: True)
        resumed = _run(["resume", "imessage", "--json"], input="y\n")
        synced = _run(["sync", "imessage", "--json"], input="y\n")
    assert pause.exit_code == 0 and json.loads(pause.stdout) == {"source_id": "imessage", "status": "paused"}
    assert resume.exit_code == 2 and json.loads(resume.stdout)["code"] == "owner_confirmation_required"
    assert sync.exit_code == 2 and "own terminal" in sync.stderr
    assert resumed.exit_code == 0 and json.loads(resumed.stdout) == {"source_id": "imessage", "status": "resumed"}
    assert json.loads(synced.stdout) == {"source_id": "imessage", "status": "queued", "job_id": "job-9"}
    assert _doors(node) == [
        ("imessage", "pause", {}, False),
        ("imessage", "resume", {}, True),
        ("imessage", "sync", {}, True),
    ]


def test_exit_3_when_the_node_has_no_identity(monkeypatch):
    node = _node()
    node.paired = False
    monkeypatch.setattr(sources_cmd, "_can_prompt", lambda: True)
    with serve_fake_node(node, _socket_path()):
        result = _run(["preview", "imessage"])
    assert result.exit_code == 3
    _clean(result)


# --- the extensions' sources ------------------------------------------------------------


@pytest.mark.parametrize("word, source_id, app, sources", [
    ("Browser History Plugin", "browser_visits", "browser-history-plugin", "browser_visits,browser_events"),
    ("ChatGPT Shadow", "chatgpt_ui_conversation", "chatgpt-shadow-extension", "chatgpt_ui_conversation"),
])
def test_an_extension_source_is_answered_with_the_persons_three_steps(word, source_id, app, sources):
    node = _node()
    with serve_fake_node(node, _socket_path()):
        result = _run(["enable", word, "--json"])
        human = _run(["enable", word])
    assert result.exit_code == 2 and human.exit_code == 2
    body = json.loads(result.stdout)
    assert body["status"] == "needs_human" and body["code"] == "extension_setup" and body["app"] == app
    assert body["command"].startswith(f"topos-node pushers add {app} --sources {sources} --origin ")
    assert body["install_url"] == f"https://github.com/dialoguesai/{app}"
    assert "own terminal" in body["human"]["say"] and body["install_url"] in body["human"]["say"]
    assert _doors(node) == [], "nothing is posted: the node cannot install an extension"
    _clean(result, human)


def test_a_connected_extension_is_reported_as_connected():
    node = _node()
    node.source_status["browser_visits"]["connected"] = True
    with serve_fake_node(node, _socket_path()):
        result = _run(["enable", "browser_visits", "--json"])
        preview = _run(["preview", "browser_visits"])
        sync = _run(["sync", "browser_visits"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"source_id": "browser_visits", "status": "connected", "app": "browser-history-plugin"}
    assert preview.exit_code == 0 and "nothing to preview" in preview.stdout
    assert sync.exit_code == 1 and _doors(node) == []


# --- the window ---------------------------------------------------------------------------


def test_the_window_script_runs_the_command_and_carries_no_format_directive(monkeypatch):
    from topos.cli import terminal_window

    monkeypatch.setenv("HOME", "/tmp/some home")
    script = terminal_window.build_script(
        title="Turn on 100% of iMessage's history", argv=["sources", "enable", "imessage", "--backfill", "30d"],
        done="Done.", not_done="Not turned on.",
    )
    assert script.startswith("#!/bin/sh\n")
    assert "-m topos.cli sources enable imessage --backfill 30d\n" in script
    assert "export HOME='/tmp/some home'\n" in script
    assert "100%% of iMessage" in script, "a percent sign in a title must not be read by printf as a directive"
    assert "TOPOS_KEY" not in script and "token" not in script.lower()
