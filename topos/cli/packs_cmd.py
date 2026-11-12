"""`topos-node packs list|add|try|on|off|remove|backfill|status|stop|auto|retract-reactions` — fact packs, from a terminal.

A fact pack reads new records as they arrive only for the sources whose lane runs it; everything
else, and the history from before a pack was switched on, is read when the owner asks (a backfill).
In the app that is one press per batch of 200 records per pack. Here it is one command and one yes:

    topos-node packs list
    topos-node packs backfill --all-enabled --until-done
    topos-node packs backfill --all-enabled --since 2026-04-01 --until-done
    topos-node packs backfill --pack moments.joy --pack life.events --limit 100

or one standing yes, after which the node keeps the switched-on packs current by itself
(`features/derivation/auto_catchup`): recent records only, when arrivals have gone quiet, never
beside another backfill, and a backfill you start wins over it:

    topos-node packs auto on
    topos-node packs auto status
    topos-node packs auto off

`backfill` prints the plan (which packs, how far back, how many records wait for each, which model
reads each pack and which one checks what it finds, the batch size), asks `[y/N]` once on this
terminal, and only then starts ONE run on the node (`features/derivation/backfill_runs`). One yes
covers the whole job: there is nothing to press per batch or per pack.
The node works through the run itself, pack after pack and batch after batch; this command follows
it and prints each batch. Ctrl-C asks the node to stop after the record in hand. Every finished
record is kept, so the same command later picks up where the run stopped.

Starting a run is a dangerous change on the owner socket (`owner_door_routes`): it needs the human,
asked here on a terminal, and a one-change confirmation token (`uds.write_owner_confirmation`) —
never a flag an agent could pass. An agent that wants a backfill tells the person to run this
command in their own terminal. `auto on` and `auto off` are the same kind of change (the setting is
the owner's) and ask the same way. `list`, `status`, `auto status` and `stop` change nothing a yes
protects and need no terminal.

`retract-reactions` is about facts a pack stored before it learned that a reaction the owner sent in
Messages quotes someone else (`features/derivation/reaction_retract`). By itself it only counts them,
per pack, and is a read. `retract-reactions --apply` ends them (each stays in the history, closed; no
row is deleted, no message is touched) and is the same kind of change as a backfill: a terminal, the
count, one `[y/N]`, one confirmation, one request that carries the count that was shown. It puts
nothing back: a fact one of these replaced stays closed, and the command says so before it asks.

    topos-node packs retract-reactions
    topos-node packs retract-reactions --apply

The extractor is whatever the owner has picked for facts (Topos Facts for the packs it knows, the
local model for the rest); this command names it and never changes it.

A pack of the owner's own is added from a file (`features/derivation/custom_packs`):

    topos-node packs add --check my-pack.yaml     says whether the file is a pack, adds nothing
    topos-node packs add my-pack.yaml             shows what it would read and write, asks `[y/N]`
    topos-node packs remove custom.gardening

`add` is a change to what the node may write about its owner, so it asks the same way a backfill
does: on a terminal, once, and there is no flag that answers for the person. It writes the pack file
and a small stamp beside it in the node's own pack directory and writes nothing else, so it works
with the node stopped. Its one other read is the node's database, read-only, to count the apps whose
grant already names the pack's id (`_apps_granted`): a grant names an id, so it covers whatever
definition is added under that id, and the prompt says so before the person answers. The pack
starts switched off. No route, relay message or tool adds a pack: an app cannot.

A pack drafted by a coding agent goes from file to reading with one yes from its owner:

    topos-node packs add --check --json my-pack.yaml      what the file declares, for a program to read
    topos-node packs try --json my-pack.yaml              runs the file's OWN examples on the local model
    topos-node packs add my-pack.yaml --on --backfill 30d --open-terminal --json
    topos-node packs on custom.gardening                  the switch by itself; `off` is its twin

`--check` and `try` need no person. `try` sends the file's text to the running node
(`features/derivation/pack_trial`), which runs only the examples under the file's `eval` through the
prompt and parser of a real run, on the node's own model admission, and writes nothing; it reads no
record of the owner's. `add --on [--backfill WINDOW]` names every effect before its one `[y/N]`: the
pack is added, it is switched on now, and that much history is read. After the yes it installs the
bytes it showed, switches the pack on through the owner socket's switch (`surfaces.switch_pack`: the
node switches on only a pack whose definition is still the one that was shown) and starts the run
through the door `packs backfill` uses; each of the two requests carries its own one-change
confirmation, minted here after the yes. Each stage is reported, and a stage that fails leaves the
ones before it done and says so. Without a terminal these exit `EXIT_NEEDS_HUMAN`; `--open-terminal`
opens the window where the same question is waiting (`cli/terminal_window`), bound to the bytes that
were checked. `on` and `off` ask the same way. A pack that writes about other people is never
switched on from here: its consent stays in the app.

What is printed is a projection: pack ids, counts, state words, model names, times. Never a response body.
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
#: `packs try` ran and at least one example failed, or could not be run.
EXIT_EXAMPLES_FAILED = 3
EXIT_NODE_DOWN = 4
#: `packs try`: this node has no local model picked for facts, or the model did not answer.
EXIT_NO_MODEL = 5
#: The run was stopped before its packs were done (Ctrl-C here, `packs stop`, or the node going down).
EXIT_STOPPED = 130

PACKS_PATH = "/v1/signal/derivation/packs"
TRY_PATH = PACKS_PATH + "/try"
PLAN_PATH = "/v1/signal/derivation/backfill/plan"
RUNS_PATH = "/v1/signal/derivation/backfill/runs"
CURRENT_PATH = RUNS_PATH + "/current"
STOP_PATH = CURRENT_PATH + "/stop"
AUTO_PATH = "/v1/signal/derivation/backfill/auto"
REACTION_FACTS_PATH = "/v1/signal/derivation/reaction-facts"
RETRACT_PATH = REACTION_FACTS_PATH + "/retract"

DEFAULT_LIMIT = 200
#: How long `packs try` waits for the node: up to 20 examples, one model call each, behind whatever the node is
#: already running on that model (the node ends a trial itself after `pack_trial.TIME_LIMIT_S`).
TRY_TIMEOUT_S = 1800.0
MAX_LIMIT = 1000
#: Seconds between reads of a run being followed.
POLL_S = 2.0
#: A batch in flight is reported at most this often (a batch of 200 records runs for an hour or more).
HEARTBEAT_S = 300.0
#: Reads of the run that may fail in a row before the follower gives up (the run itself goes on).
MAX_POLL_FAILURES = 10

_PACK_ID_RE = re.compile(r"^[a-z0-9_]{1,40}(\.[a-z0-9_]{1,40}){0,3}$")
_WORD_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,79}$")
_SINCE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COUNT_KEYS = ("processed", "assertions", "accepted", "written", "quarantined", "llm_errors")
_TIME_KEYS = ("extract_s", "verify_s")
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


def _model(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text if _MODEL_RE.match(text) else None


def _seconds(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0.0


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


def _call_seconds(body: Dict[str, Any], phase: str) -> Optional[float]:
    """The last 30 days' mean seconds per local-model call of one phase (`extraction`, `verification`)
    on this node, when it has measured any. Topos Facts calls are not in these numbers."""
    processing = body.get("processing_30d")
    rows = processing.get("models") if isinstance(processing, dict) else None
    calls = total_ms = 0.0
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("phase") == phase:
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
        return {"index": _int(row.get("index")), "elapsed_s": _seconds(row.get("elapsed_s")),
                **{k: _int(row.get(k)) for k in _COUNT_KEYS}, **{k: _seconds(row.get(k)) for k in _TIME_KEYS}}

    packs = []
    for row in run.get("packs") or []:
        if not isinstance(row, dict) or not _PACK_ID_RE.match(str(row.get("pack_id") or "")):
            continue
        packs.append({"pack_id": str(row["pack_id"]), "state": _word(row.get("state")) or "unknown",
                      "error": _word(row.get("error")),
                      "extract_model": _model(row.get("extract_model")),
                      "verify_model": _model(row.get("verify_model")),
                      "batches": [b for b in (batch(x) for x in row.get("batches") or []) if b],
                      "current": batch(row.get("current"))})
    totals = run.get("totals") if isinstance(run.get("totals"), dict) else {}
    since = str(run.get("since") or "")
    return {"run_id": str(run.get("run_id") or "")[:40], "state": _word(run.get("state")) or "unknown",
            "reason": _word(run.get("reason")), "limit": _int(run.get("limit")),
            "origin": "auto" if run.get("origin") == "auto" else "owner",
            "waiting_for": _word(run.get("waiting_for")),
            "until_done": bool(run.get("until_done")), "since": since if _SINCE_RE.match(since) else None,
            "started_at": _stamp(run.get("started_at")),
            "finished_at": _stamp(run.get("finished_at")), "elapsed_s": _seconds(run.get("elapsed_s")),
            "packs": packs,
            "totals": {**{k: _int(totals.get(k)) for k in _COUNT_KEYS},
                       **{k: _seconds(totals.get(k)) for k in _TIME_KEYS}}}


def _auto(body: Any) -> Optional[Dict[str, Any]]:
    """The automatic catch-up in a status body, reduced to switches, periods, state words, stamps and counts."""
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        return None
    last = body.get("last") if isinstance(body.get("last"), dict) else None
    nxt = body.get("next") if isinstance(body.get("next"), dict) else {}
    look = None
    if last is not None:
        totals = last.get("totals") if isinstance(last.get("totals"), dict) else None
        since = str(last.get("since") or "")
        new = last.get("new_records")
        look = {"run_id": str(last.get("run_id") or "")[:40] or None, "state": _word(last.get("state")) or "unknown",
                "reason": _word(last.get("reason")), "started_at": _stamp(last.get("started_at")),
                "finished_at": _stamp(last.get("finished_at")), "since": since if _SINCE_RE.match(since) else None,
                "packs": _int(last.get("packs")), "waiting": _int(last.get("waiting")),
                "new_records": _int(new) if isinstance(new, int) and not isinstance(new, bool) else None,
                "older_records": _int(last.get("older_records")),
                "totals": ({**{k: _int(totals.get(k)) for k in _COUNT_KEYS},
                            **{k: _seconds(totals.get(k)) for k in _TIME_KEYS}} if totals is not None else None)}
    return {"enabled": body["enabled"], "kill_switch": body.get("kill_switch") is True,
            "changed_at": _stamp(body.get("changed_at")),
            "quiet_s": _int(body.get("quiet_s")), "min_gap_s": _int(body.get("min_gap_s")),
            "first_window_days": _int(body.get("first_window_days")),
            "max_window_days": _int(body.get("max_window_days")), "last": look,
            "next": {"state": _word(nxt.get("state")) or "unknown", "not_before": _stamp(nxt.get("not_before"))}}


def _auto_lines(auto: Dict[str, Any]) -> List[str]:
    """The automatic catch-up, in words: the switch, the last look, when the next may start."""
    if auto["kill_switch"]:
        head = ("Automatic catch-up: held off by TOPOS_PACKS_AUTO_CATCHUP in the node's environment"
                + (" (your switch is on)." if auto["enabled"] else "."))
    elif not auto["enabled"]:
        head = "Automatic catch-up: off. `topos-node packs auto on` keeps the switched-on packs current."
    else:
        head = "Automatic catch-up: on" + (f" since {auto['changed_at']} UTC." if auto["changed_at"] else ".")
    lines = [head]
    if auto["enabled"] and not auto["kill_switch"]:
        lines.append(f"  It looks when new journal entries or messages have landed and none has arrived for "
                     f"{_duration(auto['quiet_s'])}, at most once every {_duration(auto['min_gap_s'])}.")
    last = auto["last"]
    if last is not None:
        when = f"{last['started_at'] or '?'} UTC"
        window = f"records dated {last['since']} or later" if last["since"] else "recent records"
        if last["state"] == "nothing_waiting":
            lines.append(f"  Last look {when}: nothing waiting among {window}; no run, no model loaded.")
        else:
            line = (f"  Last automatic run {last['run_id'] or '?'}: {last['state']}"
                    + (f" ({last['reason']})" if last["reason"] else "") + f", started {when}, {window}, "
                    f"{last['packs']} pack{'s' if last['packs'] != 1 else ''}")
            if last["totals"] is not None:
                line += f": {_counts_line(last['totals'])}{_times(last['totals'])}"
            lines.append(line + ".")
        if last["older_records"]:
            lines.append(f"  {last['older_records']:,} of the records that landed carry a date before that and were "
                         "left: `topos-node packs backfill --since YYYY-MM-DD` reads them.")
    if auto["enabled"] and not auto["kill_switch"]:
        nxt = auto["next"]
        at = f"not before {nxt['not_before']} UTC" if nxt["not_before"] else "soon"
        lines.append("  Next: " + {
            "run_active": "after the backfill that is working now.",
            "waiting_for_records": "when new journal entries or messages land.",
            "waiting_for_quiet": f"{at} (arrivals have to go quiet first).",
            "waiting_for_gap": f"{at} (the minimum gap between looks).",
            "due": "at the next look, within a minute or two.",
        }.get(nxt["state"], "unknown."))
    return lines


def _auto_status(sock) -> Optional[Dict[str, Any]]:
    """The automatic catch-up's state, or None from a node that does not serve it or is not answering."""
    import httpx

    from topos.mcp_server import NodeDown, NodeRefused

    try:
        return _auto(sock.request_sync("GET", AUTO_PATH))
    except (NodeDown, NodeRefused, httpx.HTTPError):
        return None


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


