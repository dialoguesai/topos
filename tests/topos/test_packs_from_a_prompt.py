"""`topos-node packs add --check --json | try | on | off | add --on --backfill`: a pack from a draft to reading.

protects: a coding agent can read what a draft declares (`--check --json`, the words that route a record to it
included) and try the draft's own examples (`try`) with no person, no confirmation and nothing written; switching
a pack on or off, and the staged `add --on [--backfill]`, happen only after a person answers one `[y/N]` on a
terminal. With no terminal they ask no one, send nothing that changes anything and mint no confirmation, and
`--open-terminal` opens the same question bound to the bytes that were checked. The staged add names every effect
before the question, does nothing on a no, and on a yes installs the bytes it showed, sends ONE switch carrying
the digest of the definition it showed, then ONE run start, each with its own confirmation. A stage that fails
leaves the stages before it done and says so. A pack that writes about other people is refused before anything
is asked or sent. The commands print a projection, never a response body.

A terminal and a person are patched (``isatty`` on the module's ``sys``, ``click.confirm``); the owner socket is
an in-memory fake that records each request, except in the last two tests, which run the commands against the
real routes behind the real socket wrapper on a real unix socket. The model is never called. Synthetic only.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import click
import pytest
import yaml
from click.testing import CliRunner

import topos
from topos import uds
from topos.cli import packs_cmd
from topos.features.derivation import backfill_runs as runs
from topos.features.derivation import custom_packs
from topos.features.derivation.packs import load_packs
from topos.features.derivation.prefilter import PackPrefilter
from topos.features.derivation.registry import (bundled_pack_dir, definition_sha, enabled_packs,
                                                runs_as_switched_on, seed_pack_registry)
from topos.mcp_server import NodeDown, NodeRefused

pytestmark = pytest.mark.public

PACK = "custom.gardening"
SHIPPED = "work.career"
OUTWARD = "net.character"
EXAMPLE = Path(topos.__file__).resolve().parents[1] / "docs" / "examples" / "custom.gardening.yaml"
PLANTED = "tpn_" + "planted" + "0123456789abcdef" * 2
SWITCH = f"{packs_cmd.PACKS_PATH}/{PACK}/switch"
MUTATING = ("POST", "PUT", "PATCH", "DELETE")


def definition(**changes) -> Dict[str, Any]:
    data = {
        "pack": PACK, "version": "1.0.0", "title": "My gardening",
        "sensitivity_class": "personal", "role_policy": "authored_only", "net_subject": "deny",
        "routing": {"descriptors": ["Plants the owner grows"], "exemplars": ["I planted basil"]},
        "guidance": {"definitions": "Plants the owner says they grow."},
        "predicates": [{"name": "custom.plant", "value_type": "structured", "cardinality": "multi",
                        "temporal": "interval", "altitude": "stated", "value_schema": {"name": "string"}}],
        "eval": {"gold": [{"text": "I grow tomatoes in my garden.",
                           "expect": [{"predicate": "custom.plant", "value": {"name": "tomatoes"}}]}],
                 "negative_controls": [{"text": "The film had a beautiful garden.", "expect": []}]},
        "consumers": ["assistant_context"],
    }
    data.update(changes)
    return data


def _report(*outcomes: str, stopped: Optional[str] = None) -> Dict[str, Any]:
    """What the node answers for a trial: one row per outcome, gold first, the last one a negative control."""
    rows = []
    for n, outcome in enumerate(outcomes, 1):
        gold = n < len(outcomes)
        rows.append({"kind": "gold" if gold else "negative_control", "index": n if gold else 1,
                     "text": "I grow tomatoes in my garden." if gold else "The film had a beautiful garden.",
                     "role": "authored", "routed": gold,
                     "expected": [{"predicate": "custom.plant", "value": {"name": "tomatoes"}}] if gold else [],
                     "returned": [{"predicate": "custom.plant", "value": {"name": "tomatoes"}, "about": "owner",
                                   "confidence": 0.9, "quote": "I grow tomatoes"}] if outcome == "pass" and gold else [],
                     "rejected": 0, "seconds": 1.5, "outcome": outcome,
                     "reason": {"fail": "expected_fact_missing", "error": "model_call_failed",
                                "skipped": "model_unavailable"}.get(outcome),
                     "missing": 1 if outcome == "fail" else 0, "extra": 0, "planted": PLANTED})
    return {"pack_id": PACK, "version": "1.0.0", "model": "local-9b", "template": "shadow-11",
            "fact_check": "not_run", "stopped": stopped, "seconds": 3.0, "examples": rows, "planted": PLANTED,
            "totals": {"examples": 999}}


class _Socket:
    """The owner socket, in memory: records each request and answers as a node would."""

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.answers: Dict[tuple, Any] = {}
        self.enabled: Dict[str, bool] = {PACK: False, SHIPPED: False, OUTWARD: False}
        self.active: Optional[Dict[str, Any]] = None

    def sent(self, *methods: str) -> List[tuple]:
        return [(c["method"], c["path"]) for c in self.calls if not methods or c["method"] in methods]

    def request_sync(self, method, path, json_body=None, params=None, label=None, headers=None):
        self.calls.append({"method": method, "path": path, "json": json_body, "params": params, "headers": headers})
        answer = self.answers.get((method, path))
        if isinstance(answer, BaseException):
            raise answer
        if answer is not None:
            return answer
        if (method, path) == ("GET", packs_cmd.PACKS_PATH):
            return {"packs": [{"pack_id": pid, "title": pid, "enabled": on, "fact_count": 7, "last_run_at": None,
                               "planted": PLANTED} for pid, on in self.enabled.items()],
                    "processing_30d": {"available": True, "models": [
                        {"model": "local-9b", "phase": "extraction", "calls": 100, "total_latency_ms": 2_280_000.0}]},
                    "message": f"key={PLANTED}"}
        if (method, path) == ("GET", packs_cmd.CURRENT_PATH):
            return {"run": self.active}
        if (method, path) == ("GET", packs_cmd.PLAN_PATH):
            pid = params["pack_id"][0]
            return {"packs": [{"pack_id": pid, "waiting": 412 if self.enabled.get(pid) else None,
                               "extract_route": "llm", "extract_model": "local-9b", "note": PLANTED}],
                    "extraction": {"provider": "llm", "llm_model": "local-9b", "classifier_model": None},
                    "verify_model": "local-27b", "history_window": 20000}
        if (method, path) == ("GET", packs_cmd.TRY_PATH):
            return {"model": "local-9b", "available": "yes", "max_examples": 20, "planted": PLANTED}
        if (method, path) == ("POST", packs_cmd.TRY_PATH):
            return _report("pass", "pass")
        if method == "PUT" and path.endswith("/switch"):
            pid = path.split("/")[-2]
            changed = self.enabled.get(pid) != json_body["enabled"]
            self.enabled[pid] = json_body["enabled"]
            return {"pack_id": pid, "enabled": json_body["enabled"], "changed": changed, "planted": PLANTED}
        if (method, path) == ("POST", packs_cmd.RUNS_PATH):
            return {"run": {"run_id": "bfr_1", "state": "running", "limit": json_body["limit"],
                            "until_done": json_body["until_done"], "since": json_body["since"],
                            "packs": [{"pack_id": p, "state": "waiting"} for p in json_body["pack_ids"]],
                            "totals": {}, "planted": PLANTED}}
        raise AssertionError(f"unexpected request {method} {path}")


class _Stream:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


@pytest.fixture
def cli(tmp_path, monkeypatch):
    home = tmp_path / "node" / "fact-packs"
    monkeypatch.setattr(custom_packs, "custom_pack_dir", lambda: home)
    custom_packs._SAID.clear()
    sock = _Socket()
    asked: List[str] = []
    said: List[str] = []           # every line printed, with the questions, in order
    minted: List[str] = []
    windows: List[Dict[str, Any]] = []
    monkeypatch.setattr(packs_cmd, "_socket", lambda *a, **kw: sock)
    monkeypatch.setattr(packs_cmd, "_apps_granted", lambda pack_id: 0)

    def mint(**kw):
        minted.append(f"token-{len(minted) + 1}")
        return minted[-1]

    monkeypatch.setattr(uds, "write_owner_confirmation", mint)

    def window(**kw):
        windows.append(kw)
        return True

    monkeypatch.setattr("topos.cli.terminal_window.open_terminal_window", window)
    echo = click.echo
    monkeypatch.setattr(click, "echo", lambda message=None, **kw: (said.append(str(message)), echo(message, **kw))[1])

    def run(args, *, tty=True, answer=True, during=None):
        def confirm(text, **kwargs):
            asked.append(text)
            said.append(f"? {text}")
            if during is not None:
                during()
            return answer

        monkeypatch.setattr(packs_cmd, "sys", SimpleNamespace(stdin=_Stream(tty), stdout=_Stream(tty), exit=sys.exit))
        monkeypatch.setattr(click, "confirm", confirm)
        return CliRunner().invoke(packs_cmd.packs_group, args)

    def write(data, name="my-pack.yaml"):
        path = tmp_path / name
        path.write_text(data if isinstance(data, str) else yaml.safe_dump(data))
        return str(path)

    def files():
        return sorted(p.name for p in home.iterdir() if not p.name.startswith(".")) if home.exists() else []

    return SimpleNamespace(run=run, write=write, home=home, asked=asked, said=said, minted=minted, windows=windows,
                           sock=sock, files=files, monkeypatch=monkeypatch, tmp_path=tmp_path)


def _doc(result) -> Dict[str, Any]:
    """stdout is ONE JSON document: what a program reads."""
    return json.loads(result.stdout)


# ----------------------------------------------------------------------------- A. --check --json
def test_check_json_says_what_the_file_declares_and_what_adding_it_would_do(cli):
    result = cli.run(["add", "--check", "--json", str(EXAMPLE)], tty=False)
    assert result.exit_code == 0, result.stdout + result.stderr
    doc = _doc(result)
    assert set(doc) == {"ok", "added", "pack_id", "version", "title", "sensitivity", "role_policy", "reads",
                        "predicates", "describe", "install", "apps_with_grant", "examples", "routing",
                        "local_model", "sha256"}
    assert (doc["ok"], doc["added"], doc["pack_id"], doc["version"], doc["title"]) == (
        True, False, PACK, "1.0.0", "My gardening")
    assert (doc["sensitivity"], doc["role_policy"], doc["reads"]) == ("personal", "authored_only",
                                                                     "only what you wrote")
    assert doc["predicates"] == [{"name": "custom.plant", "type": "structured"}]
    assert doc["describe"] == custom_packs.describe(custom_packs.read_candidate(EXAMPLE)[0])
    assert doc["install"] == {"would": "new", "replaces": None, "reason": None}
    assert doc["apps_with_grant"] == 0
    assert doc["examples"] == {"gold": 1, "negative_controls": 1, "gold_with_expected_fact": 1, "try_limit": 20}
    assert doc["local_model"] == {"node_running": True, "available": "yes", "model": "local-9b"}
    assert doc["sha256"] == hashlib.sha256(EXAMPLE.read_bytes()).hexdigest()
    assert cli.asked == [] and cli.files() == [] and cli.minted == []
    assert cli.sock.sent() == [("GET", packs_cmd.TRY_PATH)], "one read of the node, nothing else"
    assert PLANTED not in result.stdout


def test_the_routing_words_are_the_prefilters_own_lexicon(cli):
    """The words that send a record to the pack, as the engine decides it: not a second implementation."""
    doc = _doc(cli.run(["add", "--check", "--json", str(EXAMPLE)], tty=False))
    prefilter = PackPrefilter(custom_packs.read_candidate(EXAMPLE)[0])
    assert doc["routing"]["words"] == sorted(prefilter.lexicon) and doc["routing"]["words"]
    assert doc["routing"]["phrases"] == prefilter.exemplars == ["i grow tomatoes", "i planted basil"]
    # an everyday word in a gold text is a routing word: the reason an agent is shown the list
    assert {"tomatoes", "garden", "grow"} <= set(doc["routing"]["words"])
    # a record reaches the pack on one of them, and not without
    assert prefilter.passes("We sat in the garden all afternoon") and not prefilter.passes("zzqx vvkp wwrt")
    everyday = definition(eval={"gold": [{"text": "Every morning I water the basil today."}],
                                "negative_controls": [{"text": "A film."}]})
    words = _doc(cli.run(["add", "--check", "--json", cli.write(everyday)], tty=False))["routing"]["words"]
    assert {"every", "morning", "today"} <= set(words)


def test_check_in_text_says_how_many_words_route_a_record_and_lists_none(cli):
    planted = definition(routing={"descriptors": [f"Plants the owner grows {PLANTED}"]})
    result = cli.run(["add", "--check", cli.write(planted)], tty=False)
    assert result.exit_code == 0, result.stdout + result.stderr
    words = len(PackPrefilter(custom_packs.read_candidate(Path(cli.write(planted)))[0]).lexicon)
    assert f"reads a record when it holds one of {words} words" in result.stdout
    assert "`topos-node packs add --check --json FILE` lists them" in result.stdout
    assert "This file is a pack Topos can add. Nothing was added." in result.stdout
    assert PLANTED not in result.stdout and cli.sock.calls == [], "text mode asks the node nothing"


def test_check_json_for_a_file_that_is_not_a_pack_is_one_sentence_and_a_non_zero_exit(cli):
    for data, sentence in ((definition(net_subject="allow"), "A pack you add can only describe you. Remove "
                                                               "`net_subject: allow`."),
                           (f"pack: [unclosed {PLANTED}\n", "The file is not valid YAML (line ")):
        result = cli.run(["add", "--check", "--json", cli.write(data)], tty=False)
        assert result.exit_code == packs_cmd.EXIT_FAILED
        doc = _doc(result)
        assert set(doc) == {"ok", "error"} and doc["ok"] is False and doc["error"].startswith(sentence)
        assert PLANTED not in result.stdout
    assert cli.sock.calls == [] and cli.files() == []


def test_check_json_says_new_replace_unchanged_or_why_this_node_would_refuse(cli):
    assert cli.run(["add", cli.write(definition())]).exit_code == 0
    cli.sock.calls.clear()

    def would(data):
        return _doc(cli.run(["add", "--check", "--json", cli.write(data)], tty=False))["install"]

    assert would(definition()) == {"would": "unchanged", "replaces": "1.0.0", "reason": None}
    assert would(definition(title="Renamed", version="1.1.0")) == {"would": "replace", "replaces": "1.0.0",
                                                                   "reason": None}
    refused = would(definition(title="Renamed"))
    assert refused["would"] == "refused" and "Give the changed pack a new `version`" in refused["reason"]
    other = definition(pack="custom.orchard")                # another pack that declares the same predicate
    assert "The predicate custom.plant is already declared by custom.gardening." == would(other)["reason"]


def test_check_json_says_unknown_for_the_model_when_the_node_is_not_running(cli):
    cli.sock.answers[("GET", packs_cmd.TRY_PATH)] = NodeDown("down")
    doc = _doc(cli.run(["add", "--check", "--json", str(EXAMPLE)], tty=False))
    assert doc["ok"] is True and doc["local_model"] == {"node_running": False, "available": "unknown", "model": None}
    cli.sock.answers[("GET", packs_cmd.TRY_PATH)] = NodeRefused(404, "http_404")       # an older node
    doc = _doc(cli.run(["add", "--check", "--json", str(EXAMPLE)], tty=False))
    assert doc["local_model"] == {"node_running": True, "available": "unknown", "model": None}


# ----------------------------------------------------------------------------- B. try
def test_try_sends_the_files_text_once_with_no_confirmation_and_reports_each_example(cli):
    path = cli.write(definition())
    result = cli.run(["try", "--json", path], tty=False)
    assert result.exit_code == packs_cmd.EXIT_OK, result.stdout + result.stderr
    doc = _doc(result)
    assert (doc["ok"], doc["status"], doc["pack_id"], doc["model"], doc["fact_check"]) == (
        True, "passed", PACK, "local-9b", "not_run")
    gold, negative = doc["examples"]
    assert set(gold) == {"kind", "index", "text", "role", "routed", "expected", "returned", "rejected", "seconds",
                         "outcome", "reason", "missing", "extra"}
    assert (gold["kind"], gold["outcome"], gold["text"]) == ("gold", "pass", "I grow tomatoes in my garden.")
    assert gold["expected"] == [{"predicate": "custom.plant", "value": {"name": "tomatoes"}}]
    assert gold["returned"][0]["value"] == {"name": "tomatoes"}
    assert (negative["kind"], negative["outcome"], negative["returned"]) == ("negative_control", "pass", [])
    # the totals are counted from the rows that were printed, never copied from the body
    assert doc["totals"] == {"examples": 2, "passed": 2, "failed": 0, "unjudged": 0, "errors": 0, "skipped": 0,
                             "gold": {"examples": 1, "passed": 1, "failed": 0},
                             "negative_controls": {"examples": 1, "passed": 1, "failed": 0}}
    assert PLANTED not in result.stdout
    assert cli.sock.sent() == [("POST", packs_cmd.TRY_PATH)]
    sent = cli.sock.calls[0]
    assert sent["json"] == {"content": open(path).read()} and sent["headers"] is None
    assert cli.asked == [] and cli.minted == [] and cli.files() == []


def test_try_says_which_examples_failed_and_exits_with_its_own_code(cli):
    cli.sock.answers[("POST", packs_cmd.TRY_PATH)] = _report("fail", "pass")
    as_json = cli.run(["try", "--json", cli.write(definition())], tty=False)
    assert as_json.exit_code == packs_cmd.EXIT_EXAMPLES_FAILED == 3
    assert _doc(as_json)["status"] == "failed" and _doc(as_json)["totals"]["failed"] == 1
    text = cli.run(["try", cli.write(definition())], tty=False)
    assert text.exit_code == packs_cmd.EXIT_EXAMPLES_FAILED
    assert "gold 1: FAIL (expected_fact_missing)" in text.stdout and "negative control 1: pass" in text.stdout
    assert 'expected custom.plant {"name": "tomatoes"}' in text.stdout and "returned nothing" in text.stdout
    assert "2 examples: 1 passed, 1 failed." in text.stdout
    assert "none of your records" in text.stdout and "was not run" in text.stdout
    assert PLANTED not in text.stdout


def test_try_has_distinct_codes_for_a_node_that_is_down_and_a_node_with_no_model(cli):
    path = cli.write(definition())
    cli.sock.answers[("POST", packs_cmd.TRY_PATH)] = NodeDown("down")
    down = cli.run(["try", "--json", path], tty=False)
    assert down.exit_code == packs_cmd.EXIT_NODE_DOWN == 4
    assert _doc(down) == {"ok": False, "code": "node_down", "error": "Topos isn't running — say \"start Topos\"."}

    cli.sock.answers[("POST", packs_cmd.TRY_PATH)] = NodeRefused(409, "no_local_model")
    none = cli.run(["try", "--json", path], tty=False)
    assert none.exit_code == packs_cmd.EXIT_NO_MODEL == 5
    assert _doc(none) == {"ok": False, "code": "no_local_model",
                          "error": "This Topos has no local model picked for facts, so there is nothing to try "
                                   "the pack on."}
    text = cli.run(["try", path], tty=False)
    assert text.exit_code == packs_cmd.EXIT_NO_MODEL and len(text.stderr.strip().splitlines()) == 1

    cli.sock.answers[("POST", packs_cmd.TRY_PATH)] = _report("error", "error", "skipped", stopped="model_unavailable")
    silent = cli.run(["try", "--json", path], tty=False)
    assert silent.exit_code == packs_cmd.EXIT_NO_MODEL and _doc(silent)["status"] == "model_unavailable"

    cli.sock.answers[("POST", packs_cmd.TRY_PATH)] = NodeRefused(409, "trial_already_running")
    busy = cli.run(["try", "--json", path], tty=False)
    assert busy.exit_code == packs_cmd.EXIT_FAILED and _doc(busy)["code"] == "trial_already_running"

    # A node from before this command refuses the route at its socket; the person is not told to approve anything.
    cli.sock.answers[("POST", packs_cmd.TRY_PATH)] = NodeRefused(403, "owner_confirmation_required")
    older = cli.run(["try", "--json", path], tty=False)
    assert older.exit_code == packs_cmd.EXIT_FAILED and _doc(older)["code"] == "older_node"
    assert "older than this command" in _doc(older)["error"] and cli.minted == []


def test_try_refuses_a_file_that_is_not_a_pack_or_holds_too_many_examples_without_asking_the_node(cli):
    bad = cli.run(["try", "--json", cli.write(definition(net_subject="allow"))], tty=False)
    assert bad.exit_code == packs_cmd.EXIT_FAILED
    assert _doc(bad) == {"ok": False, "error": "A pack you add can only describe you. Remove `net_subject: allow`."}
    many = [{"text": f"I grow plant number {n}."} for n in range(20)]
    over = cli.run(["try", "--json", cli.write(definition(eval={"gold": many,
                                                               "negative_controls": [{"text": "A film."}]}))],
                   tty=False)
    assert over.exit_code == packs_cmd.EXIT_FAILED
    assert _doc(over) == {"ok": False, "code": "too_many_examples",
                          "error": "The file holds 21 examples and one try runs at most 20. Try it with fewer."}
    assert cli.sock.calls == []


# ----------------------------------------------------------------------------- C. on and off
def _added(cli) -> str:
    """The digest of the pack once it is in the node's directory: what `packs on` shows and sends."""
    custom_packs.install_content(yaml.safe_dump(definition()).encode())
    return definition_sha(load_packs(bundled_pack_dir(), only=[PACK])[PACK], stamped=False)


