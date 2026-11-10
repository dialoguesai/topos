"""`topos-node packs list|backfill|status|stop` — fact packs and their history backfill, from a terminal.

A fact pack reads new records as they arrive; the history from before it was switched on is read
only when the owner asks (a backfill). In the app that is one press per batch of 200 records per
pack. Here it is one command and one yes:

    topos-node packs list
    topos-node packs backfill --all-enabled --until-done
    topos-node packs backfill --pack moments.joy --pack life.events --limit 100

`backfill` prints the plan (which packs, how many records wait for each, the batch size), asks
`[y/N]` on this terminal, and only then starts ONE run on the node (`features/derivation/backfill_runs`).
The node works through the run itself, pack after pack and batch after batch; this command follows
it and prints each batch. Ctrl-C asks the node to stop after the record in hand. Every finished
record is kept, so the same command later picks up where the run stopped.

Starting a run is a dangerous change on the owner socket (`owner_door_routes`): it needs the human,
asked here on a terminal, and a one-change confirmation token (`uds.write_owner_confirmation`) —
never a flag an agent could pass. An agent that wants a backfill tells the person to run this
command in their own terminal. `list`, `status` and `stop` change nothing a yes protects and need
no terminal.

What is printed is a projection: pack ids, counts, state words, times. Never a response body.
"""
from __future__ import annotations

import json
import re
import sys
import time
from typing import Any, Dict, List, Optional

import click

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NEEDS_HUMAN = 2
EXIT_NODE_DOWN = 4
#: The run was stopped before its packs were done (Ctrl-C here, `packs stop`, or the node going down).
EXIT_STOPPED = 130

PACKS_PATH = "/v1/signal/derivation/packs"
PLAN_PATH = "/v1/signal/derivation/backfill/plan"
RUNS_PATH = "/v1/signal/derivation/backfill/runs"
CURRENT_PATH = RUNS_PATH + "/current"
STOP_PATH = CURRENT_PATH + "/stop"

DEFAULT_LIMIT = 200
MAX_LIMIT = 1000
#: Seconds between reads of a run being followed.
POLL_S = 2.0
#: A batch in flight is reported at most this often (a batch of 200 records runs for an hour or more).
HEARTBEAT_S = 300.0
#: Reads of the run that may fail in a row before the follower gives up (the run itself goes on).
MAX_POLL_FAILURES = 10

_PACK_ID_RE = re.compile(r"^[a-z0-9_]{1,40}(\.[a-z0-9_]{1,40}){0,3}$")
_WORD_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_JSON_OPTION = click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
_ACTIVE = ("running", "stopping")


class _PackId(click.ParamType):
    name = "pack"

    def convert(self, value, param, ctx):
        text = str(value or "").strip()
        if not _PACK_ID_RE.match(text):
            self.fail(f"{value!r} is not a pack id (see `topos-node packs list`)", param, ctx)
        return text


PACK = _PackId()


def _socket(timeout_s: float = 600.0):
    from topos.mcp_server import NodeSocket

    return NodeSocket(timeout_s=timeout_s)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _fail(message: str, code: int = EXIT_FAILED) -> None:
    click.echo(message, err=True)
    sys.exit(code)


def _request(sock, method: str, path: str, *, what: str, **kwargs) -> Dict[str, Any]:
    """One request; a node that is down or refuses ends the command with its code, never its body."""
    from topos.mcp_server import NodeDown, NodeRefused

    try:
        return sock.request_sync(method, path, **kwargs)
    except NodeDown:
        _fail("Topos isn't running — say \"start Topos\".", EXIT_NODE_DOWN)
    except NodeRefused as exc:
        _fail(f"Topos refused {what} ({exc.code or exc.status}).")
    return {}  # unreachable