def _times(counts: Dict[str, Any]) -> str:
    """Extraction and verification time, apart: the two are different models with different costs."""
    extract, verify = _seconds(counts.get("extract_s")), _seconds(counts.get("verify_s"))
    if not (extract or verify):
        return ""
    return f" (extraction {_duration(extract)}, verification {_duration(verify)})"


# ----------------------------------------------------------------------------- the group
@click.group("packs")
def packs_group() -> None:
    """List fact packs, add and try your own, switch them on or off, and backfill their history."""


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
    _unloaded_custom_packs()


def _unloaded_custom_packs() -> None:
    """Say which files in this node's own pack directory are not loading, and why: a pack the owner added that
    stopped validating is otherwise only missing from the list."""
    from topos.features.derivation.custom_packs import custom_pack_problems

    for name, reason in sorted(custom_pack_problems().items()):
        click.echo(f"Not loaded: {name}. {reason}")


def _apps_granted(pack_id: str) -> Optional[int]:
    """How many connected apps hold a grant that names this pack id, or None when this node's database cannot be
    read. The database is opened read-only and nothing is created; a node that has no database yet has no apps."""
    import sqlite3

    try:
        from topos.mcp_clients import apps_granted_pack
        from topos.storage.db.paths import resolve_active_database

        path = resolve_active_database().path
        if not path.is_file():
            return 0
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            return apps_granted_pack(conn, pack_id)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 -- a database that cannot be read is said, never guessed as "no apps"
        return None


def _existing_grants_line(pack_id: str) -> Optional[str]:
    """What to say before the question when an app's grant already names this id; None when no app's does."""
    count = _apps_granted(pack_id)
    if count is None:
        return ("  Topos could not check whether an app already holds a grant that names this id. If one does, "
                "that grant covers this definition too once you switch the pack on, with no new approval "
                "(`topos-node agents list --json` shows what each app reads).")
    if count <= 0:
        return None
    apps = "1 app already holds a grant" if count == 1 else f"{count} apps already hold a grant"
    return (f"  {apps} that names {pack_id}. A grant names the id, not this definition: once you switch the "
            "pack on, it covers the facts this file writes too, with no new approval "
            "(`topos-node agents list --json` shows what each app reads).")


#: Who reads the facts of a pack the owner adds, as `packs add` says it before it asks. An app is held to a grant
#: that names the pack (`pack_facts`). A coding agent and a routine are not: they put questions to the node, and
#: no rule holds a fact back from an answer because its pack is one the owner added (DISCLOSURE_RULES DR-74).
_WHO_READS = (
    "  It starts switched off. Once you switch it on: an app reads its facts only if you approve a grant that "
    "names this pack. A coding agent you connected and your routines need no such grant: an answer Topos gives "
    "them can draw on this pack's facts, as it can on the facts of the packs Topos ships.")
#: Said in place of whatever a check raised, when it raised instead of refusing.
_NOT_A_PACK = "The file is not a pack Topos can read."
_NODE_DOWN = "Topos isn't running — say \"start Topos\"."
_OLDER_NODE = ("This Topos is older than this command. Update it (`uv tool upgrade topos-node`), restart it, "
               "then try again.")

#: What a person reads for a refusal of the try, the switch or the run's start, written here from the node's
#: code (never the node's own text).
_REFUSALS = {
    "unknown_pack": "This Topos has no such pack. `topos-node packs list` shows them.",
    "consent_in_app": "That pack writes facts about other people, so switching it on asks for your consent in the "
                      "app (Data → Fact packs).",
    "pack_changed": "The pack changed after it was approved, so it was not switched on. Run the command again to "
                    "see what it is now.",
    "definition_required": _OLDER_NODE,
    "owner_confirmation_required": "Topos did not take the approval. Run the command again in your own terminal.",
    "backfill_already_running": "Another backfill started first. `topos-node packs status --follow` watches it.",
    "pack_not_enabled": "The pack is not switched on, so its history was not read.",
    "no_local_model": "This Topos has no local model picked for facts, so there is nothing to try the pack on.",
    "trial_already_running": "Another pack is being tried on this Topos. Wait for it, then run this again.",
    "too_many_examples": "The file holds more examples than one try runs. Try it with fewer.",
    "pack_invalid": _NOT_A_PACK,
    "http_404": _OLDER_NODE,
    "http_405": _OLDER_NODE,
}


NO_ANSWER = "no_answer"


class _Stop(Exception):
    """A step that did not happen: a fixed word, the sentence a person reads, the exit code."""

    def __init__(self, code: str, say: str, exit_code: int = EXIT_FAILED) -> None:
        self.code, self.say, self.exit_code = code, say, exit_code
        super().__init__(say)


def _node(sock, method: str, path: str, *, confirm: bool = False, **kwargs) -> Dict[str, Any]:
    """One request for `try`, `on`, `off` and the staged `add`. With `confirm` it carries a fresh one-change
    confirmation: call it so only after the person said yes on this terminal. A node that is down or refuses
    raises :class:`_Stop` with a sentence written here; the node's body goes no further than its code."""
    import httpx

    from topos.mcp_server import NodeDown, NodeRefused

    headers = None
    if confirm:
        from topos.uds import write_owner_confirmation

        try:
            headers = {"X-Topos-Owner-Confirm": write_owner_confirmation()}
        except OSError:
            raise _Stop("confirmation_not_written", "This terminal could not record your yes for Topos, so "
                        "nothing was sent. Check that your home folder is writable and run the command again."
                        ) from None
    try:
        return sock.request_sync(method, path, headers=headers, **kwargs)
    except NodeDown:
        raise _Stop("node_down", _NODE_DOWN, EXIT_NODE_DOWN) from None
    except NodeRefused as exc:
        code = str(exc.code or f"http_{exc.status}")
        raise _Stop(code, _REFUSALS.get(code, f"Topos refused this ({code}).")) from None
    except httpx.HTTPError:
        # The request went out and no answer came back (a timeout, a dropped connection). For a change, the node
        # may have made it: say so rather than guess either way.
        raise _Stop(NO_ANSWER, "Topos did not answer, so whether this happened is not known.") from None