def test_on_without_a_terminal_asks_no_one_and_changes_nothing(cli):
    _added(cli)
    result = cli.run(["on", PACK, "--json"], tty=False)
    assert result.exit_code == packs_cmd.EXIT_NEEDS_HUMAN
    doc = _doc(result)
    assert (doc["ok"], doc["status"], doc["pack_id"], doc["enabled"]) == (True, "needs_human", PACK, False)
    assert doc["command"] == f"topos-node packs on {PACK}"
    assert "run this command in your own terminal" in doc["human"]["say"]
    assert cli.asked == [] and cli.minted == [] and cli.windows == []
    assert cli.sock.sent(*MUTATING) == [] and cli.sock.enabled[PACK] is False
    text = cli.run(["on", PACK], tty=False)
    assert text.exit_code == packs_cmd.EXIT_NEEDS_HUMAN and "run this command in your own terminal" in text.stderr


def test_on_with_open_terminal_opens_the_same_question_and_waits(cli):
    _added(cli)
    result = cli.run(["on", PACK, "--open-terminal", "--json"], tty=False)
    assert result.exit_code == packs_cmd.EXIT_NEEDS_HUMAN
    assert _doc(result)["status"] == "approval_pending"
    assert [w["argv"] for w in cli.windows] == [["packs", "on", PACK]], "the window runs the asking command"
    assert cli.asked == [] and cli.minted == [] and cli.sock.sent(*MUTATING) == []


