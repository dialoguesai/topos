"""Guards for `topos-node sources preview|enable|pause|resume|sync` (CONTRACTS.md §2.8, §4).

protects: each verb hits its §2.8 door over the owner socket with the
contract body; exit codes are 0/3/4; the command prints a projection of
counts and status words, never a response body (the fake node plants a secret
in every body); `--quiet` prints nothing on success.
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


def _socket_path() -> Path:
    return Path(os.environ["TOPOS_UDS_PATH"])


def _run(args):
    return CliRunner().invoke(sources_cmd.sources_group, args, catch_exceptions=False)


def _node() -> FakeNode:
    node = FakeNode()
    node.planted_secret = PLANTED
    return node


def _doors(node: FakeNode):
    return [(c["source_id"], c["action"], c["body"]) for c in node.calls if c["route"] == "door"]


def test_preview_posts_skip_and_prints_counts_only(caplog):
    node = _node()
    with serve_fake_node(node, _socket_path()):
        result = _run(["preview", "claude_code_sessions", "--skip", "alpha", "--skip", "beta"])
        as_json = _run(["preview", "claude_code_sessions", "--json"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _doors(node)[0] == ("claude_code_sessions", "preview", {"skip": ["alpha", "beta"]})
    assert _doors(node)[1] == ("claude_code_sessions", "preview", {"skip": []})
    assert "3 sessions" in result.stdout
    assert json.loads(as_json.stdout) == {
        "sessions": 3, "bytes_text": 1200, "folders": ["alpha", "beta"],
        "oldest": "2026-09-01T00:00:00+00:00", "newest": "2026-10-15T00:00:00+00:00",
    }
    for out in (result, as_json):
        assert PLANTED not in out.stdout + out.stderr + caplog.text


def test_enable_posts_backfill_and_skip():
    node = _node()
    with serve_fake_node(node, _socket_path()):
        default = _run(["enable", "claude_code_sessions"])
        explicit = _run(["enable", "codex_sessions", "--backfill", "all", "--skip", "x"])
    assert default.exit_code == 0 and explicit.exit_code == 0
    assert _doors(node) == [
        ("claude_code_sessions", "enable", {"skip": [], "backfill": "30d"}),
        ("codex_sessions", "enable", {"skip": ["x"], "backfill": "all"}),
    ]
    assert "enabled" in default.stdout


def test_backfill_choices_are_all_or_30d():
    result = _run(["enable", "claude_code_sessions", "--backfill", "90d"])
    assert result.exit_code == 2


@pytest.mark.parametrize("already_running", [False, True])
def test_accepted_sync_reports_job_acceptance_not_completed_ingestion(already_running):
    node = _node()
    node.door_results[("codex_sessions", "sync")] = (200, {
        "status": "ok", "job_id": "job-123", "already_running": already_running,
    })
    with serve_fake_node(node, _socket_path()):
        result = _run(["sync", "codex_sessions", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {
        "source_id": "codex_sessions", "job_id": "job-123",
        "status": "already_running" if already_running else "queued",
    }


def test_pause_resume_and_sync_with_session(caplog):
    node = _node()
    with serve_fake_node(node, _socket_path()):
        pause = _run(["pause", "claude_code_sessions"])
        resume = _run(["resume", "claude_code_sessions"])
        sync = _run(["sync", "claude_code_sessions", "--session", "/tmp/x.jsonl"])
        quiet = _run(["sync", "claude_code_sessions", "--quiet"])
    for r in (pause, resume, sync, quiet):
        assert r.exit_code == 0, r.stdout + r.stderr
    assert _doors(node) == [
        ("claude_code_sessions", "pause", {}),
        ("claude_code_sessions", "resume", {}),
        ("claude_code_sessions", "sync", {"session_path": "/tmp/x.jsonl"}),
        ("claude_code_sessions", "sync", {}),
    ]
    assert quiet.stdout == "" and quiet.stderr == ""
    for r in (pause, resume, sync):
        assert PLANTED not in r.stdout + r.stderr + caplog.text


def test_exit_3_when_not_paired():
    node = _node()
    node.paired = False
    with serve_fake_node(node, _socket_path()):
        result = _run(["enable", "claude_code_sessions"])
    assert result.exit_code == 3
    assert "pair" in result.stdout + result.stderr
    assert PLANTED not in result.stdout + result.stderr


def test_exit_4_when_the_node_is_down(tmp_path, monkeypatch):
    # A socket path of its own that nothing ever listened on: the session-wide path can still be answered by a
    # fake node a neighbouring test is tearing down (Linux CI answered "not paired" here), which is not "down".
    monkeypatch.setenv("TOPOS_UDS_PATH", str(tmp_path / "nobody-home" / "engine.sock"))
    result = _run(["sync", "claude_code_sessions", "--quiet"])
    assert result.exit_code == 4
    assert "Topos isn't running" in result.stderr + result.stdout


def test_door_refusal_prints_the_code_not_the_body(caplog):
    node = _node()
    node.door_results[("claude_code_sessions", "sync")] = (
        409, {"error": "source_not_enabled", "detail": f"settings row {PLANTED} missing"},
    )
    with serve_fake_node(node, _socket_path()):
        result = _run(["sync", "claude_code_sessions"])
    assert result.exit_code == 1
    assert "source_not_enabled" in result.stdout + result.stderr
    assert PLANTED not in result.stdout + result.stderr + caplog.text
    assert "settings row" not in result.stdout + result.stderr


def test_source_ids_are_validated():
    result = _run(["pause", "not a source"])
    assert result.exit_code == 2


def test_quiet_sync_of_a_paused_or_not_enabled_source_is_silent_and_exit_0():
    """The plugin hook after every turn: a 409 from the door is the owner's choice, not noise."""
    node = _node()
    node.door_results[("claude_code_sessions", "sync")] = (409, {"error": "source_not_enabled"})
    with serve_fake_node(node, _socket_path()):
        quiet = _run(["sync", "claude_code_sessions", "--session", "/tmp/agent-transcripts/x.jsonl", "--quiet"])
        loud = _run(["sync", "claude_code_sessions"])
    assert quiet.exit_code == 0
    assert quiet.stdout == "" and quiet.stderr == ""
    assert loud.exit_code == 1 and "source_not_enabled" in loud.stderr
    assert _doors(node)[0][2] == {"session_path": "/tmp/agent-transcripts/x.jsonl"}