def _json_out(doc: Dict[str, Any]) -> None:
    click.echo(json.dumps(doc, indent=2, sort_keys=True))


def _can_prompt() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _sha256(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


def _shown(text: Any, limit: int = 160) -> str:
    """Text from a pack file or a model, as a terminal may print it: one line, nothing that moves the cursor."""
    line = "".join(ch if ch.isprintable() else " " for ch in str(text or ""))
    line = " ".join(line.split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _shown_value(value: Any, limit: int = 200) -> str:
    try:
        text = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = "?"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _routing_line(reach: Dict[str, List[str]]) -> str:
    words, phrases = len(reach["words"]), len(reach["phrases"])
    return (f"  reads a record when it holds one of {words:,} word{'s' if words != 1 else ''}"
            + (f" or {phrases:,} phrase{'s' if phrases != 1 else ''}" if phrases else "")
            + " (`topos-node packs add --check --json FILE` lists them)")


class _Window(click.ParamType):
    """How far back `--backfill` reads: `30d` (any number of days), `all`, or a day as `YYYY-MM-DD`."""

    name = "window"

    def convert(self, value, param, ctx):
        import datetime as _dt

        if isinstance(value, dict):
            return value
        text = str(value or "").strip().lower()
        if text == "all":
            return {"text": "all", "since": None, "words": "all of it, as far back as a backfill reaches"}
        days = re.match(r"^([1-9][0-9]{0,3})d$", text)
        if days:
            n = int(days.group(1))
            since = (_dt.datetime.now(_dt.timezone.utc).date() - _dt.timedelta(days=n)).isoformat()
            return {"text": text, "since": since,
                    "words": f"the last {n} day{'s' if n != 1 else ''} (records dated {since} or later)"}
        if _SINCE_RE.match(text):
            try:
                _dt.date.fromisoformat(text)
            except ValueError:
                self.fail(f"{value!r} is not a day", param, ctx)
            return {"text": text, "since": text, "words": f"records dated {text} or later"}
        self.fail(f"{value!r} is not a window: use 30d, 90d (a number of days), all, or YYYY-MM-DD", param, ctx)


WINDOW = _Window()


def _trial_model(timeout_s: float = 10.0) -> Dict[str, Any]:
    """Whether `packs try` could run now: is the node up, and does it find a local extraction model. Asked of the
    running node, which looks without loading or calling a model. `unknown` when it cannot be told."""
    import httpx

    from topos.mcp_server import NodeDown, NodeRefused

    try:
        body = _socket(timeout_s).request_sync("GET", TRY_PATH)
    except NodeDown:
        return {"node_running": False, "available": "unknown", "model": None}
    except (NodeRefused, httpx.HTTPError):
        return {"node_running": True, "available": "unknown", "model": None}
    available = body.get("available")
    return {"node_running": True, "available": available if available in ("yes", "no") else "unknown",
            "model": _model(body.get("model"))}


def _check_doc(pack, content: bytes) -> Dict[str, Any]:
    """`packs add --check --json`: what the file declares and what adding it would do, for a program to read."""
    from topos.features.derivation import custom_packs, pack_trial

    try:
        install = {**custom_packs.install_plan(pack), "reason": None}
    except custom_packs.CustomPackError as exc:
        install = {"would": "refused", "replaces": None, "reason": str(exc)}
    examples = pack_trial.examples_of(pack)
    gold = [e for e in examples if e["kind"] == pack_trial.GOLD]
    return {
        "ok": True, "added": False, "pack_id": pack.pack, "version": pack.version, "title": pack.title,
        "sensitivity": pack.sensitivity_class, "role_policy": pack.role_policy,
        "reads": custom_packs.ROLE_POLICY_WORDS[pack.role_policy],
        "predicates": [{"name": name, "type": str(pack.predicates[name].value_type)}
                       for name in sorted(pack.predicates)],
        "describe": custom_packs.describe(pack),
        "install": install,
        "apps_with_grant": _apps_granted(pack.pack),
        "examples": {"gold": len(gold), "negative_controls": len(examples) - len(gold),
                     "gold_with_expected_fact": sum(1 for e in gold if e["expect"]),
                     "try_limit": pack_trial.MAX_EXAMPLES},
        "routing": custom_packs.routing_reach(pack),
        "local_model": _trial_model(),
        "sha256": _sha256(content),
    }


def _hand_to_person(*, argv: List[str], slug: str, title: str, open_terminal: bool, as_json: bool,
                    doc: Dict[str, Any], typed: str) -> None:
    """End with the waiting code: this step is a person's. With `--open-terminal` (macOS) the window where the
    command is already asking is opened; opening it grants nothing. Otherwise the person is handed the command."""
    import shlex

    command = shlex.join(["topos-node", *argv])
    opened = False
    if open_terminal:
        from topos.cli.terminal_window import open_terminal_window

        opened = open_terminal_window(slug=slug, title=title, argv=argv,
                                      # Exit 0 also ends a "no", so this line must not say "Done": the lines
                                      # above it say what happened.
                                      done="Finished; the lines above say what happened. Return to your coding "
                                           "assistant.",
                                      not_done="Not done. Return to your coding assistant.")
    say = ("Opened the question in a Terminal window. Answer it there, then return here." if opened else typed)
    if as_json:
        _json_out({**doc, "ok": True, "status": "approval_pending" if opened else "needs_human",
                   "command": command, "human": {"say": say}})
    else:
        click.echo(say if opened else f"{say}\n\n  {command}\n", err=not opened)
    sys.exit(EXIT_NEEDS_HUMAN)


def _backfill_words(sock, pack_id: str, window: Dict[str, Any], reach: Dict[str, List[str]]) -> List[str]:
    """What reading history will cost, as far as it can be said before the pack is added: the window, the model
    that reads and the one that checks, and this node's measured seconds per call. How many records wait cannot
    be counted yet (the node counts for a pack that is added and on), and the lines say so. Loads no model."""
    listing = _node(sock, "GET", PACKS_PATH)
    plan_params: Dict[str, Any] = {"pack_id": [pack_id]}
    if window["since"]:
        plan_params["since"] = window["since"]
    plan = _node(sock, "GET", PLAN_PATH, params=plan_params)
    extraction = plan.get("extraction") if isinstance(plan.get("extraction"), dict) else {}
    llm_model = _model(extraction.get("llm_model")) or "the local extraction model"
    classifier = _model(extraction.get("classifier_model")) if extraction.get("provider") == "topos_facts" else None
    rows = [row for row in plan.get("packs") or [] if isinstance(row, dict) and row.get("pack_id") == pack_id]
    route = (_word(rows[0].get("extract_route")) if rows else None) or "llm"
    if classifier and route == "classifier":
        reader = classifier
    elif classifier and route == "unknown":
        reader = f"{classifier} if it knows this pack, else the local model {llm_model}"
    else:
        reader = f"the local model {llm_model}"
    verifier = _model(plan.get("verify_model"))
    per_call = _call_seconds(listing, "extraction")
    words = len(reach["words"])
    reach_size = _int(plan.get("history_window"))
    lines = [f"     It reads {window['words']}"
             + (f", within the newest {reach_size:,} journal entries and the newest {reach_size:,} messages."
                if reach_size else "."),
             f"     Each record that holds one of the pack's {words:,} word{'s' if words != 1 else ''} is one call "
             f"to {reader}"
             + (f" (about {per_call:.1f} s each, by this node's 30-day average)" if per_call else "") + "; "
             + (f"every fact found is then checked by {verifier}, one call each." if verifier
                else "verification is off on this node."),
             "     How many records that is cannot be counted before the pack is added: the run says it when it "
             "starts. `topos-node packs stop` stops it; what it read is kept."]
    return lines


def _started_run(sock, pack_id: str, window: Dict[str, Any]) -> Dict[str, Any]:
    """Start the history run the person approved, through the door `packs backfill` uses; its receipt."""
    waiting: Optional[int] = None
    params: Dict[str, Any] = {"pack_id": [pack_id]}
    if window["since"]:
        params["since"] = window["since"]
    try:
        plan = _node(sock, "GET", PLAN_PATH, params=params)
        rows = [row for row in plan.get("packs") or [] if isinstance(row, dict) and row.get("pack_id") == pack_id]
        if rows and isinstance(rows[0].get("waiting"), int) and not isinstance(rows[0].get("waiting"), bool):
            waiting = rows[0]["waiting"]
    except _Stop:
        waiting = None                    # the count is a courtesy; the run itself is what was approved
    started = _node(sock, "POST", RUNS_PATH, confirm=True,
                    json_body={"pack_ids": [pack_id], "limit": DEFAULT_LIMIT, "until_done": True,
                               "since": window["since"]})
    run = _run(started)
    if run is None or not run["run_id"]:
        raise _Stop("no_run", "Topos did not report a run. Check `topos-node packs status`.")
    return {"stage": "backfill_started", "ok": True, "run_id": run["run_id"], "since": window["since"],
            "waiting": waiting}


@packs_group.command("add")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option("--check", is_flag=True, help="Only say whether the file is a pack Topos can add; add nothing.")
@click.option("--on", "switch_on", is_flag=True,
              help="Also switch the pack on, in the same question (default: it starts switched off).")
@click.option("--backfill", "window", type=WINDOW, default=None, metavar="WINDOW",
              help="With --on: also read history for it, in the same question: 30d, 90d (a number of days), "
                   "all, or a day as YYYY-MM-DD.")
@click.option("--open-terminal", is_flag=True,
              help="Without a terminal: open the question in a Terminal window on macOS; no command typing needed.")
@click.option("--expected-sha256", "expected", default="", hidden=True)
@_JSON_OPTION
def packs_add(file: str, check: bool, switch_on: bool, window: Optional[Dict[str, Any]], open_terminal: bool,
              expected: str, as_json: bool) -> None:
    """Add a fact pack of your own from a YAML file: shows what it would read and write, asks you once.

    With --on (and --backfill) the one question also covers switching the pack on (and reading history for it).
    Every effect is named before the question, and there is no option that answers it for you.
    """
    from pathlib import Path

    from topos.features.derivation import custom_packs

    if window is not None and not switch_on:
        raise click.UsageError("--backfill reads history for a pack that is on: pass --on with it.")
    if check and (switch_on or open_terminal):
        raise click.UsageError("--check adds nothing, so it takes no --on, --backfill or --open-terminal.")

    def refuse(sentence: str) -> None:
        if as_json:
            _json_out({"ok": False, "error": sentence})
            sys.exit(EXIT_FAILED)
        _fail(f"Not added. {sentence}")

    try:
        pack, content = custom_packs.read_candidate(Path(file))
    except custom_packs.CustomPackError as exc:
        refuse(str(exc))
    except Exception:  # noqa: BLE001 -- the last line: a file from outside never ends this command in a traceback
        refuse(_NOT_A_PACK)
    digest = _sha256(content)
    if expected and expected != digest:
        # The window was opened for other bytes: what the agent checked is not what would be shown here.
        refuse("The file changed after it was checked. Check it again, then add it.")
    if check and as_json:
        _json_out(_check_doc(pack, content))
        return
    reach = custom_packs.routing_reach(pack)
    # With --json, everything a person reads before the question goes to stderr: stdout stays one document.
    for line in custom_packs.describe(pack):
        click.echo(line, err=as_json)
    click.echo(_routing_line(reach), err=as_json)
    click.echo(_WHO_READS, err=as_json)
    grants = _existing_grants_line(pack.pack)
    if grants:
        click.echo(grants, err=as_json)
    if check:
        click.echo("This file is a pack Topos can add. Nothing was added.")
        return
    staged = switch_on or open_terminal or as_json
    base = {"pack_id": pack.pack, "version": pack.version, "sha256": digest}
    if not _can_prompt():
        if not staged:
            _fail("Adding a pack changes what Topos may write about you, so it asks you first: "
                  "run this command in your own terminal.", EXIT_NEEDS_HUMAN)
        # Look before handing over a question that could only fail.
        try:
            custom_packs.install_plan(pack)
            if switch_on:
                _node(_socket(30.0), "GET", PACKS_PATH)
        except custom_packs.CustomPackError as exc:
            refuse(str(exc))
        except _Stop as stop:
            if as_json:
                _json_out({**base, "ok": False, "status": "failed", "code": stop.code, "error": stop.say,
                           "receipts": [], "state": {"added": False, "enabled": False, "backfill_run": None}})
                sys.exit(stop.exit_code)
            _fail(stop.say, stop.exit_code)
        argv = ["packs", "add", str(Path(file).resolve()), "--expected-sha256", digest]
        argv += ["--on"] if switch_on else []
        argv += ["--backfill", window["text"]] if window is not None else []
        _hand_to_person(
            argv=argv, slug="pack-add", title="Add a fact pack to the Topos on this computer",
            open_terminal=open_terminal, as_json=as_json,
            doc={**base, "added": False},
            typed="Adding a pack changes what Topos may write about you, so it asks you first: "
                  "run this command in your own terminal.")

    sock = _socket() if switch_on else None
    cost: List[str] = []
    try:
        if staged:
            custom_packs.install_plan(pack)         # what this node would refuse is said before the question
        if switch_on:
            _node(sock, "GET", PACKS_PATH)          # is Topos running: asked before the person is
        if window is not None:
            active = _run(_node(sock, "GET", CURRENT_PATH))
            if active is not None and active["state"] in _ACTIVE and active["origin"] != "auto":
                raise _Stop("backfill_already_running",
                            "A backfill run is working now, so --backfill cannot start another. Wait for it "
                            "(`topos-node packs status --follow`), or leave --backfill out.")
            cost = _backfill_words(sock, pack.pack, window, reach)
    except custom_packs.CustomPackError as exc:
        refuse(str(exc))
    except _Stop as stop:
        if as_json:
            _json_out({**base, "ok": False, "status": "failed", "code": stop.code, "error": stop.say,
                       "receipts": [], "state": {"added": False, "enabled": False, "backfill_run": None}})
            sys.exit(stop.exit_code)
        _fail(f"Not added. {stop.say}", stop.exit_code)
    click.echo("If a pack with this id is already added, this file replaces it and the pack is switched off "
               "until you switch it on again.", err=as_json)
    if switch_on:
        click.echo("One yes here does all of this, in this order:", err=as_json)
        click.echo("  1. The pack is added: this file is copied into this Topos.", err=as_json)
        click.echo("  2. It is switched on now. From then on Topos reads your new records for it and writes the "
                   "facts it finds.", err=as_json)
        if window is not None:
            click.echo("  3. History is read for it, in one run that starts now and goes on after this command "
                       "ends.", err=as_json)
            for line in cost:
                click.echo(line, err=as_json)
        else:
            click.echo(f"  No history is read. `topos-node packs backfill --pack {pack.pack}` reads it later.",
                       err=as_json)
    question = ("Add it?" if not switch_on else "Add it and switch it on?" if window is None
                else "Add it, switch it on and read that history?")
    receipts: List[Dict[str, Any]] = []
    state = {"added": False, "enabled": False, "backfill_run": None}
    if not click.confirm(question, default=False, err=True):
        if as_json:
            _json_out({**base, "ok": True, "status": "cancelled", "receipts": receipts, "state": state})
        else:
            click.echo("Nothing added.")
        sys.exit(EXIT_OK)

    def failed(stage: str, code: str, sentence: str, exit_code: int, after: str) -> None:
        receipts.append({"stage": stage, "ok": False, "code": code, "error": sentence})
        if as_json:
            _json_out({**base, "ok": False, "status": "failed", "failed_stage": stage, "code": code,
                       "error": sentence, "receipts": receipts, "state": state})
            sys.exit(exit_code)
        _fail(f"{'Not added. ' if stage == 'added' else ''}{sentence}{after}", exit_code)

    try:
        # The bytes that were read and shown above, not the file again: a file that changed while the question
        # was open is not what the owner said yes to.
        added = custom_packs.install_content(content)
    except custom_packs.CustomPackError as exc:
        failed("added", "not_added", str(exc), EXIT_FAILED, "") if staged else _fail(f"Not added. {exc}")
    except Exception:  # noqa: BLE001
        failed("added", "not_added", _NOT_A_PACK, EXIT_FAILED, "") if staged else _fail(f"Not added. {_NOT_A_PACK}")
    receipts.append({"stage": "added", "ok": True, "replaced": added["replaced"], "unchanged": added["unchanged"]})
    state["added"] = True
    if added["unchanged"]:
        click.echo(f"{added['pack_id']} {added['version']} is already added, unchanged.", err=as_json)
    else:
        click.echo(f"Added {added['pack_id']} {added['version']}"
                   + (f" (it replaces {added['replaced']})." if added["replaced"] else "."), err=as_json)
    if not switch_on:
        if as_json:
            _json_out({**base, "ok": True, "status": "done", "receipts": receipts, "state": state})
            return
        if not added["unchanged"]:
            click.echo(f"It is switched off. `topos-node packs on {added['pack_id']}` switches it on (or the app: "
                       f"Data → Fact packs); `topos-node packs backfill --pack {added['pack_id']}` then reads "
                       "your history.")
        return

    from topos.features.derivation.registry import definition_sha

    try:
        # The digest of what was shown: the node switches on only a pack that still has it.
        _node(sock, "PUT", _switch_path(pack.pack), confirm=True,
              json_body={"enabled": True, "definition": definition_sha(pack, stamped=False)})
    except _Stop as stop:
        if stop.code == NO_ANSWER or added["unchanged"]:
            # No answer: the switch may have happened. Unchanged: the pack was already added, and its switch is
            # whatever it was before this command, which this command did not read.
            state["enabled"] = None
            after = (" The pack is added; whether it is switched on is not known. `topos-node packs list` says, "
                     f"and `topos-node packs on {pack.pack}` switches it on.")
        else:
            after = f" The pack is added and switched off. `topos-node packs on {pack.pack}` switches it on."
        failed("switched_on", stop.code, stop.say, stop.exit_code, after)
    receipts.append({"stage": "switched_on", "ok": True})
    state["enabled"] = True
    click.echo("It is switched on.", err=as_json)
    if window is not None:
        try:
            receipt = _started_run(sock, pack.pack, window)
        except _Stop as stop:
            since = f" --since {window['since']}" if window["since"] else ""
            if stop.code == NO_ANSWER:
                state["backfill_run"] = None
                after = (" The pack is added and switched on; whether reading its history started is not known. "
                         "`topos-node packs status` says. Do not start another run before checking.")
            else:
                after = (" The pack is added and switched on, and no history was read. "
                         f"`topos-node packs backfill --pack {pack.pack}{since} --until-done` reads it.")
            failed("backfill_started", stop.code, stop.say, stop.exit_code, after)
        receipts.append(receipt)
        state["backfill_run"] = receipt["run_id"]
        waiting = receipt["waiting"]
        click.echo(f"Reading history ({receipt['run_id']})"
                   + (f": {waiting:,} record{'s' if waiting != 1 else ''} waiting" if waiting is not None else "")
                   + ". `topos-node packs status --follow` watches it; `topos-node packs stop` stops it.",
                   err=as_json)
    if as_json:
        _json_out({**base, "ok": True, "status": "done", "receipts": receipts, "state": state})


# ----------------------------------------------------------------------------- trying a draft's own examples
def _trial_report(body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The node's trial report, reduced to the fields this command prints. The texts and expected facts are the
    file's own; what came back is the model's reading of them. No record of the owner's is in it."""
    if not isinstance(body, dict) or not isinstance(body.get("examples"), list):
        return None
    rows = []
    for row in body["examples"]:
        if not isinstance(row, dict) or row.get("kind") not in ("gold", "negative_control"):
            continue
        rows.append({
            "kind": row["kind"], "index": _int(row.get("index")), "text": str(row.get("text") or ""),
            "role": _word(row.get("role")) or "authored", "routed": row.get("routed") is True,
            "expected": row.get("expected") if isinstance(row.get("expected"), list) else [],
            "returned": row.get("returned") if isinstance(row.get("returned"), list) else [],
            "rejected": _int(row.get("rejected")), "seconds": _seconds(row.get("seconds")),
            "outcome": row.get("outcome") if row.get("outcome") in ("pass", "fail", "unjudged", "error", "skipped")
            else "error",
            "reason": _word(row.get("reason")), "missing": _int(row.get("missing")), "extra": _int(row.get("extra"))})

    def count(outcome: str, kind: Optional[str] = None) -> int:
        return sum(1 for row in rows if row["outcome"] == outcome and (kind is None or row["kind"] == kind))

    def part(kind: str) -> Dict[str, int]:
        return {"examples": sum(1 for row in rows if row["kind"] == kind), "passed": count("pass", kind),
                "failed": count("fail", kind)}

    totals = {"examples": len(rows), "passed": count("pass"), "failed": count("fail"),
              "unjudged": count("unjudged"), "errors": count("error"), "skipped": count("skipped"),
              "gold": part("gold"), "negative_controls": part("negative_control")}
    version = str(body.get("version") or "")
    return {"pack_id": str(body.get("pack_id") or "") if _PACK_ID_RE.match(str(body.get("pack_id") or "")) else None,
            "version": version if re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$", version) else None,
            "model": _model(body.get("model")), "template": _model(body.get("template")),
            "fact_check": "not_run", "stopped": _word(body.get("stopped")),
            "seconds": _seconds(body.get("seconds")), "examples": rows, "totals": totals}


@packs_group.command("try")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@_JSON_OPTION
def packs_try(file: str, as_json: bool) -> None:
    """Run a pack file's own examples on the local model and say which pass. Adds and writes nothing.

    Only the texts under the file's `eval.gold` and `eval.negative_controls` are read: none of your records.
    A gold example passes when every fact it expects comes back; a negative control passes when nothing does.
    It needs Topos running, and takes the model's time: about one call per example.
    """
    from pathlib import Path

    from topos.features.derivation import custom_packs, pack_trial

    def refuse(sentence: str, code: Optional[str] = None, exit_code: int = EXIT_FAILED) -> None:
        if as_json:
            _json_out({"ok": False, "error": sentence, **({"code": code} if code else {})})
        else:
            click.echo(f"Not tried. {sentence}", err=True)
        sys.exit(exit_code)

    try:
        pack, content = custom_packs.read_candidate(Path(file))
    except custom_packs.CustomPackError as exc:
        refuse(str(exc))
    except Exception:  # noqa: BLE001 -- a file from outside never ends this command in a traceback
        refuse(_NOT_A_PACK)
    examples = pack_trial.examples_of(pack)
    if len(examples) > pack_trial.MAX_EXAMPLES:
        refuse(f"The file holds {len(examples)} examples and one try runs at most {pack_trial.MAX_EXAMPLES}. "
               "Try it with fewer.", "too_many_examples")
    try:
        body = _node(_socket(TRY_TIMEOUT_S), "POST", TRY_PATH, json_body={"content": content.decode("utf-8")})
    except _Stop as stop:
        if stop.code == "owner_confirmation_required":
            # A try needs no approval. A node that asks for one does not know the route: its socket refuses every
            # changing request it has no entry for.
            refuse(_OLDER_NODE, "older_node")
        refuse(stop.say, stop.code, EXIT_NO_MODEL if stop.code in ("no_local_model", NO_ANSWER) else stop.exit_code)
    report = _trial_report(body)
    if report is None:
        refuse("Topos did not report the try. Check `topos-node status`.", "no_report")
    totals = report["totals"]
    if report["stopped"] == "model_unavailable" or (totals["errors"] and not totals["passed"] + totals["failed"]):
        status, code = "model_unavailable", EXIT_NO_MODEL
    elif totals["failed"]:
        status, code = "failed", EXIT_EXAMPLES_FAILED
    elif totals["errors"] or totals["skipped"] or totals["unjudged"]:
        # A gold example with no expected fact was run but not judged: it passed nothing, so neither did the try.
        status, code = "incomplete", EXIT_EXAMPLES_FAILED
    else:
        status, code = "passed", EXIT_OK
    if as_json:
        _json_out({"ok": True, "status": status, **report})
        sys.exit(code)
    click.echo(f"{report['pack_id'] or pack.pack} {report['version'] or pack.version}, tried on "
               f"{report['model'] or 'the local model'}: the file's own examples, none of your records. "
               "Nothing was added or written.")
    for row in report["examples"]:
        label = "gold" if row["kind"] == "gold" else "negative control"
        verdict = {"pass": "pass", "fail": "FAIL", "unjudged": "not judged", "error": "no answer",
                   "skipped": "not run"}[row["outcome"]]
        click.echo(f"  {label} {row['index']}: {verdict}" + (f" ({row['reason']})" if row["reason"] else "")
                   + f"  \"{_shown(row['text'])}\"")
        for wanted in row["expected"]:
            if isinstance(wanted, dict):
                click.echo(f"      expected {_shown(wanted.get('predicate'), 80)} "
                           f"{_shown_value(wanted.get('value')) if 'value' in wanted else ''}".rstrip())
        for got in row["returned"]:
            if isinstance(got, dict):
                click.echo(f"      returned {_shown(got.get('predicate'), 80)} {_shown_value(got.get('value'))}")
        if row["outcome"] in ("pass", "fail") and not row["returned"]:
            click.echo("      returned nothing")
    click.echo(f"{totals['examples']} example{'s' if totals['examples'] != 1 else ''}: {totals['passed']} passed, "
               f"{totals['failed']} failed"
               + (f", {totals['unjudged']} not judged (a gold example with no expected fact)"
                  if totals["unjudged"] else "")
               + (f", {totals['errors']} with no answer from the model" if totals["errors"] else "")
               + (f", {totals['skipped']} not run" if totals["skipped"] else "") + ".")
    if status == "model_unavailable":
        click.echo("The local model did not answer. Check `topos-node status`, then try again.", err=True)
    click.echo("A pass here is about these examples only. The check Topos makes on each fact it finds was not "
               "run, and nothing here says how the pack reads your own records.")
    sys.exit(code)


# ----------------------------------------------------------------------------- the switch, from a terminal
def _switch_path(pack_id: str) -> str:
    return f"{PACKS_PATH}/{pack_id}/switch"


def _listed(sock, pack_id: str) -> Optional[Dict[str, Any]]:
    for row in _packs(_node(sock, "GET", PACKS_PATH)):
        if row["pack_id"] == pack_id:
            return row
    return None


def _switch_stop(stop: _Stop, pack_id: str, as_json: bool) -> None:
    if as_json:
        _json_out({"ok": False, "pack_id": pack_id, "code": stop.code, "error": stop.say})
        sys.exit(stop.exit_code)
    _fail(stop.say, stop.exit_code)


@packs_group.command("on")
@click.argument("pack_id", type=PACK, metavar="ID")
@click.option("--open-terminal", is_flag=True,
              help="Without a terminal: open the question in a Terminal window on macOS; no command typing needed.")
@_JSON_OPTION
def packs_on(pack_id: str, open_terminal: bool, as_json: bool) -> None:
    """Switch a fact pack on: shows what it reads and writes, asks you once.

    A pack that writes about other people is not switched on here: its consent is asked in the app.
    """
    from topos.features.derivation import custom_packs
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.registry import bundled_pack_dir, definition_sha

    try:
        pack = load_packs(bundled_pack_dir(), only=[pack_id]).get(pack_id)
    except Exception:  # noqa: BLE001 -- a catalog that cannot be read names no pack
        pack = None
    try:
        if pack is None:
            raise _Stop("unknown_pack", f"No such pack: {pack_id}. `topos-node packs list` shows them.")
        if str(getattr(pack, "net_subject", "deny")) == "allow":
            raise _Stop("consent_in_app", f"{pack_id} writes facts about other people, so switching it on asks "
                                          "for your consent in the app (Data → Fact packs). Not switched on.")
        sock = _socket()
        row = _listed(sock, pack_id)
        if row is None:
            raise _Stop("unknown_pack", f"No such pack: {pack_id}. `topos-node packs list` shows them.")
    except _Stop as stop:
        _switch_stop(stop, pack_id, as_json)
    if row["enabled"]:
        if as_json:
            _json_out({"ok": True, "status": "already_on", "pack_id": pack_id, "enabled": True, "changed": False})
        else:
            click.echo(f"{pack_id} is already on.")
        sys.exit(EXIT_OK)
    if not _can_prompt():
        _hand_to_person(
            argv=["packs", "on", pack_id], slug="pack-on", title=f"Switch the fact pack {pack_id} on",
            open_terminal=open_terminal, as_json=as_json, doc={"pack_id": pack_id, "enabled": False},
            typed="Switching a pack on changes what Topos reads and writes about you, so it asks you first: "
                  "run this command in your own terminal.")
    for line in custom_packs.describe(pack):
        click.echo(line, err=as_json)
    click.echo(_routing_line(custom_packs.routing_reach(pack)), err=as_json)
    count = _apps_granted(pack_id)
    if count:
        click.echo(f"  {count} connected app{'s hold' if count != 1 else ' holds'} a grant that names this pack "
                   "and can read its facts once it is on.", err=as_json)
    click.echo("  A coding agent you connected and your routines need no grant: an answer Topos gives them can "
               "draw on this pack's facts.", err=as_json)
    click.echo("Switching it on: from now on Topos reads your new records for this pack and writes the facts it "
               f"finds. It reads no history by itself: `topos-node packs backfill --pack {pack_id}` does.",
               err=as_json)
    if not click.confirm("Switch it on?", default=False, err=True):
        if as_json:
            _json_out({"ok": True, "status": "cancelled", "pack_id": pack_id, "enabled": False, "changed": False})
        else:
            click.echo("Nothing changed.")
        sys.exit(EXIT_OK)
    body: Dict[str, Any] = {"enabled": True}
    shown = definition_sha(pack, stamped=False)
    if shown is not None:
        body["definition"] = shown        # a pack you added: the node switches on only what was shown above
    try:
        _node(sock, "PUT", _switch_path(pack_id), confirm=True, json_body=body)
    except _Stop as stop:
        _switch_stop(stop, pack_id, as_json)
    if as_json:
        _json_out({"ok": True, "status": "switched_on", "pack_id": pack_id, "enabled": True, "changed": True})
    else:
        click.echo(f"{pack_id} is switched on. `topos-node packs backfill --pack {pack_id}` reads your history "
                   "for it.")


@packs_group.command("off")
@click.argument("pack_id", type=PACK, metavar="ID")
@click.option("--open-terminal", is_flag=True,
              help="Without a terminal: open the question in a Terminal window on macOS; no command typing needed.")
@_JSON_OPTION
def packs_off(pack_id: str, open_terminal: bool, as_json: bool) -> None:
    """Switch a fact pack off: it stops reading. The facts it wrote are kept; asks you once."""
    try:
        sock = _socket()
        row = _listed(sock, pack_id)
        if row is None:
            raise _Stop("unknown_pack", f"No such pack: {pack_id}. `topos-node packs list` shows them.")
    except _Stop as stop:
        _switch_stop(stop, pack_id, as_json)
    if not row["enabled"]:
        if as_json:
            _json_out({"ok": True, "status": "already_off", "pack_id": pack_id, "enabled": False, "changed": False})
        else:
            click.echo(f"{pack_id} is already off.")
        sys.exit(EXIT_OK)
    if not _can_prompt():
        _hand_to_person(
            argv=["packs", "off", pack_id], slug="pack-off", title=f"Switch the fact pack {pack_id} off",
            open_terminal=open_terminal, as_json=as_json, doc={"pack_id": pack_id, "enabled": True},
            typed="A pack's switch is yours to change, so it asks you first: run this command in your own "
                  "terminal.")
    click.echo(f"Switching {pack_id} off: Topos stops reading records for it. The {row['fact_count']:,} "
               f"fact{'s' if row['fact_count'] != 1 else ''} it holds "
               f"{'are' if row['fact_count'] != 1 else 'is'} kept, and no app reads them while the pack is off. "
               f"`topos-node packs on {pack_id}` switches it on again.", err=as_json)
    if not click.confirm("Switch it off?", default=False, err=True):
        if as_json:
            _json_out({"ok": True, "status": "cancelled", "pack_id": pack_id, "enabled": True, "changed": False})
        else:
            click.echo("Nothing changed.")
        sys.exit(EXIT_OK)
    try:
        _node(sock, "PUT", _switch_path(pack_id), confirm=True, json_body={"enabled": False})
    except _Stop as stop:
        _switch_stop(stop, pack_id, as_json)
    if as_json:
        _json_out({"ok": True, "status": "switched_off", "pack_id": pack_id, "enabled": False, "changed": True})
    else:
        click.echo(f"{pack_id} is switched off.")


@packs_group.command("remove")
@click.argument("pack_id", type=PACK, metavar="ID")
def packs_remove(pack_id: str) -> None:
    """Remove a fact pack you added. The facts it already wrote are kept."""
    from topos.features.derivation import custom_packs

    try:
        removed = custom_packs.remove_pack(pack_id)
    except custom_packs.CustomPackError as exc:
        _fail(f"Not removed. {exc}")
    if not removed:
        _fail(f"{pack_id} is not a pack you added (see `topos-node packs list`).")
    click.echo(f"Removed {pack_id}. It reads nothing from now on; the facts it already wrote are kept.")


@packs_group.command("backfill")
@click.option("--pack", "pack_ids", multiple=True, type=PACK, metavar="ID", help="A pack to backfill (repeatable).")
@click.option("--all-enabled", is_flag=True, help="Every pack that is switched on.")
@click.option("--limit", type=click.IntRange(1, MAX_LIMIT), default=DEFAULT_LIMIT, show_default=True,
              help="Records per batch.")
@click.option("--until-done", is_flag=True, help="Batch after batch until each pack's history is read "
                                                 "(default: one batch per pack).")
@click.option("--since", type=click.DateTime(formats=["%Y-%m-%d"]), default=None, metavar="YYYY-MM-DD",
              help="Only records dated on or after this day (default: as far back as a backfill reaches).")
@click.option("--detach", is_flag=True, help="Start the run and return; `packs status --follow` watches it.")
def packs_backfill(pack_ids: tuple, all_enabled: bool, limit: int, until_done: bool, since, detach: bool) -> None:
    """Read the history of fact packs: shows the plan, asks you once, then the node works through it."""
    if bool(pack_ids) == bool(all_enabled):
        raise click.UsageError("Name the packs with --pack ID (repeatable), or pass --all-enabled.")
    since = since.date().isoformat() if since is not None else None
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        _fail("A backfill puts hours of local-model work on your Topos, so it asks you first: "
              "run this command in your own terminal.", EXIT_NEEDS_HUMAN)
    sock = _socket()
    listing = _request(sock, "GET", PACKS_PATH, what="the pack list")
    known = {p["pack_id"]: p for p in _packs(listing)}
    if all_enabled:
        chosen = sorted(pid for pid, p in known.items() if p["enabled"])
        if not chosen:
            click.echo("No fact packs are switched on. `topos-node packs on ID` switches one on "
                       "(or the app: Data → Fact packs).")
            sys.exit(EXIT_OK)
    else:
        chosen = list(dict.fromkeys(pack_ids))
        unknown = [pid for pid in chosen if pid not in known]
        off = [pid for pid in chosen if pid in known and not known[pid]["enabled"]]
        if unknown:
            _fail(f"No such pack: {', '.join(unknown)}. `topos-node packs list` shows them.")
        if off:
            _fail(f"Not switched on: {', '.join(off)}. `topos-node packs on ID` switches a pack on "
                  "(or the app: Data → Fact packs).")

    active = _run(_request(sock, "GET", CURRENT_PATH, what="the backfill status"))
    yields = active is not None and active["state"] in _ACTIVE and active["origin"] == "auto"
    if active is not None and active["state"] in _ACTIVE and not yields:
        _fail("A backfill run is already working. `topos-node packs status --follow` watches it; "
              "`topos-node packs stop` stops it.")

    params: Dict[str, Any] = {"pack_id": chosen}
    if since:
        params["since"] = since
    plan = _request(sock, "GET", PLAN_PATH, what="the backfill count", params=params)
    rows = {str(row.get("pack_id")): row for row in plan.get("packs") or [] if isinstance(row, dict)}
    waiting = {pid: _int(rows.get(pid, {}).get("waiting")) for pid in chosen}
    extraction = plan.get("extraction") if isinstance(plan.get("extraction"), dict) else {}
    llm_model = _model(extraction.get("llm_model")) or "the local extraction model"
    classifier = _model(extraction.get("classifier_model")) if extraction.get("provider") == "topos_facts" else None
    verifier = _model(plan.get("verify_model"))

    def reader(pid: str) -> str:
        route = _word(rows.get(pid, {}).get("extract_route")) or "llm"
        if route == "classifier":
            return _model(rows.get(pid, {}).get("extract_model")) or classifier or "Topos Facts"
        if route == "unknown":
            return f"{classifier or 'Topos Facts'} if it knows this pack, else {llm_model}"
        return llm_model

    routes = {pid: _word(rows.get(pid, {}).get("extract_route")) or "llm" for pid in chosen}
    total = sum(waiting.values())
    width = max(len(pid) for pid in chosen)
    click.echo("Fact-pack backfill" + (f", records dated {since} or later" if since else ""))
    for pid in chosen:
        click.echo(f"  {pid.ljust(width)}  {waiting[pid]:>7,} records waiting   read by {reader(pid)}")
    window = _int(plan.get("history_window"))
    click.echo(f"{len(chosen)} pack{'s' if len(chosen) != 1 else ''}, {total:,} records waiting"
               + (f" (counted over the newest {window:,} journal entries and the newest {window:,} messages"
                  + (f" dated {since} or later" if since else "") + ")." if window else "."))
    if plan.get("history_truncated"):
        click.echo(f"There is more history than one backfill walks: records older than the newest {window:,} "
                   "of each kind are not counted and not reached.")
    if not total:
        click.echo("Nothing is waiting for these packs.")
        sys.exit(EXIT_OK)
    this_run = {pid: (waiting[pid] if until_done else min(limit, waiting[pid])) for pid in chosen}
    to_do = sum(this_run.values())
    if until_done:
        click.echo(f"Batches of {limit:,} records, one pack after another, until each pack is done.")
    else:
        click.echo(f"One batch of up to {limit:,} records per pack, one pack after another: "
                   f"{to_do:,} records this run. --until-done reads them all.")

    def share(route: str) -> str:
        packs = [pid for pid in chosen if routes[pid] == route]
        return f"{len(packs)} pack{'s' if len(packs) != 1 else ''}, {sum(this_run[pid] for pid in packs):,} records"

    by_llm = sum(this_run[pid] for pid in chosen if routes[pid] == "llm")
    split_known = not any(route == "unknown" for route in routes.values())
    if classifier is None:
        click.echo(f"Extraction: the local model {llm_model} reads every record, one call each.")
    elif not split_known:
        click.echo(f"Extraction: Topos Facts ({classifier}) is picked for facts, but its pack list is not on this "
                   f"computer yet. The packs it knows go to it; the rest go to the local model {llm_model}.")
    else:
        click.echo(f"Extraction: Topos Facts ({classifier}) reads {share('classifier')}; "
                   f"the local model {llm_model} reads {share('llm')}.")
    click.echo(f"Verification: every fact found is checked by {verifier}, one call each." if verifier
               else "Verification: off on this node.")
    per_call = _call_seconds(listing, "extraction")
    if per_call and by_llm and split_known:
        click.echo(f"At this node's 30-day average of {per_call:.1f} s per extraction call, the {by_llm:,} records "
                   f"on the local model are about {_duration(by_llm * per_call)}.")
    if classifier is not None:
        click.echo("Topos Facts has no measured time on this node yet; the run reports it per batch.")
    per_check = _call_seconds(listing, "verification")
    if per_check and verifier:
        click.echo(f"The verifier's 30-day average is {per_check:.1f} s per call; how many calls depends on "
                   "how many facts are found.")
    click.echo("The node does the work and this terminal follows it. Ctrl-C stops it after the record in hand; "
               "the same command later picks up where it stopped.")
    if yields:
        click.echo("The automatic catch-up is working right now. Your run wins: it stops after the record in "
                   "hand, and your run starts then.")
    if not click.confirm("Run it?", default=False, err=True):
        click.echo("Nothing started.")
        sys.exit(EXIT_OK)

    from topos.mcp_server import NodeDown, NodeRefused
    from topos.uds import write_owner_confirmation

    try:
        started = sock.request_sync("POST", RUNS_PATH,
                                    json_body={"pack_ids": chosen, "limit": limit, "until_done": until_done,
                                               "since": since},
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
    auto = _auto_status(sock)
    if as_json:
        click.echo(json.dumps({"run": run, "auto": auto}, indent=2, sort_keys=True))
        return
    if run is None:
        click.echo("No backfill run since the node started.")
        for line in _auto_lines(auto) if auto is not None else []:
            click.echo(line)
        return
    if follow and run["state"] in _ACTIVE:
        sys.exit(_follow(sock, run["run_id"]))
    click.echo(f"Run {run['run_id']}" + (" (automatic catch-up)" if run["origin"] == "auto" else "")
               + f": {run['state']}" + (f" ({run['reason']})" if run["reason"] else "")
               + f", started {run['started_at'] or '?'} UTC, {_duration(run['elapsed_s'])}"
               + (f", records dated {run['since']} or later." if run["since"] else "."))
    for pack in run["packs"]:
        done = {k: sum(b[k] for b in pack["batches"]) for k in _COUNT_KEYS + _TIME_KEYS}
        line = f"  {pack['pack_id']}: {pack['state']}"
        if pack["error"]:
            line += f" ({pack['error']})"
        if pack["batches"]:
            line += (f", {len(pack['batches'])} batch{'es' if len(pack['batches']) != 1 else ''}: "
                     f"{_counts_line(done)}{_times(done)}")
        if pack["current"]:
            line += f"; batch {pack['current']['index']} in flight, {pack['current']['processed']:,} so far"
        if pack["extract_model"]:
            line += f"; read by {pack['extract_model']}"
        click.echo(line)
    click.echo(f"Total: {_counts_line(run['totals'])}{_times(run['totals'])}.")
    for line in _auto_lines(auto) if auto is not None else []:
        click.echo(line)


@packs_group.command("stop")
def packs_stop() -> None:
    """Stop the backfill run after the record in hand. Finished records are kept."""
    body = _request(_socket(), "POST", STOP_PATH, what="the stop")
    if body.get("stopping"):
        click.echo("Stopping after the record in hand. `topos-node packs status` shows when it has stopped.")
    else:
        click.echo("No backfill run is active.")


# ----------------------------------------------------------------------------- the standing switch
@packs_group.group("auto")
def packs_auto() -> None:
    """Keep the switched-on fact packs current without a backfill each time."""


@packs_auto.command("status")
@_JSON_OPTION
def packs_auto_status(as_json: bool) -> None:
    """Whether the automatic catch-up is on, how its last look ended and when the next may start."""
    auto = _auto(_request(_socket(), "GET", AUTO_PATH, what="the automatic catch-up status"))
    if as_json:
        click.echo(json.dumps({"auto": auto}, indent=2, sort_keys=True))
        return
    if auto is None:
        _fail("Topos did not report the automatic catch-up. Check `topos-node status`.")
    for line in _auto_lines(auto):
        click.echo(line)


def _set_auto(sock, body: Dict[str, Any]) -> Dict[str, Any]:
    """Send the one confirmed change; the node's answer as a projection."""
    from topos.mcp_server import NodeDown, NodeRefused
    from topos.uds import write_owner_confirmation

    try:
        answer = sock.request_sync("PUT", AUTO_PATH, json_body=body,
                                   headers={"X-Topos-Owner-Confirm": write_owner_confirmation()})
    except NodeDown:
        _fail("Topos stopped before the change was made. Check `topos-node status`.", EXIT_NODE_DOWN)
    except NodeRefused as exc:
        _fail(f"Topos refused the change ({exc.code or exc.status}). Nothing changed.")
    auto = _auto(answer)
    if auto is None:
        _fail("Topos did not report the automatic catch-up. Check `topos-node packs auto status`.")
    return auto


@packs_auto.command("on")
@click.option("--quiet-minutes", type=click.IntRange(1, 24 * 60), default=None,
              help="How long nothing new must have arrived before a look (default: 10, or as set before).")
@click.option("--min-gap-minutes", type=click.IntRange(5, 7 * 24 * 60), default=None,
              help="The least time between two looks (default: 60, or as set before).")
def packs_auto_on(quiet_minutes: Optional[int], min_gap_minutes: Optional[int]) -> None:
    """Turn the automatic catch-up on: says what it will do, asks you once."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        _fail("Turning this on lets your Topos start fact-pack runs by itself, so it asks you first: "
              "run this command in your own terminal.", EXIT_NEEDS_HUMAN)
    sock = _socket()
    auto = _auto(_request(sock, "GET", AUTO_PATH, what="the automatic catch-up status"))
    if auto is None:
        _fail("Topos did not report the automatic catch-up. Check `topos-node status`.")
    if auto["enabled"] and quiet_minutes is None and min_gap_minutes is None:
        click.echo("Already on.")
        for line in _auto_lines(auto)[1:]:
            click.echo(line)
        sys.exit(EXIT_OK)
    quiet_s = quiet_minutes * 60 if quiet_minutes is not None else auto["quiet_s"]
    gap_s = min_gap_minutes * 60 if min_gap_minutes is not None else auto["min_gap_s"]
    listing = _request(sock, "GET", PACKS_PATH, what="the pack list")
    packs = _packs(listing)
    on = sorted(p["pack_id"] for p in packs if p["enabled"])
    days = auto["first_window_days"] or 7
    click.echo("Automatic catch-up for fact packs")
    click.echo(f"  {len(on)} of {len(packs)} packs are switched on. This keeps the switched-on ones current; it "
               "switches none on or off.")
    click.echo(f"  When new journal entries or messages have landed, from any source, and none has arrived for "
               f"{_duration(quiet_s)}, Topos reads them for those packs: at most one look every {_duration(gap_s)}.")
    click.echo(f"  The first look reads the last {days} days; after that, what landed since the last one"
               + (f", never more than {auto['max_window_days']} days back" if auto["max_window_days"] else "")
               + ". Older history stays `topos-node packs backfill`.")
    if on and not auto["enabled"]:
        import datetime as _dt

        since = (_dt.datetime.now(_dt.timezone.utc).date() - _dt.timedelta(days=days)).isoformat()
        plan = _request(sock, "GET", PLAN_PATH, what="the count", params={"pack_id": on, "since": since})
        extraction = plan.get("extraction") if isinstance(plan.get("extraction"), dict) else {}
        llm_model = _model(extraction.get("llm_model")) or "the local extraction model"
        classifier = _model(extraction.get("classifier_model")) if extraction.get("provider") == "topos_facts" else None
        verifier = _model(plan.get("verify_model"))
        click.echo(f"  Waiting now among records dated {since} or later: {_int(plan.get('total_waiting')):,}.")
        click.echo("  Read by " + (f"Topos Facts ({classifier}) for the packs it knows and the local model "
                                   f"{llm_model} for the rest" if classifier else f"the local model {llm_model}")
                   + ("; every fact found is checked by " + verifier + "." if verifier
                      else "; verification is off on this node."))
    click.echo("  It uses the models you have picked and changes no setting. It starts no run beside another "
               "backfill, a backfill you start stops it after the record in hand, and it gives way to your own "
               "requests between records. No model is loaded when nothing is waiting.")
    click.echo("  `topos-node packs auto off` turns it off; `topos-node packs status` shows what it did.")
    if not click.confirm("Turn it on?" if not auto["enabled"] else "Change it?", default=False, err=True):
        click.echo("Nothing changed.")
        sys.exit(EXIT_OK)
    body: Dict[str, Any] = {"enabled": True}
    if quiet_minutes is not None:
        body["quiet_s"] = quiet_s
    if min_gap_minutes is not None:
        body["min_gap_s"] = gap_s
    for line in _auto_lines(_set_auto(sock, body)):
        click.echo(line)


@packs_auto.command("off")
def packs_auto_off() -> None:
    """Turn the automatic catch-up off. A run it started stops after the record in hand."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        _fail("This setting is yours to change, so it asks you first: run this command in your own terminal. "
              "(`topos-node packs stop` stops a run that is working and needs no terminal.)", EXIT_NEEDS_HUMAN)
    sock = _socket()
    auto = _auto(_request(sock, "GET", AUTO_PATH, what="the automatic catch-up status"))
    if auto is None:
        _fail("Topos did not report the automatic catch-up. Check `topos-node status`.")
    if not auto["enabled"]:
        click.echo("Already off.")
        sys.exit(EXIT_OK)
    click.echo("Turning the automatic catch-up off: new records are then read for the fact packs only when "
               "you run `topos-node packs backfill` (or where a source's own lane reads them). "
               "A run it started stops after the record in hand; what it read is kept.")
    if not click.confirm("Turn it off?", default=False, err=True):
        click.echo("Nothing changed.")
        sys.exit(EXIT_OK)
    for line in _auto_lines(_set_auto(sock, {"enabled": False})):
        click.echo(line)


# ----------------------------------------------------------------------------- facts written from reactions
def _reaction_facts(body: Any) -> Optional[Dict[str, Any]]:
    """The count of facts written from the owner's reactions, reduced to pack ids and counts."""
    if not isinstance(body, dict) or not isinstance(body.get("packs"), list):
        return None
    packs = [{"pack_id": str(row["pack_id"]), "facts": _int(row.get("facts"))}
             for row in body["packs"]
             if isinstance(row, dict) and _PACK_ID_RE.match(str(row.get("pack_id") or "")) and _int(row.get("facts"))]
    out = {"packs": packs, "total": sum(p["facts"] for p in packs), "kept_confirmed": _int(body.get("kept_confirmed")),
           "kept_marked": _int(body.get("kept_marked")), "kept_other_record": _int(body.get("kept_other_record")),
           "kept_timing": _int(body.get("kept_timing"))}
    for key in ("retracted", "changed"):
        if key in body:
            out[key] = _int(body.get(key))
    return out


def _reaction_lines(found: Dict[str, Any], verb: str) -> List[str]:
    lines = []
    if found["packs"]:
        width = max(len(p["pack_id"]) for p in found["packs"])
        lines = [f"  {p['pack_id'].ljust(width)}  {p['facts']:>5,}" for p in found["packs"]]
    total = found["total"]
    lines.append(f"{total:,} fact{'s' if total != 1 else ''} in {len(found['packs'])} "
                 f"pack{'s' if len(found['packs']) != 1 else ''} {verb}.")
    kept = found["kept_confirmed"]
    if kept:
        lines.append(f"{kept:,} more of the same kind {'is' if kept == 1 else 'are'} left as "
                     f"{'it is' if kept == 1 else 'they are'}: you confirmed, corrected or added "
                     f"{'it' if kept == 1 else 'them'} in the app.")
    marked = found["kept_marked"]
    if marked:
        lines.append(f"{marked:,} more of the same kind {'is' if marked == 1 else 'are'} left as "
                     f"{'it is' if marked == 1 else 'they are'}: you marked {'it' if marked == 1 else 'them'} "
                     "in an app (a reaction or a rank).")
    other = found["kept_other_record"]
    if other:
        lines.append(f"{other:,} more {'names' if other == 1 else 'name'} only a reaction and "
                     f"{'is' if other == 1 else 'are'} left as {'it is' if other == 1 else 'they are'}: Topos also "
                     f"stored {'it' if other == 1 else 'them'} from another record, or cannot tell that it did not.")
    timing = found["kept_timing"]
    if timing:
        lines.append(f"{timing:,} more {'names' if timing == 1 else 'name'} only reactions and "
                     f"{'is' if timing == 1 else 'are'} left as {'it is' if timing == 1 else 'they are'}: "
                     f"{'it is' if timing == 1 else 'they are'} about when you send messages, not what they say.")
    return lines


@packs_group.command("retract-reactions")
@click.option("--apply", "apply", is_flag=True,
              help="End those facts. Asks you once, on a terminal (default: count them and change nothing).")
@_JSON_OPTION
def packs_retract_reactions(apply: bool, as_json: bool) -> None:
    """Facts stored from a reaction you sent in Messages: counts them per pack; --apply ends them.

    Ending puts nothing back. If one of these facts took the place of an earlier fact, the earlier
    fact stays closed. If a reaction changed a detail of a fact you also wrote yourself, that fact is
    left as it is, with the changed detail.
    """
    if apply and as_json:
        raise click.UsageError("--json is for the count; --apply asks you on a terminal.")
    if apply and not (sys.stdin.isatty() and sys.stdout.isatty()):
        _fail("Ending facts changes what your Topos holds to be true, so it asks you first: "
              "run this command in your own terminal.", EXIT_NEEDS_HUMAN)
    sock = _socket()
    found = _reaction_facts(_request(sock, "GET", REACTION_FACTS_PATH, what="the count"))
    if found is None:
        _fail("Topos did not report the count. Check `topos-node status`.")
    if as_json:
        click.echo(json.dumps({"reaction_facts": found}, indent=2, sort_keys=True))
        return
    click.echo("Facts stored from a reaction you sent in Messages")
    click.echo("  A reaction quotes the message it reacts to, so its words are the other person's. A fact counts "
               "here when every record it was read from is such a reaction; a fact with any other source does not.")
    for line in _reaction_lines(found, "standing"):
        click.echo(line)
    if not found["total"]:
        click.echo("Nothing to end.")
        sys.exit(EXIT_OK)
    if not apply:
        click.echo("Nothing changed. `topos-node packs retract-reactions --apply` ends them, after asking you.")
        sys.exit(EXIT_OK)
    click.echo("Ending a fact closes it as of now: it stays in the history, no row is deleted and no message is "
               "touched. A reaction is not read again, so a later backfill does not bring these back.")
    click.echo("Ending puts nothing back. If one of these facts took the place of an earlier fact, the earlier "
               "fact stays closed. If a reaction changed a detail of a fact you also wrote yourself, that fact is "
               "left as it is, with the changed detail.")
    if not click.confirm("End them?", default=False, err=True):
        click.echo("Nothing changed.")
        sys.exit(EXIT_OK)

    from topos.mcp_server import NodeDown, NodeRefused
    from topos.uds import write_owner_confirmation

    try:
        answer = sock.request_sync("POST", RETRACT_PATH, json_body={"expected_total": found["total"]},
                                   headers={"X-Topos-Owner-Confirm": write_owner_confirmation()})
    except NodeDown:
        _fail("Topos stopped before the change was made. Check `topos-node status`.", EXIT_NODE_DOWN)
    except NodeRefused as exc:
        if exc.code == "reaction_facts_changed":
            _fail("The count changed while you were asked. Nothing changed; run the command again.")
        _fail(f"Topos refused the change ({exc.code or exc.status}). Nothing changed.")
    done = _reaction_facts(answer)
    if done is None:
        _fail("Topos did not report what it ended. `topos-node packs retract-reactions` counts what is left.")
    for line in _reaction_lines(done, "ended"):
        click.echo(line)
    if done.get("changed"):
        click.echo(f"{done['changed']:,} changed while this ran and "
                   f"{'was' if done['changed'] == 1 else 'were'} left; run the command again to count.")


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
    said_waiting = False
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
            if run["waiting_for"] and not said_waiting:
                said_waiting = True
                click.echo("Waiting for the automatic catch-up to finish the record in hand...")
            for pack in run["packs"]:
                pid = pack["pack_id"]
                if pack["state"] != "waiting" and pid not in announced:
                    announced.add(pid)
                    click.echo(f"{pid}:")
                if pack["extract_model"] and (pid, "models") not in announced:
                    announced.add((pid, "models"))
                    click.echo(f"  read by {pack['extract_model']}"
                               + (f", checked by {pack['verify_model']}" if pack["verify_model"] else ""))
                for batch in pack["batches"][printed.get(pid, 0):]:
                    click.echo(f"  batch {batch['index']}: {_counts_line(batch)}, "
                               f"{_duration(batch['elapsed_s'])}{_times(batch)}")
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
    totals = f"{_counts_line(run['totals'])} in {_duration(run['elapsed_s'])}{_times(run['totals'])}"
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
    if run["reason"] == "topos_facts_unavailable":
        click.echo("Not run: Topos Facts is picked for facts on this node and could not load, so nothing was read "
                   "or marked.\nCheck Settings → Models → Node functions → Facts extraction, then run the same "
                   "command.", err=True)
    elif run["reason"] == "model_unavailable":
        click.echo(f"Stopped: the local model failed several extraction calls in a row. {totals}.\n"
                   "Check `topos-node status`, then run the same command: finished records are kept.", err=True)
    else:
        click.echo(f"The run failed ({run['reason'] or run['state']}): {totals}. "
                   "Finished records are kept; the node log has the error.", err=True)
    return EXIT_FAILED