def test_on_shows_the_pack_asks_once_and_sends_one_switch_with_the_definition_that_was_shown(cli):
    shown = _added(cli)
    result = cli.run(["on", PACK])
    assert result.exit_code == packs_cmd.EXIT_OK, result.stdout + result.stderr
    assert cli.asked == ["Switch it on?"]
    question = cli.said.index("? Switch it on?")
    before = "\n".join(cli.said[:question])
    assert f"{PACK}  1.0.0  My gardening" in before and "reads: only what you wrote" in before
    assert "writes 1 kind of fact, about you only: custom.plant" in before
    assert "from now on Topos reads your new records for this pack and writes the facts it finds" in before
    assert cli.sock.sent(*MUTATING) == [("PUT", SWITCH)]
    put = [c for c in cli.sock.calls if c["method"] == "PUT"][0]
    assert put["json"] == {"enabled": True, "definition": shown}
    assert put["headers"] == {"X-Topos-Owner-Confirm": "token-1"} and cli.minted == ["token-1"]
    assert f"{PACK} is switched on." in result.stdout and PLANTED not in result.stdout

    again = cli.run(["on", PACK, "--json"])
    assert again.exit_code == 0 and _doc(again)["status"] == "already_on" and cli.asked == ["Switch it on?"]


def test_a_no_to_on_sends_nothing(cli):
    _added(cli)
    result = cli.run(["on", PACK, "--json"], answer=False)
    assert result.exit_code == packs_cmd.EXIT_OK and _doc(result)["status"] == "cancelled"
    assert cli.asked == ["Switch it on?"] and cli.minted == [] and cli.sock.sent(*MUTATING) == []


