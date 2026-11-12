"""Guards for `topos-node pushers add|list|disconnect` (cli/pushers_cmd.py).

protects: a key is minted only after a human says yes on a TTY (no flag skips the prompt, an agent without a
TTY is told to hand the step over); the key is printed exactly once and never logged; sources and origins
are checked before anything is written (an unknown source, a non-push source or a wildcard origin enrol
nothing); `disconnect` tombstones the row; a disconnected app re-enrols on a fresh yes.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.public

from topos import local_pushers as lp  # noqa: E402
from topos.cli import pushers_cmd  # noqa: E402

ORIGIN = "chrome-extension://" + "c" * 32


@pytest.fixture
def db(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "node.db"
    sqlite3.connect(str(path)).close()
    monkeypatch.setattr(pushers_cmd, "_database_path", lambda: path)
    return path


def _run(args, input_text=None):
    return CliRunner().invoke(pushers_cmd.pushers_group, args, input=input_text, catch_exceptions=False)


def _rows(db):
    conn = sqlite3.connect(str(db))
    try:
        return lp.list_pushers(conn)
    finally:
        conn.close()


def _verify(db, key):
    conn = sqlite3.connect(str(db))
    try:
        return lp.verify_pusher_key(conn, key)
    finally:
        conn.close()


ADD = ["add", "browser-history-plugin", "--sources", "browser_visits,browser_events", "--origin", ORIGIN]


def test_add_without_a_tty_exits_2_and_mints_nothing(db, monkeypatch):
    monkeypatch.setattr(pushers_cmd, "_can_prompt", lambda: False)
    result = _run(ADD)
    assert result.exit_code == 2
    assert "yes from you in a terminal" in result.stderr and "pushers add browser-history-plugin" in result.stderr
    assert _rows(db) == []


def test_add_with_tty_no_declines(db, monkeypatch):
    monkeypatch.setattr(pushers_cmd, "_can_prompt", lambda: True)
    result = _run(ADD, input_text="n\n")
    assert result.exit_code == 2 and "Not connected" in result.stderr
    assert _rows(db) == []


def test_add_with_tty_yes_prints_the_key_once_and_enrols(db, monkeypatch, caplog):
    monkeypatch.setattr(pushers_cmd, "_can_prompt", lambda: True)
    result = _run(ADD, input_text="y\n")
    assert result.exit_code == 0, result.stderr
    assert "Dialogues Browser History" in result.stderr  # the question names the app
    assert "browser_visits, browser_events" in result.stderr and ORIGIN in result.stderr
    keys = [w for w in result.stdout.split() if w.startswith("tpw_")]
    assert len(keys) == 1
    key = keys[0]
    assert key not in result.stderr and key not in caplog.text
    row = _verify(db, key)
    assert row["app"] == "browser-history-plugin"
    assert row["grant"] == lp.PusherGrant(("browser_visits", "browser_events"), (ORIGIN,))


def test_add_json_carries_the_key_and_the_grant(db, monkeypatch):
    monkeypatch.setattr(pushers_cmd, "_can_prompt", lambda: True)
    result = _run(ADD + ["--json"], input_text="y\n")
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["app"] == "browser-history-plugin" and payload["sources"] == ["browser_visits", "browser_events"]
    assert payload["origins"] == [ORIGIN] and payload["key"].startswith("tpw_browser-history-plugin.")
    assert _verify(db, payload["key"]) is not None


@pytest.mark.parametrize("args, said", [
    (["add", "x", "--sources", "no_such_source"], "no source named"),
    (["add", "x", "--sources", "imessage"], "not a source an app pushes"),
    (["add", "x", "--sources", "browser_visits", "--origin", "chrome-extension://*"], "not an extension origin"),
    (["add", "x", "--sources", "browser_visits", "--origin", "https://example.test"], "not an extension origin"),
    (["add", "!!!", "--sources", "browser_visits"], "can't be an app name"),
])
def test_a_bad_source_origin_or_name_enrols_nothing_before_any_prompt(db, monkeypatch, args, said):
    monkeypatch.setattr(pushers_cmd, "_can_prompt", lambda: True)
    result = _run(args, input_text="y\n")
    assert result.exit_code == 2 and said in result.stderr
    assert _rows(db) == []


def test_add_without_a_database_says_start_first(tmp_path, monkeypatch):
    monkeypatch.setattr(pushers_cmd, "_database_path", lambda: tmp_path / "missing.db")
    monkeypatch.setattr(pushers_cmd, "_can_prompt", lambda: True)
    result = _run(ADD, input_text="y\n")
    assert result.exit_code == 2 and "hasn't been started" in result.stderr


def test_list_and_disconnect(db, monkeypatch):
    monkeypatch.setattr(pushers_cmd, "_can_prompt", lambda: True)
    assert _run(["list"]).stdout.strip() == "(no apps connected)"
    key = json.loads(_run(ADD + ["--json"], input_text="y\n").stdout)["key"]
    listed = json.loads(_run(["list", "--json"]).stdout)
    assert [(r["app"], r["revoked"], r["sources"]) for r in listed] == [
        ("browser-history-plugin", False, ["browser_visits", "browser_events"])]
    assert not any("tpw_" in json.dumps(r) for r in listed)
    gone = _run(["disconnect", "browser-history-plugin"])
    assert gone.exit_code == 0 and "Disconnected" in gone.stdout
    assert _verify(db, key) is None
    assert "(disconnected)" in _run(["list"]).stdout
    assert _run(["disconnect", "browser-history-plugin"]).exit_code == 2
    assert _run(["disconnect", "nobody"]).exit_code == 2


def test_a_disconnected_app_re_enrols_on_a_fresh_yes_with_a_new_key(db, monkeypatch):
    monkeypatch.setattr(pushers_cmd, "_can_prompt", lambda: True)
    first = json.loads(_run(ADD + ["--json"], input_text="y\n").stdout)["key"]
    _run(["disconnect", "browser-history-plugin"])
    second = json.loads(_run(ADD + ["--json"], input_text="y\n").stdout)["key"]
    assert first != second
    assert _verify(db, first) is None and _verify(db, second) is not None
