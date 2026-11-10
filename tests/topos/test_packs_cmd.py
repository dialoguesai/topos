"""`topos-node packs list|backfill|status|stop`: what it asks, what it sends, what it prints.

protects: a backfill starts only after a person answers `[y/N]` on a terminal. With no terminal the command asks no
one, sends nothing and mints no confirmation; a no sends nothing; a yes mints exactly one confirmation and sends ONE
start carrying the plan that was shown (the packs, the batch size, one batch each or until done). The command then
follows the run by reading its status, prints each batch, and on Ctrl-C asks the node to stop and waits for it. It
prints a projection (pack ids, counts, state words), never a response body. Every path it sends is a route the node
serves, and the start is the one the owner socket's census names dangerous.

A terminal and a person are patched (``isatty`` on the module's ``sys``, ``click.confirm``); the owner socket is an
in-memory fake that records each request and plays a scripted run.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import click
import pytest
from click.testing import CliRunner

from topos import owner_door_routes as census
from topos.cli import packs_cmd
from topos.mcp_server import NodeDown, NodeRefused

pytestmark = pytest.mark.public

JOY = "moments.joy"
EVENTS = "life.events"
OFF = "values.motivation"
PLANTED = "tpn_" + "planted" + "0123456789abcdef" * 2

PACKS = {
    "packs": [
        {"pack_id": EVENTS, "title": "Life events", "enabled": True, "fact_count": 40, "last_run_at": None,
         "planted": PLANTED},
        {"pack_id": JOY, "title": "Moments of joy", "enabled": True, "fact_count": 1234,
         "last_run_at": "2026-11-08T15:42:10Z", "consent_note": PLANTED},
        {"pack_id": OFF, "title": "Values", "enabled": False, "fact_count": 0, "last_run_at": None},
        {"pack_id": "../../etc/passwd", "title": "not a pack id", "enabled": True},
    ],
    "processing_30d": {"available": True, "models": [
        {"model": "local-9b", "phase": "extraction", "calls": 100, "total_latency_ms": 2_280_000.0},
        {"model": "local-27b", "phase": "verification", "calls": 50, "total_latency_ms": 400_000.0}]},
    "message": f"key={PLANTED}",
}
WAITING = {JOY: 1204, EVENTS: 96}


def _batch(index: int, processed: int, **more: int) -> Dict[str, Any]:
    return {"index": index, "processed": processed, "assertions": processed * 2, "accepted": processed,
            "written": max(processed - 1, 0), "quarantined": 1 if processed else 0, "llm_errors": 0,
            "elapsed_s": 61.0 * processed, **more}


def _snap(state: str, packs: List[Dict[str, Any]], *, reason: Optional[str] = None, run_id: str = "bfr_1",
          until_done: bool = True, limit: int = 200) -> Dict[str, Any]:
    rows = [{"pack_id": p["pack_id"], "state": p.get("state", "waiting"), "error": p.get("error"),
             "batches": p.get("batches", []), "current": p.get("current"), "note": PLANTED} for p in packs]
    totals = {key: sum(b[key] for p in rows for b in p["batches"])
              for key in ("processed", "assertions", "accepted", "written", "quarantined", "llm_errors")}
    return {"run": {"run_id": run_id, "state": state, "reason": reason, "limit": limit, "until_done": until_done,
                    "started_at": "2026-11-10T09:00:00Z", "finished_at": None, "elapsed_s": 7384.0,
                    "pack_ids": [p["pack_id"] for p in rows], "packs": rows, "totals": totals, "planted": PLANTED}}


WORKING = _snap("running", [{"pack_id": EVENTS, "state": "running", "current": _batch(1, 57)}, {"pack_id": JOY}])
HALF = _snap("running", [{"pack_id": EVENTS, "state": "done", "batches": [_batch(1, 96)]},
                         {"pack_id": JOY, "state": "running", "batches": [_batch(1, 200)], "current": _batch(2, 3)}])
FINISHED = _snap("done", [{"pack_id": EVENTS, "state": "done", "batches": [_batch(1, 96)]},
                          {"pack_id": JOY, "state": "done", "batches": [_batch(1, 200), _batch(2, 4)]}])
STOPPED = _snap("stopped", [{"pack_id": EVENTS, "state": "stopped", "batches": [_batch(1, 57)]}, {"pack_id": JOY}],
                reason="stopped")


class _Socket:
    """The owner socket, in memory. `status` is what successive reads of the run answer after the start (the last
    one repeats); an exception in it is raised, so a KeyboardInterrupt there is a Ctrl-C while following."""

    def __init__(self, *, before: Optional[Dict[str, Any]] = None, status: Optional[List[Any]] = None,
                 answers: Optional[Dict[tuple, Any]] = None, packs: Optional[Dict[str, Any]] = None,
                 waiting: Optional[Dict[str, int]] = None, started: bool = False) -> None:
        self.calls: List[Dict[str, Any]] = []
        self.before = before if before is not None else {"run": None}
        self.status = list(status if status is not None else [FINISHED])
        self.answers = dict(answers or {})
        self.packs = packs if packs is not None else PACKS
        self.waiting = waiting if waiting is not None else WAITING
        self.started = started      # True: a run is already working when the command begins

    def sent(self) -> List[tuple]:
        return [(c["method"], c["path"]) for c in self.calls]

    def request_sync(self, method, path, json_body=None, params=None, label=None, headers=None):
        self.calls.append({"method": method, "path": path, "json": json_body, "params": params, "headers": headers})
        answer = self.answers.get((method, path))
        if isinstance(answer, BaseException):
            raise answer
        if answer is not None:
            return answer
        if (method, path) == ("GET", packs_cmd.PACKS_PATH):
            return self.packs
        if (method, path) == ("GET", packs_cmd.PLAN_PATH):
            asked = list((params or {}).get("pack_id") or [])
            return {"packs": [{"pack_id": pid, "title": pid, "known": True, "enabled": True,
                               "waiting": self.waiting.get(pid, 0), "planted": PLANTED} for pid in asked],
                    "history_window": 20000, "history_records": 31000,
                    "total_waiting": sum(self.waiting.get(pid, 0) for pid in asked)}
        if (method, path) == ("POST", packs_cmd.RUNS_PATH):
            self.started = True
            return _snap("running", [{"pack_id": pid} for pid in json_body["pack_ids"]],
                         until_done=json_body["until_done"], limit=json_body["limit"])
        if (method, path) == ("GET", packs_cmd.CURRENT_PATH):
            if not self.started:
                return self.before
            frame = self.status.pop(0) if len(self.status) > 1 else self.status[0]
            if isinstance(frame, BaseException):
                raise frame
            return frame
        if (method, path) == ("POST", packs_cmd.STOP_PATH):
            return {"stopping": True, "run": None}
        raise AssertionError(f"unexpected request {method} {path}")


class _Stream:
    def __init__(self, tty: bool):
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


@pytest.fixture
def cli(monkeypatch):
    """Run a `packs` command with a terminal (or not), a scripted answer to the prompt and a fake socket."""
    import topos.uds as uds

    tokens: List[str] = []

    def _token() -> str:
        tokens.append("confirm-token")
        return "confirm-token"

    monkeypatch.setattr(uds, "write_owner_confirmation", _token)
    monkeypatch.setattr(packs_cmd, "_sleep", lambda seconds: None)

    def run(*args: str, tty: bool = True, answer: bool = True, socket: Optional[_Socket] = None):
        sock = socket or _Socket()
        prompts: List[str] = []

        def _confirm(text, default=False, err=False, **_):
            prompts.append(text)
            assert default is False, "the default answer is no"
            return answer

        monkeypatch.setattr(packs_cmd, "_socket", lambda *a, **kw: sock)
        monkeypatch.setattr(packs_cmd, "sys", SimpleNamespace(stdin=_Stream(tty), stdout=_Stream(tty), exit=sys.exit))
        monkeypatch.setattr(click, "confirm", _confirm)
        result = CliRunner().invoke(packs_cmd.packs_group, list(args), catch_exceptions=False)
        assert PLANTED not in result.output, "a response body reached the terminal"
        return SimpleNamespace(result=result, out=result.output, code=result.exit_code, sock=sock,
                               calls=sock.calls, prompts=prompts, tokens=list(tokens))

    return run


def _posts(out) -> List[Dict[str, Any]]:
    return [c for c in out.calls if c["method"] == "POST"]


# ----------------------------------------------------------------------------- the yes


def test_it_shows_the_plan_asks_once_and_sends_one_confirmed_start(cli):
    out = cli("backfill", "--pack", JOY, "--pack", EVENTS, "--pack", JOY, "--until-done",
              socket=_Socket(status=[WORKING, HALF, FINISHED]))
    assert out.code == packs_cmd.EXIT_OK, out.out
    assert out.prompts == ["Run it?"] and out.tokens == ["confirm-token"]
    assert out.sock.sent()[:4] == [("GET", packs_cmd.PACKS_PATH), ("GET", packs_cmd.CURRENT_PATH),
                                   ("GET", packs_cmd.PLAN_PATH), ("POST", packs_cmd.RUNS_PATH)]
    assert out.calls[2]["params"] == {"pack_id": [JOY, EVENTS]}
    (start,) = _posts(out)
    assert start["json"] == {"pack_ids": [JOY, EVENTS], "limit": 200, "until_done": True}
    assert start["headers"] == {"X-Topos-Owner-Confirm": "confirm-token"}
    # the plan, before the question
    plan = out.out.split("life.events:")[0]
    assert "moments.joy    1,204 records waiting" in plan and "life.events       96 records waiting" in plan
    assert "2 packs, 1,300 records waiting (counted over the newest 20,000 journal entries" in plan
    assert "Batches of 200 records, one pack after another, until each pack is done." in plan
    assert "22.8 s per extraction call that is about 8h 14m of model time" in plan
    # each batch once, as it finished, then the totals
    assert out.out.count("  batch 1: 96 processed, 192 assertions, 96 accepted, 95 written, 1 quarantined, 1h 37m") == 1
    assert out.out.count("  batch 1: 200 processed") == 1 and out.out.count("  batch 2: 4 processed") == 1
    assert out.out.index("life.events:") < out.out.index("moments.joy:")
    assert "Done: 300 processed, 600 assertions, 300 accepted, 297 written, 3 quarantined in 2h 03m." in out.out


def test_one_batch_per_pack_is_the_default_and_says_what_it_leaves(cli):
    out = cli("backfill", "--pack", JOY, "--pack", EVENTS, "--limit", "100",
              socket=_Socket(status=[_snap("done", [{"pack_id": JOY, "state": "done", "batches": [_batch(1, 100)]}],
                                           until_done=False, limit=100)]))
    assert out.code == packs_cmd.EXIT_OK, out.out
    assert _posts(out)[0]["json"] == {"pack_ids": [JOY, EVENTS], "limit": 100, "until_done": False}
    assert "One batch of up to 100 records per pack, one pack after another: 196 records this run." in out.out
    assert "That was one batch per pack. --until-done reads the rest." in out.out


def test_all_enabled_names_exactly_the_packs_that_are_switched_on(cli):
    out = cli("backfill", "--all-enabled", "--until-done")
    assert out.code == packs_cmd.EXIT_OK, out.out
    assert _posts(out)[0]["json"]["pack_ids"] == [EVENTS, JOY], "sorted; the one that is off and the bad id left out"


def test_a_no_starts_nothing_and_mints_no_confirmation(cli):
    out = cli("backfill", "--all-enabled", answer=False)
    assert out.code == packs_cmd.EXIT_OK and "Nothing started." in out.out
    assert out.prompts == ["Run it?"] and out.tokens == [] and _posts(out) == []


def test_without_a_terminal_it_asks_no_one_and_touches_nothing(cli):
    out = cli("backfill", "--all-enabled", "--until-done", tty=False)
    assert out.code == packs_cmd.EXIT_NEEDS_HUMAN
    assert "run this command in your own terminal" in out.out
    assert out.calls == [] and out.prompts == [] and out.tokens == []


def test_there_is_no_flag_that_answers_for_the_person(cli):
    for flag in ("--yes", "-y", "--force", "--no-confirm", "--confirm"):
        out = cli("backfill", "--all-enabled", flag)
        assert out.code == 2 and out.calls == [] and out.tokens == [], flag
    options = {opt for param in packs_cmd.packs_backfill.params for opt in param.opts}
    assert options == {"--pack", "--all-enabled", "--limit", "--until-done", "--detach"}


@pytest.mark.parametrize("args", [(), ("--pack", JOY, "--all-enabled"), ("--pack", "Not A Pack"),
                                  ("--all-enabled", "--limit", "0"), ("--all-enabled", "--limit", "1001")])
def test_a_command_that_does_not_say_what_to_run_is_a_usage_error(cli, args):
    out = cli("backfill", *args)
    assert out.code == 2 and out.calls == [] and out.prompts == []


def test_an_unknown_or_switched_off_pack_is_refused_before_anyone_is_asked(cli):
    unknown = cli("backfill", "--pack", "no.such_pack", "--pack", JOY)
    assert unknown.code == packs_cmd.EXIT_FAILED and "No such pack: no.such_pack." in unknown.out
    off = cli("backfill", "--pack", OFF)
    assert off.code == packs_cmd.EXIT_FAILED and f"Not switched on: {OFF}." in off.out
    for out in (unknown, off):
        assert out.prompts == [] and out.tokens == [] and _posts(out) == []


def test_a_run_that_is_already_working_is_not_asked_about_twice(cli):
    out = cli("backfill", "--all-enabled", socket=_Socket(before=WORKING))
    assert out.code == packs_cmd.EXIT_FAILED and "already working" in out.out
    assert out.prompts == [] and out.tokens == [] and _posts(out) == []


def test_nothing_waiting_ends_without_a_question(cli):
    out = cli("backfill", "--all-enabled", socket=_Socket(waiting={}))
    assert out.code == packs_cmd.EXIT_OK and "Nothing is waiting for these packs." in out.out
    assert out.prompts == [] and out.tokens == [] and _posts(out) == []


def test_no_packs_switched_on_ends_without_a_question(cli):
    out = cli("backfill", "--all-enabled", socket=_Socket(packs={"packs": [PACKS["packs"][2]]}))
    assert out.code == packs_cmd.EXIT_OK and "No fact packs are switched on." in out.out
    assert out.prompts == [] and _posts(out) == []


@pytest.mark.parametrize("refusal, says", [
    (NodeRefused(403, "owner_confirmation_required"), "Topos refused the backfill (owner_confirmation_required)"),
    (NodeRefused(409, "backfill_already_running"), "Another backfill started first."),
    (NodeRefused(409, "pack_not_enabled"), "Topos refused the backfill (pack_not_enabled)"),
])
def test_a_refused_start_says_so_and_follows_nothing(cli, refusal, says):
    out = cli("backfill", "--all-enabled", socket=_Socket(answers={("POST", packs_cmd.RUNS_PATH): refusal}))
    assert out.code == packs_cmd.EXIT_FAILED and says in out.out
    assert out.sock.sent()[-1] == ("POST", packs_cmd.RUNS_PATH)


def test_a_node_that_is_down_is_exit_4(cli):
    down = cli("backfill", "--all-enabled", socket=_Socket(answers={("GET", packs_cmd.PACKS_PATH): NodeDown("down")}))
    assert down.code == packs_cmd.EXIT_NODE_DOWN and down.prompts == []
    mid = cli("backfill", "--all-enabled", socket=_Socket(status=[WORKING, NodeDown("down")]))
    assert mid.code == packs_cmd.EXIT_NODE_DOWN and "the run ended with it" in mid.out


def test_detach_starts_the_run_and_returns(cli):
    out = cli("backfill", "--all-enabled", "--until-done", "--detach")
    assert out.code == packs_cmd.EXIT_OK and "Started (bfr_1)." in out.out
    assert out.sock.sent()[-1] == ("POST", packs_cmd.RUNS_PATH) and out.tokens == ["confirm-token"]


# ----------------------------------------------------------------------------- following


def test_ctrl_c_asks_the_node_to_stop_and_waits_for_the_stop(cli):
    out = cli("backfill", "--all-enabled", "--until-done",
              socket=_Socket(status=[WORKING, KeyboardInterrupt(), WORKING, STOPPED]))
    assert out.code == packs_cmd.EXIT_STOPPED, out.out
    stops = [c for c in out.calls if (c["method"], c["path"]) == ("POST", packs_cmd.STOP_PATH)]
    assert len(stops) == 1 and stops[0]["headers"] is None, "a stop carries no confirmation: none was minted for it"
    assert out.tokens == ["confirm-token"]
    assert "Stopping after the record in hand" in out.out
    assert "  batch 1: 57 processed" in out.out
    assert "Stopped: 57 processed" in out.out and "The same command picks up where it stopped." in out.out


def test_a_second_ctrl_c_stops_watching_only(cli):
    out = cli("backfill", "--all-enabled",
              socket=_Socket(status=[WORKING, KeyboardInterrupt(), WORKING, KeyboardInterrupt(), FINISHED]))
    assert out.code == packs_cmd.EXIT_STOPPED and "No longer watching." in out.out
    assert out.sock.sent().count(("POST", packs_cmd.STOP_PATH)) == 1


def test_a_batch_in_flight_is_reported_between_batches(cli, monkeypatch):
    monkeypatch.setattr(packs_cmd, "HEARTBEAT_S", 0.0)
    out = cli("backfill", "--all-enabled", "--until-done", socket=_Socket(status=[WORKING, WORKING, FINISHED]))
    assert out.out.count("  batch 1: 57 of up to 200 so far") == 1, "once per change, not once per read"


def test_a_model_that_went_away_is_a_failure_that_keeps_what_was_done(cli):
    failed = _snap("failed", [{"pack_id": EVENTS, "state": "failed", "error": "model_unavailable",
                               "batches": [_batch(1, 5, llm_errors=5)]}, {"pack_id": JOY}], reason="model_unavailable")
    out = cli("backfill", "--all-enabled", "--until-done", socket=_Socket(status=[failed]))
    assert out.code == packs_cmd.EXIT_FAILED
    assert "the local model failed several extraction calls in a row" in out.out
    assert "5 model errors" in out.out and "finished records are kept" in out.out


def test_a_skipped_pack_makes_a_finished_run_a_failure(cli):
    partly = _snap("done", [{"pack_id": EVENTS, "state": "failed", "error": "pack_not_enabled"},
                            {"pack_id": JOY, "state": "done", "batches": [_batch(1, 4)]}])
    out = cli("backfill", "--all-enabled", "--until-done", socket=_Socket(status=[partly]))
    assert out.code == packs_cmd.EXIT_FAILED
    assert "  not finished: pack_not_enabled" in out.out and f"Not finished: {EVENTS}." in out.out


def test_a_node_that_restarted_is_reported_not_followed_forever(cli):
    other = _snap("running", [{"pack_id": JOY}], run_id="bfr_other")
    for after in ({"run": None}, other):
        out = cli("backfill", "--all-enabled", socket=_Socket(status=[WORKING, after]))
        assert out.code == packs_cmd.EXIT_FAILED and "The node restarted, and the run ended with it." in out.out


def test_a_status_that_keeps_failing_gives_up_on_watching_not_on_the_run(cli):
    out = cli("backfill", "--all-enabled",
              socket=_Socket(status=[WORKING] + [NodeRefused(503, "http_503")] * packs_cmd.MAX_POLL_FAILURES))
    assert out.code == packs_cmd.EXIT_FAILED and "It may still be working" in out.out
    assert ("POST", packs_cmd.STOP_PATH) not in out.sock.sent()


# ----------------------------------------------------------------------------- list, status, stop


def test_list_prints_ids_switches_counts_and_last_runs(cli):
    out = cli("list", tty=False)
    assert out.code == 0 and out.prompts == [] and out.tokens == []
    lines = out.out.splitlines()
    assert lines[0].split() == ["PACK", "ON", "FACTS", "LAST", "RUN", "(UTC)"]
    assert lines[1].split() == [EVENTS, "yes", "40", "never"]
    assert lines[2].split() == [JOY, "yes", "1,234", "2026-11-08", "15:42:10"]
    assert lines[3].split() == [OFF, "no", "0", "never"]
    assert "2 of 3 switched on." in lines[4] and "passwd" not in out.out
    as_json = json.loads(cli("list", "--json", tty=False).out)
    assert as_json["packs"][1] == {"pack_id": JOY, "title": "Moments of joy", "enabled": True, "fact_count": 1234,
                                   "last_run_at": "2026-11-08 15:42:10"}


def test_status_reads_the_run_and_needs_no_terminal(cli):
    none = cli("status", tty=False)
    assert none.code == 0 and "No backfill run since the node started." in none.out
    out = cli("status", tty=False, socket=_Socket(before=HALF))
    assert out.code == 0 and out.sock.sent() == [("GET", packs_cmd.CURRENT_PATH)]
    assert "Run bfr_1: running, started 2026-11-10 09:00:00 UTC, 2h 03m." in out.out
    assert f"  {EVENTS}: done, 1 batch: 96 processed" in out.out
    assert f"  {JOY}: running, 1 batch: 200 processed" in out.out and "batch 2 in flight, 3 so far" in out.out
    doc = json.loads(cli("status", "--json", tty=False, socket=_Socket(before=HALF)).out)
    assert doc["run"]["state"] == "running" and set(doc["run"]["packs"][0]) == {"pack_id", "state", "error", "batches",
                                                                               "current"}


def test_status_follow_watches_a_working_run_to_its_end(cli):
    out = cli("status", "--follow", tty=False, socket=_Socket(status=[WORKING, HALF, FINISHED], started=True))
    assert out.code == packs_cmd.EXIT_OK and "Done: 300 processed" in out.out
    assert out.out.count("  batch 1: 96 processed") == 1
    assert out.tokens == [] and all(c["method"] == "GET" for c in out.calls)


def test_stop_asks_the_node_and_needs_no_confirmation(cli):
    out = cli("stop", tty=False)
    assert out.code == 0 and "Stopping after the record in hand." in out.out
    assert out.sock.sent() == [("POST", packs_cmd.STOP_PATH)] and out.calls[0]["headers"] is None and out.tokens == []
    idle = cli("stop", tty=False, socket=_Socket(answers={("POST", packs_cmd.STOP_PATH): {"stopping": False}}))
    assert idle.code == 0 and "No backfill run is active." in idle.out


# ----------------------------------------------------------------------------- the node serves what the CLI sends


def _served(method: str, path: str) -> bool:
    from starlette.routing import Match

    from topos.app import app

    scope = {"type": "http", "method": method, "path": path, "root_path": "", "query_string": b"", "headers": []}
    return any(route.matches(scope)[0] == Match.FULL for route in app.routes)


def test_every_path_the_commands_send_is_a_route_the_node_serves(cli):
    sent = set()
    for args, sock in ((("list",), None), (("status",), None), (("stop",), None),
                       (("backfill", "--all-enabled"), _Socket(status=[WORKING, KeyboardInterrupt(), STOPPED]))):
        sent |= set(cli(*args, socket=sock).sock.sent())
    assert sent == {("GET", packs_cmd.PACKS_PATH), ("GET", packs_cmd.PLAN_PATH), ("GET", packs_cmd.CURRENT_PATH),
                    ("POST", packs_cmd.RUNS_PATH), ("POST", packs_cmd.STOP_PATH)}
    missing = sorted(f"{m} {p}" for m, p in sent if not _served(m, p))
    assert not missing, f"the CLI sends paths the node does not serve: {missing}"


def test_only_the_start_needs_the_yes_and_it_is_the_dangerous_one():
    assert census.lookup("POST", packs_cmd.RUNS_PATH).verdict == census.DANGEROUS
    assert census.lookup("POST", packs_cmd.STOP_PATH).verdict == census.OPEN
    for path in (packs_cmd.PACKS_PATH, packs_cmd.PLAN_PATH, packs_cmd.CURRENT_PATH):
        assert census.lookup("GET", path).verdict == census.READ


def test_the_group_is_on_the_node_cli():
    from topos.cli.commands import main

    assert main.commands["packs"] is packs_cmd.packs_group
    assert set(packs_cmd.packs_group.commands) == {"list", "backfill", "status", "stop"}