def test_reclassify_touches_is_a_dry_run_by_default_and_prints_counts_only(caplog):
    node = _node()
    node.door_results[("claude_code_sessions", "reclassify-touches")] = (200, {
        "status": "ok", "source_id": "claude_code_sessions", "applied": False, "flagged_rows": 9, "would_clear": 4,
        "kept_after_a_read": 3, "kept_no_transcript": 1, "topos_reads": 2, "topos_system_touches": 2,
        "session_hint": PLANTED,
    })
    with serve_fake_node(node, _socket_path()):
        result = _run(["reclassify-touches", "claude_code_sessions"])
    assert result.exit_code == 0, result.stdout + result.stderr
    assert _doors(node) == [("claude_code_sessions", "reclassify-touches", {"apply": False})]
    assert "9 replies are kept owner-only" in result.stdout and "4 would be shareable again" in result.stdout
    assert PLANTED not in result.stdout + result.stderr + caplog.text


def test_reclassify_touches_apply_needs_a_human_at_a_terminal():
    """Applying makes replies shareable: refused without a terminal, before anything reaches the node."""
    node = _node()
    with serve_fake_node(node, _socket_path()):
        result = _run(["reclassify-touches", "claude_code_sessions", "--apply"])
    assert result.exit_code == 1
    assert "run it in your own terminal" in result.stderr
    assert _doors(node) == []