def test_on_refuses_a_pack_that_writes_about_other_people_before_anything_is_asked_or_sent(cli):
    assert str(load_packs(bundled_pack_dir(), only=[OUTWARD])[OUTWARD].net_subject) == "allow"
    result = cli.run(["on", OUTWARD, "--json"])
    assert result.exit_code == packs_cmd.EXIT_FAILED
    doc = _doc(result)
    assert (doc["ok"], doc["code"]) == (False, "consent_in_app") and "in the app" in doc["error"]
    assert cli.asked == [] and cli.minted == [] and cli.sock.calls == []
    unknown = cli.run(["on", "custom.nothing_here", "--json"])
    assert unknown.exit_code == packs_cmd.EXIT_FAILED and _doc(unknown)["code"] == "unknown_pack"
    assert cli.sock.calls == []


def test_on_for_a_pack_topos_ships_sends_no_definition_and_a_refusal_is_a_sentence(cli):
    result = cli.run(["on", SHIPPED])
    assert result.exit_code == packs_cmd.EXIT_OK, result.stdout + result.stderr
    assert [c["json"] for c in cli.sock.calls if c["method"] == "PUT"] == [{"enabled": True}]

    _added(cli)
    cli.sock.answers[("PUT", SWITCH)] = NodeRefused(409, "pack_changed")
    refused = cli.run(["on", PACK, "--json"])
    assert refused.exit_code == packs_cmd.EXIT_FAILED
    assert _doc(refused) == {"ok": False, "pack_id": PACK, "code": "pack_changed",
                             "error": "The pack changed after it was approved, so it was not switched on. Run the "
                                      "command again to see what it is now."}