# ----------------------------------------------------------------------------- projections
def _int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _word(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text if _WORD_RE.match(text) else None


def _stamp(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text[:19].replace("T", " ") if re.match(r"^\d{4}-\d{2}-\d{2}", text) else None


def _packs(body: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for row in body.get("packs") or []:
        if not isinstance(row, dict) or not _PACK_ID_RE.match(str(row.get("pack_id") or "")):
            continue
        out.append({"pack_id": str(row["pack_id"]), "title": str(row.get("title") or row["pack_id"])[:80],
                    "enabled": bool(row.get("enabled")), "fact_count": _int(row.get("fact_count")),
                    "last_run_at": _stamp(row.get("last_run_at"))})
    return out


def _extraction_seconds(body: Dict[str, Any]) -> Optional[float]:
    """The last 30 days' mean seconds per extraction call on this node, when it has measured any."""
    processing = body.get("processing_30d")
    rows = processing.get("models") if isinstance(processing, dict) else None
    calls = total_ms = 0.0
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("phase") == "extraction":
            calls += _int(row.get("calls"))
            total_ms += float(row.get("total_latency_ms") or 0.0)
    return (total_ms / calls / 1000.0) if calls >= 1 and total_ms > 0 else None


def _run(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The run in a status body, reduced to ids, state words and counts."""
    run = body.get("run")
    if not isinstance(run, dict):
        return None

    def batch(row: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(row, dict):
            return None
        return {"index": _int(row.get("index")), "elapsed_s": float(row.get("elapsed_s") or 0.0),
                **{k: _int(row.get(k)) for k in ("processed", "assertions", "accepted", "written",
                                                 "quarantined", "llm_errors")}}

    packs = []
    for row in run.get("packs") or []:
        if not isinstance(row, dict) or not _PACK_ID_RE.match(str(row.get("pack_id") or "")):
            continue
        packs.append({"pack_id": str(row["pack_id"]), "state": _word(row.get("state")) or "unknown",
                      "error": _word(row.get("error")),
                      "batches": [b for b in (batch(x) for x in row.get("batches") or []) if b],
                      "current": batch(row.get("current"))})
    totals = run.get("totals") if isinstance(run.get("totals"), dict) else {}
    return {"run_id": str(run.get("run_id") or "")[:40], "state": _word(run.get("state")) or "unknown",
            "reason": _word(run.get("reason")), "limit": _int(run.get("limit")),
            "until_done": bool(run.get("until_done")), "started_at": _stamp(run.get("started_at")),
            "finished_at": _stamp(run.get("finished_at")), "elapsed_s": float(run.get("elapsed_s") or 0.0),
            "packs": packs, "totals": {k: _int(v) for k, v in totals.items() if _WORD_RE.match(str(k))}}


def _duration(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _counts_line(counts: Dict[str, Any]) -> str:
    line = (f"{_int(counts.get('processed')):,} processed, {_int(counts.get('assertions')):,} assertions, "
            f"{_int(counts.get('accepted')):,} accepted, {_int(counts.get('written')):,} written, "
            f"{_int(counts.get('quarantined')):,} quarantined")
    errors = _int(counts.get("llm_errors"))
    return line + (f", {errors:,} model errors" if errors else "")


# ----------------------------------------------------------------------------- the group
@click.group("packs")
def packs_group() -> None:
    """List fact packs and backfill their history."""


@packs_group.command("list")
@_JSON_OPTION
def packs_list(as_json: bool) -> None:
    """Every fact pack: switched on or not, facts held, last run."""
    packs = _packs(_request(_socket(), "GET", PACKS_PATH, what="the pack list"))
    if as_json:
        click.echo(json.dumps({"packs": packs}, indent=2, sort_keys=True))
        return
    if not packs:
        click.echo("No fact packs on this node.")
        return
    width = max(len(p["pack_id"]) for p in packs)
    click.echo(f"{'PACK'.ljust(width)}  ON   {'FACTS':>7}  LAST RUN (UTC)")
    for p in packs:
        click.echo(f"{p['pack_id'].ljust(width)}  {'yes' if p['enabled'] else 'no '}  {p['fact_count']:>7,}  "
                   f"{p['last_run_at'] or 'never'}")
    on = sum(1 for p in packs if p["enabled"])
    click.echo(f"{on} of {len(packs)} switched on. `topos-node packs backfill --all-enabled` reads their history.")


@packs_group.command("backfill")
@click.option("--pack", "pack_ids", multiple=True, type=PACK, metavar="ID", help="A pack to backfill (repeatable).")
@click.option("--all-enabled", is_flag=True, help="Every pack that is switched on.")
@click.option("--limit", type=click.IntRange(1, MAX_LIMIT), default=DEFAULT_LIMIT, show_default=True,
              help="Records per batch.")
@click.option("--until-done", is_flag=True, help="Batch after batch until each pack's history is read "
                                                 "(default: one batch per pack).")
@click.option("--detach", is_flag=True, help="Start the run and return; `packs status --follow` watches it.")
def packs_backfill(pack_ids: tuple, all_enabled: bool, limit: int, until_done: bool, detach: bool) -> None:
    """Read the history of fact packs: shows the plan, asks you, then the node works through it."""
    if bool(pack_ids) == bool(all_enabled):
        raise click.UsageError("Name the packs with --pack ID (repeatable), or pass --all-enabled.")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        _fail("A backfill puts hours of local-model work on your Topos, so it asks you first: "
              "run this command in your own terminal.", EXIT_NEEDS_HUMAN)
    sock = _socket()
    listing = _request(sock, "GET", PACKS_PATH, what="the pack list")
    known = {p["pack_id"]: p for p in _packs(listing)}
    if all_enabled:
        chosen = sorted(pid for pid, p in known.items() if p["enabled"])
        if not chosen:
            click.echo("No fact packs are switched on. Switch them on in the app (Data → Fact packs).")
            sys.exit(EXIT_OK)
    else:
        chosen = list(dict.fromkeys(pack_ids))
        unknown = [pid for pid in chosen if pid not in known]
        off = [pid for pid in chosen if pid in known and not known[pid]["enabled"]]
        if unknown:
            _fail(f"No such pack: {', '.join(unknown)}. `topos-node packs list` shows them.")
        if off:
            _fail(f"Not switched on: {', '.join(off)}. Switch a pack on in the app (Data → Fact packs) first.")

    active = _run(_request(sock, "GET", CURRENT_PATH, what="the backfill status"))
    if active is not None and active["state"] in _ACTIVE:
        _fail("A backfill run is already working. `topos-node packs status --follow` watches it; "
              "`topos-node packs stop` stops it.")

    plan = _request(sock, "GET", PLAN_PATH, what="the backfill count", params={"pack_id": chosen})
    waiting = {str(row.get("pack_id")): _int(row.get("waiting")) for row in plan.get("packs") or []
               if isinstance(row, dict)}
    total = sum(waiting.get(pid, 0) for pid in chosen)
    width = max(len(pid) for pid in chosen)
    click.echo("Fact-pack backfill")
    for pid in chosen:
        click.echo(f"  {pid.ljust(width)}  {waiting.get(pid, 0):>7,} records waiting")
    window = _int(plan.get("history_window"))
    click.echo(f"{len(chosen)} pack{'s' if len(chosen) != 1 else ''}, {total:,} records waiting"
               + (f" (counted over the newest {window:,} journal entries and the newest {window:,} messages)."
                  if window else "."))
    if not total:
        click.echo("Nothing is waiting for these packs.")
        sys.exit(EXIT_OK)
    to_do = total if until_done else sum(min(limit, waiting.get(pid, 0)) for pid in chosen)
    if until_done:
        click.echo(f"Batches of {limit:,} records, one pack after another, until each pack is done.")
    else:
        click.echo(f"One batch of up to {limit:,} records per pack, one pack after another: "
                   f"{to_do:,} records this run. --until-done reads them all.")
    click.echo("Each record is one call to the local extraction model, and one verifier call for each fact it finds.")
    per_call = _extraction_seconds(listing)
    if per_call:
        click.echo(f"At this node's 30-day average of {per_call:.1f} s per extraction call that is about "
                   f"{_duration(to_do * per_call)} of model time, before the verifier calls.")
    click.echo("The node does the work and this terminal follows it. Ctrl-C stops it after the record in hand; "
               "the same command later picks up where it stopped.")
    if not click.confirm("Run it?", default=False, err=True):
        click.echo("Nothing started.")
        sys.exit(EXIT_OK)

    from topos.mcp_server import NodeDown, NodeRefused
    from topos.uds import write_owner_confirmation

    try:
        started = sock.request_sync("POST", RUNS_PATH,
                                    json_body={"pack_ids": chosen, "limit": limit, "until_done": until_done},
                                    headers={"X-Topos-Owner-Confirm": write_owner_confirmation()})
    except NodeDown:
        _fail("Topos stopped before the run started. Check `topos-node status`.", EXIT_NODE_DOWN)
    except NodeRefused as exc:
        if exc.code == "backfill_already_running":
            _fail("Another backfill started first. `topos-node packs status --follow` watches it.")
        _fail(f"Topos refused the backfill ({exc.code or exc.status}). Nothing started.")
    run = _run(started)
    if run is None or not run["run_id"]:
        _fail("Topos did not report a run. Check `topos-node packs status`.")
    if detach:
        click.echo(f"Started ({run['run_id']}). `topos-node packs status --follow` watches it; "
                   "`topos-node packs stop` stops it.")
        sys.exit(EXIT_OK)
    sys.exit(_follow(sock, run["run_id"], first=run))


@packs_group.command("status")
@click.option("--follow", is_flag=True, help="Keep printing batches until the run ends (Ctrl-C stops the run).")
@_JSON_OPTION
def packs_status(follow: bool, as_json: bool) -> None:
    """The backfill run that is working, or the last one since the node started."""
    sock = _socket()
    run = _run(_request(sock, "GET", CURRENT_PATH, what="the backfill status"))
    if as_json:
        click.echo(json.dumps({"run": run}, indent=2, sort_keys=True))
        return
    if run is None:
        click.echo("No backfill run since the node started.")
        return
    if follow and run["state"] in _ACTIVE:
        sys.exit(_follow(sock, run["run_id"]))
    click.echo(f"Run {run['run_id']}: {run['state']}" + (f" ({run['reason']})" if run["reason"] else "")
               + f", started {run['started_at'] or '?'} UTC, {_duration(run['elapsed_s'])}.")
    for pack in run["packs"]:
        done = {k: sum(b[k] for b in pack["batches"]) for k in ("processed", "assertions", "accepted",
                                                                 "written", "quarantined", "llm_errors")}
        line = f"  {pack['pack_id']}: {pack['state']}"
        if pack["error"]:
            line += f" ({pack['error']})"
        if pack["batches"]:
            line += f", {len(pack['batches'])} batch{'es' if len(pack['batches']) != 1 else ''}: {_counts_line(done)}"
        if pack["current"]:
            line += f"; batch {pack['current']['index']} in flight, {pack['current']['processed']:,} so far"
        click.echo(line)
    click.echo(f"Total: {_counts_line(run['totals'])}.")


@packs_group.command("stop")
def packs_stop() -> None:
    """Stop the backfill run after the record in hand. Finished records are kept."""
    body = _request(_socket(), "POST", STOP_PATH, what="the stop")
    if body.get("stopping"):
        click.echo("Stopping after the record in hand. `topos-node packs status` shows when it has stopped.")
    else:
        click.echo("No backfill run is active.")


# ----------------------------------------------------------------------------- following a run
def _follow(sock, run_id: str, *, first: Optional[Dict[str, Any]] = None) -> int:
    """Print a run's batches as they finish, until it ends; the command's exit code.

    Ctrl-C asks the node to stop the run and keeps following until it has; a second Ctrl-C stops
    following only (the node still stops after the record in hand)."""
    import httpx

    from topos.mcp_server import NodeDown, NodeRefused

    printed: Dict[str, int] = {}
    announced: set = set()
    stop_asked = False
    failures = 0
    last_line_at = time.monotonic()
    last_live = (None, -1)
    run = first
    while True:
        try:
            if run is None:
                try:
                    run = _run(sock.request_sync("GET", CURRENT_PATH))
                    failures = 0
                except NodeDown:
                    click.echo("Topos stopped, and the run ended with it. Start Topos and run the same command "
                               "to pick up where it stopped.", err=True)
                    return EXIT_NODE_DOWN
                except (NodeRefused, httpx.HTTPError):
                    failures += 1
                    if failures >= MAX_POLL_FAILURES:
                        click.echo("Lost sight of the run (the node is not answering its status). It may still be "
                                   "working: `topos-node packs status` shows it.", err=True)
                        return EXIT_FAILED
                    _sleep(POLL_S)
                    continue
                if run is None or run["run_id"] != run_id:
                    click.echo("The node restarted, and the run ended with it. Run the same command to pick up "
                               "where it stopped.", err=True)
                    return EXIT_FAILED
            for pack in run["packs"]:
                pid = pack["pack_id"]
                if pack["state"] != "waiting" and pid not in announced:
                    announced.add(pid)
                    click.echo(f"{pid}:")
                for batch in pack["batches"][printed.get(pid, 0):]:
                    click.echo(f"  batch {batch['index']}: {_counts_line(batch)}, {_duration(batch['elapsed_s'])}")
                    last_line_at = time.monotonic()
                printed[pid] = len(pack["batches"])
                live = pack["current"]
                if live and (pid, live["processed"]) != last_live and time.monotonic() - last_line_at >= HEARTBEAT_S:
                    click.echo(f"  batch {live['index']}: {live['processed']:,} of up to {run['limit']:,} so far, "
                               f"{_duration(live['elapsed_s'])}")
                    last_live = (pid, live["processed"])
                    last_line_at = time.monotonic()
                if pack["state"] == "failed" and pack["error"] and (pid, "error") not in announced:
                    announced.add((pid, "error"))
                    click.echo(f"  not finished: {pack['error']}")
            if run["state"] not in _ACTIVE:
                return _report_end(run)
            run = None
            _sleep(POLL_S)
        except KeyboardInterrupt:
            if stop_asked:
                click.echo("\nNo longer watching. The node stops the run after the record in hand; "
                           "`topos-node packs status` shows it.", err=True)
                return EXIT_STOPPED
            stop_asked = True
            click.echo("\nStopping after the record in hand (Ctrl-C again to stop watching)...", err=True)
            try:
                sock.request_sync("POST", STOP_PATH)
            except (NodeDown, NodeRefused, httpx.HTTPError):
                click.echo("Could not reach the node to stop the run. `topos-node packs stop` asks again.", err=True)
                return EXIT_FAILED
            run = None


def _report_end(run: Dict[str, Any]) -> int:
    totals = f"{_counts_line(run['totals'])} in {_duration(run['elapsed_s'])}"
    skipped = [p["pack_id"] for p in run["packs"] if p["state"] == "failed"]
    if run["state"] == "done":
        click.echo(f"Done: {totals}.")
        if skipped:
            click.echo(f"Not finished: {', '.join(skipped)}. `topos-node packs status` says why.", err=True)
            return EXIT_FAILED
        if not run["until_done"]:
            click.echo("That was one batch per pack. --until-done reads the rest.")
        return EXIT_OK
    if run["state"] == "stopped":
        why = " (the node is stopping)" if run["reason"] == "node_stopping" else ""
        click.echo(f"Stopped{why}: {totals}. The same command picks up where it stopped.")
        return EXIT_STOPPED
    if run["reason"] == "model_unavailable":
        click.echo(f"Stopped: the local model failed several extraction calls in a row. {totals}.\n"
                   "Check `topos-node status`, then run the same command: finished records are kept.", err=True)
    else:
        click.echo(f"The run failed ({run['reason'] or run['state']}): {totals}. "
                   "Finished records are kept; the node log has the error.", err=True)
    return EXIT_FAILED