def test_off_asks_once_and_sends_one_switch_and_without_a_terminal_changes_nothing(cli):
    cli.sock.enabled[SHIPPED] = True
    piped = cli.run(["off", SHIPPED, "--json"], tty=False)
    assert piped.exit_code == packs_cmd.EXIT_NEEDS_HUMAN and _doc(piped)["status"] == "needs_human"
    assert cli.asked == [] and cli.minted == [] and cli.sock.sent(*MUTATING) == []

    result = cli.run(["off", SHIPPED])
    assert result.exit_code == packs_cmd.EXIT_OK, result.stdout + result.stderr
    assert cli.asked == ["Switch it off?"]
    assert "Topos stops reading records for it" in result.stdout and "7 facts it holds are kept" in result.stdout
    assert "no app reads them while the pack is off" in result.stdout
    put = [c for c in cli.sock.calls if c["method"] == "PUT"]
    assert [(c["path"], c["json"], c["headers"]) for c in put] == [
        (f"{packs_cmd.PACKS_PATH}/{SHIPPED}/switch", {"enabled": False}, {"X-Topos-Owner-Confirm": "token-1"})]
    assert f"{SHIPPED} is switched off." in result.stdout

    already = cli.run(["off", SHIPPED, "--json"])
    assert already.exit_code == 0 and _doc(already)["status"] == "already_off" and len(cli.minted) == 1


# ----------------------------------------------------------------------------- D. one approval
def _since(days: int) -> str:
    return (datetime.datetime.now(datetime.timezone.utc).date() - datetime.timedelta(days=days)).isoformat()


def test_add_on_backfill_names_all_three_effects_before_the_one_question(cli):
    result = cli.run(["add", cli.write(definition()), "--on", "--backfill", "30d"], answer=False)
    assert result.exit_code == packs_cmd.EXIT_OK, result.stdout + result.stderr
    assert cli.asked == ["Add it, switch it on and read that history?"], "one question"
    question = cli.said.index("? Add it, switch it on and read that history?")
    before = " ".join(" ".join(cli.said[:question]).split())
    assert f"{PACK} 1.0.0 My gardening" in before and "reads: only what you wrote" in before
    assert "writes 1 kind of fact, about you only: custom.plant" in before
    assert "One yes here does all of this, in this order:" in before
    assert "1. The pack is added: this file is copied into this Topos." in before
    assert "2. It is switched on now." in before and "writes the facts it finds" in before
    assert "3. History is read for it, in one run that starts now" in before
    assert f"It reads the last 30 days (records dated {_since(30)} or later)" in before
    assert "within the newest 20,000 journal entries and the newest 20,000 messages" in before
    assert "is one call to the local model local-9b (about 22.8 s each, by this node's 30-day average)" in before
    assert "every fact found is then checked by local-27b, one call each" in before
    assert "How many records that is cannot be counted before the pack is added" in before
    assert PLANTED not in before

    # ...and a no does nothing at all
    assert "Nothing added." in result.stdout
    assert cli.files() == [] and cli.minted == [] and cli.sock.sent(*MUTATING) == []


def test_a_yes_adds_switches_on_and_starts_the_run_and_reports_three_receipts(cli):
    path = cli.write(definition())
    shown = open(path, "rb").read()
    digest = definition_sha(custom_packs.read_candidate(Path(path))[0], stamped=False)
    result = cli.run(["add", path, "--on", "--backfill", "30d", "--json"])
    assert result.exit_code == packs_cmd.EXIT_OK, result.stdout + result.stderr
    doc = _doc(result)
    assert doc == {
        "ok": True, "status": "done", "pack_id": PACK, "version": "1.0.0",
        "sha256": hashlib.sha256(shown).hexdigest(),
        "receipts": [{"stage": "added", "ok": True, "replaced": None, "unchanged": False},
                     {"stage": "switched_on", "ok": True},
                     {"stage": "backfill_started", "ok": True, "run_id": "bfr_1", "since": _since(30),
                      "waiting": 412}],
        "state": {"added": True, "enabled": True, "backfill_run": "bfr_1"}}
    assert cli.asked == ["Add it, switch it on and read that history?"]
    assert (cli.home / f"{PACK}.yaml").read_bytes() == shown
    changes = [c for c in cli.sock.calls if c["method"] in MUTATING]
    assert [(c["method"], c["path"]) for c in changes] == [("PUT", SWITCH), ("POST", packs_cmd.RUNS_PATH)]
    assert changes[0]["json"] == {"enabled": True, "definition": digest}
    assert changes[1]["json"] == {"pack_ids": [PACK], "limit": 200, "until_done": True, "since": _since(30)}
    # one yes, two changes, each with a confirmation of its own, minted only after the yes
    assert [c["headers"] for c in changes] == [{"X-Topos-Owner-Confirm": "token-1"},
                                               {"X-Topos-Owner-Confirm": "token-2"}]
    assert cli.minted == ["token-1", "token-2"]
    assert PLANTED not in result.stdout + result.stderr


def test_add_on_alone_reads_no_history_and_a_plain_add_is_as_before(cli):
    on = cli.run(["add", cli.write(definition()), "--on"])
    assert on.exit_code == packs_cmd.EXIT_OK, on.stdout + on.stderr
    assert cli.asked == ["Add it and switch it on?"]
    assert "No history is read." in on.stdout and "It is switched on." in on.stdout
    assert cli.sock.sent(*MUTATING) == [("PUT", SWITCH)] and cli.minted == ["token-1"]

    cli.sock.calls.clear()
    plain = cli.run(["add", cli.write(definition(pack="custom.orchard",
                                                 predicates=[{"name": "custom.tree", "value_type": "string",
                                                              "cardinality": "multi", "temporal": "interval",
                                                              "altitude": "stated"}])), "--json"])
    assert plain.exit_code == packs_cmd.EXIT_OK, plain.stdout + plain.stderr
    assert cli.asked[-1] == "Add it?"
    assert _doc(plain)["receipts"] == [{"stage": "added", "ok": True, "replaced": None, "unchanged": False}]
    assert _doc(plain)["state"] == {"added": True, "enabled": False, "backfill_run": None}
    assert cli.sock.calls == [], "without --on nothing is switched on and the node is not asked anything"
    assert cli.minted == ["token-1"]


def test_a_switch_that_fails_leaves_the_pack_added_and_off_and_says_so(cli):
    cli.sock.answers[("PUT", SWITCH)] = NodeRefused(409, "pack_changed")
    result = cli.run(["add", cli.write(definition()), "--on", "--backfill", "30d", "--json"])
    assert result.exit_code == packs_cmd.EXIT_FAILED
    doc = _doc(result)
    assert (doc["ok"], doc["status"], doc["failed_stage"], doc["code"]) == (False, "failed", "switched_on",
                                                                          "pack_changed")
    assert doc["receipts"] == [{"stage": "added", "ok": True, "replaced": None, "unchanged": False},
                               {"stage": "switched_on", "ok": False, "code": "pack_changed", "error": doc["error"]}]
    assert doc["state"] == {"added": True, "enabled": False, "backfill_run": None}
    assert cli.files() == [f"{PACK}.yaml"] and cli.sock.enabled[PACK] is False
    assert ("POST", packs_cmd.RUNS_PATH) not in cli.sock.sent(), "no history is read for a pack that is off"

    cli.sock.answers[("PUT", SWITCH)] = NodeDown("down")
    text = cli.run(["add", cli.write(definition(version="1.1.0", title="Renamed")), "--on"])
    assert text.exit_code == packs_cmd.EXIT_NODE_DOWN
    assert f"Added {PACK} 1.1.0 (it replaces 1.0.0)." in text.stdout
    assert f"The pack is added and switched off. `topos-node packs on {PACK}` switches it on." in text.stderr


def test_a_run_that_does_not_start_leaves_the_pack_added_and_on_and_says_so(cli):
    cli.sock.answers[("POST", packs_cmd.RUNS_PATH)] = NodeRefused(409, "backfill_already_running")
    result = cli.run(["add", cli.write(definition()), "--on", "--backfill", "all", "--json"])
    assert result.exit_code == packs_cmd.EXIT_FAILED
    doc = _doc(result)
    assert (doc["failed_stage"], doc["code"]) == ("backfill_started", "backfill_already_running")
    assert [(r["stage"], r["ok"]) for r in doc["receipts"]] == [("added", True), ("switched_on", True),
                                                               ("backfill_started", False)]
    assert doc["state"] == {"added": True, "enabled": True, "backfill_run": None}
    text = cli.run(["add", cli.write(definition()), "--on", "--backfill", "all"])
    assert "The pack is added and switched on, and no history was read." in text.stderr
    assert f"`topos-node packs backfill --pack {PACK} --until-done` reads it." in text.stderr


def test_an_owners_run_that_is_working_is_said_before_anyone_is_asked(cli):
    cli.sock.active = {"run_id": "bfr_0", "state": "running", "origin": "owner", "packs": [], "totals": {}}
    result = cli.run(["add", cli.write(definition()), "--on", "--backfill", "30d"])
    assert result.exit_code == packs_cmd.EXIT_FAILED and "A backfill run is working now" in result.stderr
    assert cli.asked == [] and cli.files() == [] and cli.minted == []


def test_without_a_terminal_the_staged_add_writes_nothing_and_open_terminal_binds_the_window_to_the_bytes(cli):
    path = cli.write(definition())
    digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
    piped = cli.run(["add", path, "--on", "--backfill", "30d", "--json"], tty=False)
    assert piped.exit_code == packs_cmd.EXIT_NEEDS_HUMAN
    doc = _doc(piped)
    assert (doc["ok"], doc["status"], doc["added"], doc["sha256"]) == (True, "needs_human", False, digest)
    argv = ["packs", "add", str(Path(path).resolve()), "--expected-sha256", digest, "--on", "--backfill", "30d"]
    assert doc["command"] == "topos-node " + " ".join(argv)
    assert cli.windows == [] and cli.asked == [] and cli.files() == [] and cli.minted == []
    assert cli.sock.sent(*MUTATING) == []

    opened = cli.run(["add", path, "--on", "--backfill", "30d", "--open-terminal", "--json"], tty=False)
    assert opened.exit_code == packs_cmd.EXIT_NEEDS_HUMAN and _doc(opened)["status"] == "approval_pending"
    assert [w["argv"] for w in cli.windows] == [argv]
    assert cli.asked == [] and cli.files() == [] and cli.minted == [] and cli.sock.sent(*MUTATING) == []

    # the window's command, run on the person's terminal: the same bytes pass, changed bytes do not
    open(path, "w").write(yaml.safe_dump(definition(role_policy="any_with_label", title="Swapped")))
    changed = cli.run(argv[1:])
    assert changed.exit_code == packs_cmd.EXIT_FAILED
    assert "Not added. The file changed after it was checked." in changed.stderr
    assert cli.asked == [] and cli.files() == [] and cli.sock.sent(*MUTATING) == []
    open(path, "w").write(yaml.safe_dump(definition()))
    same = cli.run(argv[1:])
    assert same.exit_code == packs_cmd.EXIT_OK and cli.files() == [f"{PACK}.yaml"]
    assert cli.sock.sent(*MUTATING) == [("PUT", SWITCH), ("POST", packs_cmd.RUNS_PATH)]


def test_without_a_terminal_a_node_that_is_down_is_said_before_a_window_is_opened(cli):
    cli.sock.answers[("GET", packs_cmd.PACKS_PATH)] = NodeDown("down")
    result = cli.run(["add", cli.write(definition()), "--on", "--open-terminal", "--json"], tty=False)
    assert result.exit_code == packs_cmd.EXIT_NODE_DOWN and _doc(result)["code"] == "node_down"
    assert cli.windows == [] and cli.files() == []


def test_a_file_changed_while_the_question_is_open_is_not_what_is_added_or_switched_on(cli):
    path = cli.write(definition())
    shown = open(path, "rb").read()
    digest = definition_sha(custom_packs.read_candidate(Path(path))[0], stamped=False)

    def swap():
        open(path, "w").write(yaml.safe_dump(definition(role_policy="any_with_label", sensitivity_class="special")))

    result = cli.run(["add", path, "--on"], during=swap)
    assert result.exit_code == packs_cmd.EXIT_OK, result.stdout + result.stderr
    assert open(path, "rb").read() != shown and (cli.home / f"{PACK}.yaml").read_bytes() == shown
    assert [c["json"] for c in cli.sock.calls if c["method"] == "PUT"] == [{"enabled": True, "definition": digest}]


def test_the_flags_that_make_no_sense_together_are_refused(cli):
    path = cli.write(definition())
    alone = cli.run(["add", path, "--backfill", "30d"])
    assert alone.exit_code == 2 and "pass --on with it" in alone.stderr
    check = cli.run(["add", "--check", path, "--on"])
    assert check.exit_code == 2 and "--check adds nothing" in check.stderr
    window = cli.run(["add", path, "--on", "--backfill", "soon"])
    assert window.exit_code == 2 and "is not a window" in window.stderr
    assert cli.asked == [] and cli.files() == [] and cli.sock.calls == []


def test_no_command_here_takes_an_option_that_answers_for_the_person():
    for name in ("add", "on", "off", "try"):
        command = packs_cmd.packs_group.commands[name]
        flags = {opt for p in command.params for opt in getattr(p, "opts", [])}
        assert not flags & {"--yes", "-y", "--force", "--assume-yes", "--approved", "--no-input"}, name


def test_every_path_the_new_commands_send_is_a_route_the_node_serves_with_the_verdict_the_gate_reads():
    from starlette.routing import Match

    from topos import owner_door_routes as census
    from topos.app import app

    def served(method: str, path: str) -> bool:
        scope = {"type": "http", "method": method, "path": path, "root_path": "", "query_string": b"", "headers": []}
        return any(route.matches(scope)[0] == Match.FULL for route in app.routes)

    for method, path, verdict in (("GET", packs_cmd.TRY_PATH, census.READ), ("POST", packs_cmd.TRY_PATH, census.OPEN),
                                  ("PUT", packs_cmd._switch_path(PACK), census.DANGEROUS)):
        assert served(method, path), f"the CLI sends {method} {path} and the node does not serve it"
        assert census.lookup(method, path).verdict == verdict


# ----------------------------------------------------------------------------- E. over a real owner socket
@pytest.fixture
def real(tmp_path, monkeypatch):
    """The real routes behind the real socket wrapper on a real unix socket, over a scratch database. A batch of
    a run goes to a scripted runner: no model is loaded or called."""
    from fastapi import FastAPI

    from tests.topos.fake_node_socket import serve_fake_node
    from tests.topos.test_pack_backfill_runs import _Script
    from topos.api import signal
    from topos.storage.db.migrations import apply_all_migrations

    home = tmp_path / "node" / "fact-packs"
    monkeypatch.setattr(custom_packs, "custom_pack_dir", lambda: home)
    custom_packs._SAID.clear()
    conn = sqlite3.connect(tmp_path / "node.db", check_same_thread=False)
    apply_all_migrations(conn)
    conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, is_self)"
                 " VALUES ('ent_owner','person','Owner','owner','[]',1)")
    conn.execute("CREATE TABLE IF NOT EXISTS engine_config (key TEXT PRIMARY KEY, value TEXT)")
    seed_pack_registry(conn, bundled_pack_dir())
    conn.execute("UPDATE pack_registry SET enabled=0")
    conn.commit()
    monkeypatch.setenv("TOPOS_OWNER_DOOR_SPLIT", "1")
    for name in ("TOPOS_OWNER_DOOR_TEAM_IDS", "TOPOS_UDS_TEAM_IDS", "TOPOS_DEV_PROXY_ALLOWED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(signal, "_entities_conn", lambda: conn)
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model", lambda s, c=None: "stub-model")
    monkeypatch.setenv("TOPOS_DERIVATION_VERIFIER_MODEL", "stub-verifier")
    monkeypatch.setattr(packs_cmd, "_apps_granted", lambda pack_id: 0)
    script = _Script({PACK: 3})
    monkeypatch.setattr(runs, "_run_batch", script)
    app = FastAPI()
    app.include_router(signal.router, prefix="/v1")
    uds.clear_owner_confirmation()
    asked: List[str] = []

    def reset():
        run = runs._active
        if run is not None:
            run.request_stop()
            if run.thread is not None:
                run.thread.join(timeout=10)
        runs._active = None
        runs._last = None
        runs._single_batches = 0

    def run(args, *, tty=True, during=None):
        def confirm(text, **kw):
            asked.append(text)
            if during is not None:
                during()
            return True

        stream = SimpleNamespace(isatty=lambda: tty)
        monkeypatch.setattr(packs_cmd, "sys", SimpleNamespace(stdin=stream, stdout=stream, exit=sys.exit))
        monkeypatch.setattr(click, "confirm", confirm)
        return CliRunner().invoke(packs_cmd.packs_group, args, catch_exceptions=False)

    def write(data, name="my-pack.yaml"):
        path = tmp_path / name
        path.write_text(yaml.safe_dump(data))
        return str(path)

    reset()
    with serve_fake_node(SimpleNamespace(app=uds.UDSChannelApp(app)), Path(os.environ["TOPOS_UDS_PATH"])):
        yield SimpleNamespace(run=run, write=write, conn=conn, home=home, script=script, asked=asked)
    reset()
    conn.close()


def _row(conn: sqlite3.Connection) -> Optional[tuple]:
    row = conn.execute("SELECT enabled, origin, definition_sha, consented_at FROM pack_registry WHERE pack_id=?",
                       (PACK,)).fetchone()
    return tuple(row) if row else None


def test_the_whole_flow_over_a_real_owner_socket(real) -> None:
    """What a coding agent's shell can do, then the owner's one yes: real client, real confirmation file, real
    gate, real switch and run routes. Only the person and the batch of a run are stand-ins."""
    from topos.mcp_server import NodeSocket

    path = real.write(definition())
    # A shell with no terminal: the switch route refuses it, and the command asks no one and writes nothing.
    with pytest.raises(NodeRefused) as refused:
        NodeSocket().request_sync("PUT", SWITCH, json_body={"enabled": True, "definition": "0" * 64})
    assert (refused.value.status, refused.value.code) == (403, "owner_confirmation_required")
    piped = real.run(["add", path, "--on", "--backfill", "30d", "--json"], tty=False)
    assert piped.exit_code == packs_cmd.EXIT_NEEDS_HUMAN and _doc(piped)["status"] == "needs_human"
    assert real.asked == [] and not real.home.exists() and _row(real.conn) is None
    assert not uds.owner_confirmation_path().exists()

    # The owner, on a terminal: one question.
    done = real.run(["add", path, "--on", "--backfill", "30d", "--json"])
    assert done.exit_code == packs_cmd.EXIT_OK, done.stdout + done.stderr
    doc = _doc(done)
    assert real.asked == ["Add it, switch it on and read that history?"]
    assert [(r["stage"], r["ok"]) for r in doc["receipts"]] == [("added", True), ("switched_on", True),
                                                               ("backfill_started", True)]
    assert doc["state"]["added"] and doc["state"]["enabled"] and doc["state"]["backfill_run"].startswith("bfr_")
    assert doc["receipts"][2]["waiting"] == 0, "counted by the node's own plan: no record on this scratch node"
    assert (real.home / f"{PACK}.yaml").read_bytes() == open(path, "rb").read()
    pack = load_packs(bundled_pack_dir(), only=[PACK])[PACK]
    assert _row(real.conn) == (1, "third_party", definition_sha(pack), None)
    assert runs_as_switched_on(real.conn, pack) and PACK in enabled_packs(real.conn, bundled_pack_dir())
    run = runs._active or runs._last
    assert run is not None and run.run_id == doc["state"]["backfill_run"]
    run.thread.join(timeout=20)
    snapshot = run.snapshot()
    assert snapshot["pack_ids"] == [PACK] and snapshot["until_done"] is True and snapshot["origin"] == "owner"
    assert snapshot["since"] == _since(30) and real.script.calls[0] == (PACK, 200)
    assert not uds.owner_confirmation_path().exists(), "both confirmations were spent, one per change"

    listed = real.run(["list", "--json"], tty=False)
    assert {p["pack_id"]: p["enabled"] for p in _doc(listed)["packs"]}[PACK] is True

    off = real.run(["off", PACK, "--json"])
    assert off.exit_code == packs_cmd.EXIT_OK and _doc(off)["status"] == "switched_off"
    assert _row(real.conn)[0] == 0 and PACK not in enabled_packs(real.conn, bundled_pack_dir())
    on = real.run(["on", PACK, "--json"])
    assert on.exit_code == packs_cmd.EXIT_OK and _doc(on)["status"] == "switched_on" and _row(real.conn)[0] == 1
    assert real.asked[1:] == ["Switch it off?", "Switch it on?"]


def test_a_pack_changed_after_the_yes_is_added_and_stays_off_over_a_real_socket(real, monkeypatch) -> None:
    """Between the yes and the switch the file in the node's directory is replaced by one that reads more. The
    node switches on only the definition that was shown, so the pack is added, off, and nothing is read."""
    install = custom_packs.install_content

    def install_then_swap(content):
        added = install(content)
        (real.home / f"{PACK}.yaml").write_text(yaml.safe_dump(
            definition(role_policy="any_with_label", sensitivity_class="special")))
        return added

    monkeypatch.setattr(custom_packs, "install_content", install_then_swap)
    result = real.run(["add", real.write(definition()), "--on", "--backfill", "30d", "--json"])
    assert result.exit_code == packs_cmd.EXIT_FAILED
    doc = _doc(result)
    assert (doc["failed_stage"], doc["code"]) == ("switched_on", "pack_changed")
    assert doc["state"] == {"added": True, "enabled": False, "backfill_run": None}
    assert _row(real.conn)[0] == 0 and PACK not in enabled_packs(real.conn, bundled_pack_dir())
    assert runs._active is None and runs._last is None and real.script.calls == [], "no history was read"


# ----------------------------------------------------------------------------- review round 1
def test_a_try_whose_gold_example_was_not_judged_is_not_a_pass(cli):
    # A gold example with no expected fact runs but is never judged. Counting it as a pass let a file with no
    # expectation at all end in exit 0, "passed": a pack that was never tested.
    cli.sock.answers[("POST", packs_cmd.TRY_PATH)] = _report("unjudged", "pass")
    result = cli.run(["try", "--json", cli.write(definition())], tty=False)
    assert result.exit_code == packs_cmd.EXIT_EXAMPLES_FAILED
    doc = _doc(result)
    assert (doc["status"], doc["totals"]["unjudged"], doc["totals"]["passed"]) == ("incomplete", 1, 1)


def test_a_try_the_node_never_answered_ends_in_a_sentence_not_a_traceback(cli):
    import httpx

    cli.sock.answers[("POST", packs_cmd.TRY_PATH)] = httpx.ReadTimeout("slow")
    result = cli.run(["try", "--json", cli.write(definition())], tty=False)
    assert result.exit_code == packs_cmd.EXIT_NO_MODEL
    assert (_doc(result)["ok"], _doc(result)["code"]) == (False, packs_cmd.NO_ANSWER)
    assert result.exception is None or isinstance(result.exception, SystemExit)


@pytest.mark.parametrize("error", ["read_timeout", "protocol"])
def test_a_switch_with_no_answer_after_the_yes_says_the_state_is_not_known(cli, error):
    # The request went out; the node may have switched the pack on. Saying "added and switched off" would be a
    # guess, and before this the command ended in a traceback with no receipts at all.
    import httpx

    exc = httpx.ReadTimeout("slow") if error == "read_timeout" else httpx.RemoteProtocolError("dropped")
    cli.sock.answers[("PUT", SWITCH)] = exc
    result = cli.run(["add", cli.write(definition()), "--on", "--backfill", "30d", "--json"])
    assert result.exit_code == packs_cmd.EXIT_FAILED, result.output
    doc = _doc(result)
    assert (doc["status"], doc["failed_stage"], doc["code"]) == ("failed", "switched_on", packs_cmd.NO_ANSWER)
    assert doc["state"] == {"added": True, "enabled": None, "backfill_run": None}
    assert ("POST", packs_cmd.RUNS_PATH) not in cli.sock.sent()
    text = cli.run(["add", cli.write(definition(version="1.1.0")), "--on"])
    assert "whether it is switched on is not known" in text.stderr
    assert "switched off" not in text.stderr


def test_a_run_start_with_no_answer_says_to_check_before_starting_another(cli):
    import httpx

    cli.sock.answers[("POST", packs_cmd.RUNS_PATH)] = httpx.ReadTimeout("slow")
    result = cli.run(["add", cli.write(definition()), "--on", "--backfill", "30d", "--json"])
    doc = _doc(result)
    assert (doc["failed_stage"], doc["code"]) == ("backfill_started", packs_cmd.NO_ANSWER)
    assert doc["state"] == {"added": True, "enabled": True, "backfill_run": None}
    text = cli.run(["add", cli.write(definition()), "--on", "--backfill", "30d"])
    assert "whether reading its history started is not known" in text.stderr
    assert "--until-done` reads it" not in text.stderr


def test_a_yes_that_cannot_be_recorded_sends_nothing(cli):
    def fail(**kw):
        raise PermissionError("read-only home")

    cli.monkeypatch.setattr(uds, "write_owner_confirmation", fail)
    result = cli.run(["add", cli.write(definition()), "--on", "--json"])
    assert result.exit_code == packs_cmd.EXIT_FAILED
    assert _doc(result)["code"] == "confirmation_not_written"
    assert cli.sock.sent("PUT") == []


def test_without_a_terminal_the_text_hand_off_prints_the_command_to_run(cli):
    _added(cli)
    text = cli.run(["on", PACK], tty=False)
    assert text.exit_code == packs_cmd.EXIT_NEEDS_HUMAN
    assert f"  topos-node packs on {PACK}\n" in text.stderr


def test_the_window_does_not_say_done_after_a_no(cli):
    _added(cli)
    cli.run(["on", PACK, "--open-terminal", "--json"], tty=False)
    assert cli.windows and not cli.windows[0]["done"].startswith("Done")


@pytest.mark.parametrize("expected, returned, same", [
    (5, 5.0, True), (5, "5", True), ("5", 5, True), (5, 6, False), (0, False, False), (False, 0, False),
    (False, False, True), (0, "", False), (0, None, False), ("Dune", " dune ", True), ("Dune", "Dunes", False),
    ({"distance_km": 5, "activity": "jogging"}, {"distance_km": 5.0, "activity": "Jogging", "minutes": 30}, True),
])
def test_a_try_compares_numbers_as_numbers_and_yes_or_no_as_itself(expected, returned, same):
    from topos.features.derivation import pack_trial

    assert pack_trial._same(expected, returned) is same
