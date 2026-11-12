"""`topos-node sources list|preview|enable|pause|resume|sync SOURCE` — sources by id, from a terminal.

A thin client of the socket-only owner doors (``POST /api/local/owner/sources/{id}/<verb>``,
``api/owner_sources.py``). ``SOURCE`` is the registry id the app uses (``imessage``,
``claude_code_sessions``) or any name ``sources/setup_catalog.py`` knows for it ("iMessage Sync",
"Browser History Plugin"), so a setup prompt's checkbox line resolves to the same source a person
would pick in the app. Three kinds, three ways of turning one on:

* the coding tools' own transcripts (§2.8): the plugin's hooks call ``sync --session PATH --quiet``
  after every agent turn; a human or the agent calls ``preview`` and ``enable`` once;
* iMessage and Signal Desktop: other people's words, so ``enable`` shows the counts and asks the
  person on a terminal, and each request carries the one-change confirmation the socket needs
  (``preview``, ``resume`` and ``sync`` too). Without a terminal (a coding agent's shell) these
  exit 2 and say so; ``enable --open-terminal`` opens the window where the question is waiting;
* a browser extension's sources: nothing the node can install. ``enable`` says whether the
  extension is connected and, when it is not, the three steps that are the person's.

Exit codes: 0 ok, 1 refused, 2 a person is needed (``human.say`` in ``--json``), 3 not paired,
4 node down (CONTRACTS §4).

What is printed is a projection — counts, status words, the source id — never the door's response
body, which on a sync carries a ledger and may one day carry more. The guard tests plant a secret in
every body to hold that line; the sentences a person reads are written here, keyed by the door's code.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Callable, Optional

import click

from topos.mcp_server import NODE_DOWN, NodeDown, NodeRefused, NodeSocket
from topos.runtime_platform import INSTALL_PYTHON
from topos.sources import setup_catalog
from topos.sources.setup_catalog import KIND_EXTENSION, KIND_MESSAGES, SetupSource

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_NEEDS_HUMAN = 2
EXIT_NOT_PAIRED = 3
EXIT_NODE_DOWN = 4

_SOURCE_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_JSON_OPTION = click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
_QUIET_OPTION = click.option("--quiet", is_flag=True, help="Print nothing on success (hooks).")
_SKIP_OPTION = click.option("--skip", "skip", multiple=True, metavar="DIR", help="Project folder name to leave out (repeatable).")


class _SourceId(click.ParamType):
    name = "source"

    def convert(self, value, param, ctx):
        text = str(value or "").strip()
        entry = setup_catalog.resolve(text)
        if entry is not None:
            return entry.source_id
        if not _SOURCE_ID_RE.match(text):
            self.fail(f"{value!r} is not a source this Topos knows. `topos-node sources list` shows them.", param, ctx)
        # An id the catalog does not name: the door answers for it (404 unknown_source).
        return text


SOURCE = _SourceId()


def _ints(mapping: Any) -> dict[str, int]:
    if not isinstance(mapping, dict):
        return {}
    return {str(k): v for k, v in mapping.items() if isinstance(v, int) and not isinstance(v, bool)}


def _word(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text if re.match(r"^[a-z0-9_]{1,32}$", text) else None


def _door(
    source_id: str,
    action: str,
    body: dict,
    *,
    as_json: bool,
    quiet: bool,
    project: Callable[[dict], tuple[dict, str]],
) -> None:
    sock = NodeSocket()
    try:
        result = sock.request_sync("POST", f"/api/local/owner/sources/{source_id}/{action}", json_body=body)
    except NodeDown:
        click.echo(NODE_DOWN, err=True)
        sys.exit(EXIT_NODE_DOWN)
    except NodeRefused as exc:
        if exc.code == "not_paired":
            click.echo("This computer isn't paired with a Topos yet. Run `topos-node pair`.", err=True)
            sys.exit(EXIT_NOT_PAIRED)
        if quiet and exc.status == 409:
            # The plugin's hook after every turn: a paused or not-yet-enabled
            # source is the owner's choice, not an error worth a line anywhere.
            sys.exit(EXIT_OK)
        click.echo(f"Topos refused `{action}` for {source_id}: {exc.code}.", err=True)
        sys.exit(EXIT_REFUSED)
    payload, human = project(result)
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    elif not quiet:
        click.echo(human)
    sys.exit(EXIT_OK)


def _human_bytes(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f} GB"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    if n >= 1_000:
        return f"{n / 1_000:.1f} KB"
    return f"{n} B"


# --- sources that are not a coding tool's transcripts -----------------------------------

_STAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ][0-9:.+\-Z]{5,32}$")

#: Refusals a person can do something about: exit 2, with the sentence. Any other code is exit 1.
_NEEDS_HUMAN_CODES = frozenset({
    "full_disk_access_needed", "signal_key_unavailable", "signal_reader_missing",
    "owner_confirmation_required", "disk_floor", "extension_setup",
})


def _can_prompt() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _stamp(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text if _STAMP_RE.match(text) else None


def _node_program() -> str:
    """The program macOS must be told about when Topos runs as a background service: this install's Python."""
    import os

    return os.path.realpath(sys.executable)


def _sentence(code: str, entry: SetupSource) -> str:
    """What a person reads for a refusal, written here from the door's code (never the door's own text)."""
    name = entry.name
    if code == "full_disk_access_needed":
        return (
            "macOS is not letting Topos read Messages yet. Open System Settings, Privacy & Security, "
            "Full Disk Access. If you use the Topos app, switch on Topos there. If Topos runs as a "
            f"background service, add this program instead: {_node_program()} . Then restart Topos "
            f"and run `topos-node sources enable {entry.source_id}` again."
        )
    if code == "signal_key_unavailable":
        return (
            "macOS did not give Topos Signal's key. When the Keychain asks to let Topos use "
            "\"Signal Safe Storage\", choose Allow, then run this again."
        )
    if code == "signal_reader_missing":
        return (
            "This install of Topos cannot open Signal's database. Reinstall it with the Signal reader and "
            f"restart Topos: uv tool install --force --python {INSTALL_PYTHON} 'topos-node[signal]'"
        )
    if code == "signal_not_found":
        return "Signal Desktop has no history on this computer. Install Signal Desktop and link it to your phone first."
    if code == "messages_not_found":
        return "There is no Messages history on this Mac to read."
    if code == "macos_only":
        return "Messages can only be read on a Mac."
    if code == "owner_confirmation_required":
        return (
            f"Turning on {name} needs a yes from you, because it holds other people's messages. Run "
            f"`topos-node sources enable {entry.source_id}` in your own terminal."
        )
    if code == "disk_floor":
        return "This computer is too low on disk space to add a source. Free some space, then try again."
    if code == "source_not_enabled":
        return f"{name} isn't turned on. Run `topos-node sources enable {entry.source_id}` first."
    if code == "not_paired":
        return "Topos has no identity on this computer yet. Start it once (`topos-node service install`), then try again."
    return f"Topos refused this for {name}: {code}."


def _stop(entry: SetupSource, code: str, *, as_json: bool, extra: Optional[dict] = None) -> None:
    """End on a refusal: exit 2 with ``human.say`` when a person can fix it, exit 1 otherwise."""
    say = _sentence(code, entry)
    needs_human = code in _NEEDS_HUMAN_CODES
    if as_json:
        payload = {"source_id": entry.source_id, "status": "needs_human" if needs_human else "refused",
                   "code": code, "human": {"say": say}, **(extra or {})}
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        click.echo(say, err=True)
    sys.exit(EXIT_NEEDS_HUMAN if needs_human else EXIT_REFUSED)


def _call(entry: SetupSource, method: str, path: str, *, as_json: bool, body: Optional[dict] = None,
          confirm: bool = False) -> dict:
    """One owner-socket call for a messenger or extension source; a refusal ends the command through ``_stop``."""
    headers = None
    if confirm:
        from topos.uds import write_owner_confirmation

        headers = {"X-Topos-Owner-Confirm": write_owner_confirmation()}
    try:
        return NodeSocket().request_sync(method, path, json_body=body, headers=headers)
    except NodeDown:
        click.echo(NODE_DOWN, err=True)
        sys.exit(EXIT_NODE_DOWN)
    except NodeRefused as exc:
        if exc.status == 404:
            click.echo("This Topos is older than this command. Update it (`uv tool upgrade topos-node`), "
                       "restart it, then try again.", err=True)
            sys.exit(EXIT_REFUSED)
        if exc.code == "not_paired":
            click.echo(_sentence("not_paired", entry), err=True)
            sys.exit(EXIT_NOT_PAIRED)
        _stop(entry, str(exc.code), as_json=as_json)
    raise AssertionError  # unreachable


def _status(entry: SetupSource, *, as_json: bool) -> dict:
    return _call(entry, "GET", f"/api/local/owner/sources/{entry.source_id}", as_json=as_json)


def _echo(payload: dict, human: str, *, as_json: bool, quiet: bool = False) -> None:
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    elif not quiet:
        click.echo(human)


def _window_words(backfill: str) -> str:
    return "the last 30 days" if backfill == "30d" else "everything on this computer"


def _needs_terminal(entry: SetupSource, action: str, *, as_json: bool) -> None:
    """End with exit 2: this verb opens a message store, so a person runs it on a terminal."""
    say = f"Run `topos-node sources {action} {entry.source_id}` in your own terminal."
    if as_json:
        click.echo(json.dumps({
            "source_id": entry.source_id, "status": "needs_human", "code": "owner_confirmation_required",
            "human": {"say": say},
        }, indent=2, sort_keys=True))
    else:
        click.echo(f"This needs you: {say[0].lower()}{say[1:]}", err=True)
    sys.exit(EXIT_NEEDS_HUMAN)


def _messenger_counts(entry: SetupSource, backfill: str, *, as_json: bool) -> tuple[dict, str]:
    """The preview door's counts and the sentence that says them. A person on a terminal asked for them:
    counting snapshots the store, so the door takes it only with the confirmation."""
    result = _call(entry, "POST", f"/api/local/owner/sources/{entry.source_id}/preview",
                   as_json=as_json, body={"backfill": backfill}, confirm=True)
    messages = int(result.get("messages") or 0)
    in_window = int(result.get("in_window") or 0)
    oldest, newest = _stamp(result.get("oldest")), _stamp(result.get("newest"))
    payload = {"source_id": entry.source_id, "messages": messages, "in_window": in_window,
               "skipped_spam": int(result.get("skipped_spam") or 0), "backfill": backfill,
               "oldest": oldest, "newest": newest}
    span = f", {oldest[:10]} to {newest[:10]}" if oldest and newest else ""
    human = (f"{entry.name}: {messages:,} messages on this computer{span}. "
             f"{_window_words(backfill).capitalize()}: {in_window:,}.")
    return payload, human


def _extension_steps(entry: SetupSource) -> tuple[str, str]:
    """(the three steps that are the person's, the command the extension's settings page completes)."""
    command = (f"topos-node pushers add {entry.app} --sources {','.join(entry.app_sources)} "
               "--origin <the extension origin its settings page shows>")
    say = (
        f"{entry.name} comes from the {entry.app_name} browser extension, so three steps are yours. "
        f"1. Add the extension to your browser: {entry.install_url} . "
        "2. Open the extension's settings and choose the local Topos option (no account). It shows a "
        "`topos-node pushers add` command: run that command in your own terminal and answer yes. "
        "3. Paste the key the command prints into the extension's settings and press Connect. "
        "If the settings have no local Topos option, the extension needs updating first."
    )
    return say, command


def _enable_extension(entry: SetupSource, *, as_json: bool, quiet: bool) -> None:
    status = _status(entry, as_json=as_json)
    if status.get("connected") is True:
        _echo({"source_id": entry.source_id, "status": "connected", "app": entry.app},
              f"{entry.name}: the {entry.app_name} extension is connected to this Topos.",
              as_json=as_json, quiet=quiet)
        sys.exit(EXIT_OK)
    say, command = _extension_steps(entry)
    if as_json:
        click.echo(json.dumps({
            "source_id": entry.source_id, "status": "needs_human", "code": "extension_setup", "app": entry.app,
            "app_sources": list(entry.app_sources), "install_url": entry.install_url, "command": command,
            "human": {"say": say},
        }, indent=2, sort_keys=True))
    else:
        click.echo(say, err=True)
    sys.exit(EXIT_NEEDS_HUMAN)


def _enable_messenger(entry: SetupSource, backfill: str, *, open_terminal: bool, as_json: bool, quiet: bool) -> None:
    sid = entry.source_id
    if not _can_prompt():
        # Look before opening a window that could only fail, and before telling a person to type a command.
        status = _status(entry, as_json=as_json)
        if status.get("enabled") is True:
            _echo({"source_id": sid, "status": "enabled"}, f"{entry.name} is already on.", as_json=as_json, quiet=quiet)
            sys.exit(EXIT_OK)
        readiness = status.get("readiness") if isinstance(status.get("readiness"), dict) else {}
        needs = _word(readiness.get("code"))
        if needs:
            _stop(entry, needs, as_json=as_json)
        if not open_terminal:
            _stop(entry, "owner_confirmation_required", as_json=as_json,
                  extra={"hint": "re-run with --open-terminal to open the question in a Terminal window"})
        from topos.cli.terminal_window import open_terminal_window

        opened = open_terminal_window(
            slug=f"source-{sid}", title=f"Turn on {entry.name} in the Topos on this computer",
            argv=["sources", "enable", sid, "--backfill", backfill],
            done="Done. Return to your coding assistant.",
            not_done="Not turned on. Return to your coding assistant.",
        )
        if not opened:
            _stop(entry, "owner_confirmation_required", as_json=as_json)
        _echo({"source_id": sid, "status": "approval_pending"},
              f"Opened the {entry.name} question in Terminal. Answer it there, then return here.",
              as_json=as_json, quiet=quiet)
        sys.exit(EXIT_OK)

    _counts, counted = _messenger_counts(entry, backfill, as_json=as_json)
    # Everything a person reads before the question goes to stderr so `--json` stdout stays one document.
    click.echo(counted, err=True)
    click.echo(f"{entry.reads} Topos keeps them on this computer. A coding assistant you connected can then "
               "read them through Topos, and what it reads is sent to its AI company.", err=True)
    if not click.confirm(f"Turn on {entry.name} and read {_window_words(backfill)}?", default=False, err=True):
        click.echo("Not turned on.", err=True)
        sys.exit(EXIT_NEEDS_HUMAN)
    result = _call(entry, "POST", f"/api/local/owner/sources/{sid}/enable", as_json=as_json,
                   body={"backfill": backfill}, confirm=True)
    scheduled = result.get("scheduled") is True
    after = (", then every hour." if scheduled else
             f". It is not read on a schedule: run `topos-node sources sync {sid}` when you want newer messages.")
    _echo({"source_id": sid, "status": "enabled", "backfill": backfill, "scheduled": scheduled},
          f"{entry.name} is on; reading {_window_words(backfill)} now{after}", as_json=as_json, quiet=quiet)
    sys.exit(EXIT_OK)


def _confirmed_messenger_verb(entry: SetupSource, action: str, question: str, done: str, *,
                              as_json: bool, quiet: bool) -> None:
    """``resume`` and ``sync`` of a messenger source: a yes on a terminal, then the door with the confirmation."""
    if not _can_prompt():
        _needs_terminal(entry, action, as_json=as_json)
    if not click.confirm(question, default=False, err=True):
        sys.exit(EXIT_NEEDS_HUMAN)
    result = _call(entry, "POST", f"/api/local/owner/sources/{entry.source_id}/{action}", as_json=as_json, confirm=True)
    payload: dict = {"source_id": entry.source_id, "status": done}
    human = f"{entry.name} {done}."
    if result.get("job_id"):
        status = "already_running" if result.get("already_running") else "queued"
        payload = {"source_id": entry.source_id, "status": status, "job_id": str(result["job_id"])}
        human = f"{entry.source_id} sync {status.replace('_', ' ')} (job {result['job_id']})."
    _echo(payload, human, as_json=as_json, quiet=quiet)
    sys.exit(EXIT_OK)


def _other_kind(source_id: str) -> Optional[SetupSource]:
    """The catalog entry when ``source_id`` is a messenger or an extension source; None for a coding tool."""
    entry = setup_catalog.BY_ID.get(source_id)
    return entry if entry is not None and entry.kind in (KIND_MESSAGES, KIND_EXTENSION) else None



@click.group("sources")
def sources_group() -> None:
    """List the sources this Topos can turn on, and preview, enable, pause, resume and sync one by its id."""


@sources_group.command("list")
@_JSON_OPTION
def sources_list(as_json: bool) -> None:
    """The sources that can be turned on from a terminal: id, name, and whether each is on."""
    try:
        result = NodeSocket().request_sync("GET", "/api/local/owner/sources")
    except NodeDown:
        click.echo(NODE_DOWN, err=True)
        sys.exit(EXIT_NODE_DOWN)
    except NodeRefused as exc:
        if exc.status == 404:
            click.echo("This Topos is older than `sources list`. Update it (`uv tool upgrade topos-node`) "
                       "and restart it.", err=True)
        else:
            click.echo(f"Topos refused `sources list`: {exc.code}.", err=True)
        sys.exit(EXIT_REFUSED)
    states = {str(row.get("source_id")): row for row in (result.get("sources") or []) if isinstance(row, dict)}
    rows, lines = [], []
    for entry in setup_catalog.CATALOG:
        # Names, aliases and links are this program's own; the node contributes flags, a code and stamps.
        state = states.get(entry.source_id) or {}
        row = {**entry.public(), "enabled": state.get("enabled") if isinstance(state.get("enabled"), bool) else None,
               "last_sync_at": _stamp(state.get("last_sync_at"))}
        if entry.kind == KIND_MESSAGES:
            row["ready"] = state.get("ready") if isinstance(state.get("ready"), bool) else None
            row["needs"] = _word(state.get("needs"))
            row["paused"] = bool(state.get("paused"))
        elif entry.kind == KIND_EXTENSION:
            row["connected"] = state.get("connected") is True
            row["last_write_at"] = _stamp(state.get("last_write_at"))
        else:
            row["paused"] = bool(state.get("paused"))
        rows.append(row)
        if entry.kind == KIND_EXTENSION:
            word = "connected" if row["connected"] else f"not connected (extension: {entry.app})"
        elif row["enabled"]:
            word = "on" + (f", last sync {row['last_sync_at'][:16]}" if row["last_sync_at"] else ", not synced yet")
        elif row.get("paused"):
            word = "paused"
        else:
            word = "off" + (f" (needs: {row['needs']})" if row.get("needs") else "")
        lines.append(f"{entry.source_id:<26}{entry.name:<26}{word}")
    _echo({"sources": rows}, "\n".join(lines), as_json=as_json)
    sys.exit(EXIT_OK)


@sources_group.command("preview")
@click.argument("source_id", type=SOURCE)
@_SKIP_OPTION
@click.option("--backfill", type=click.Choice(["all", "30d"]), default=None,
              help="Messages only: the window to count (default 30d).")
@_JSON_OPTION
@_QUIET_OPTION
def sources_preview(source_id: str, skip: tuple[str, ...], backfill: Optional[str], as_json: bool, quiet: bool) -> None:
    """Counts only: how many sessions (or messages), how much text, which folders, the date span."""
    entry = _other_kind(source_id)
    if entry is not None and entry.kind == KIND_EXTENSION:
        _echo({"source_id": source_id, "status": "nothing_to_preview", "app": entry.app},
              f"{entry.name} has nothing to preview: the extension sends new activity as it happens.",
              as_json=as_json, quiet=quiet)
        sys.exit(EXIT_OK)
    if entry is not None:
        if not _can_prompt():
            _needs_terminal(entry, "preview", as_json=as_json)
        payload, human = _messenger_counts(entry, backfill or "30d", as_json=as_json)
        _echo(payload, human, as_json=as_json, quiet=quiet)
        sys.exit(EXIT_OK)

    def project(result: dict) -> tuple[dict, str]:
        sessions = int(result.get("sessions") or 0)
        size = int(result.get("bytes_text") or 0)
        folders = [str(f) for f in (result.get("folders") or []) if isinstance(f, str)]
        oldest = str(result["oldest"]) if result.get("oldest") else None
        newest = str(result["newest"]) if result.get("newest") else None
        payload = {"sessions": sessions, "bytes_text": size, "folders": folders, "oldest": oldest, "newest": newest}
        span = f", {oldest[:10]} to {newest[:10]}" if oldest and newest else ""
        human = (
            f"{source_id}: {sessions} sessions, {_human_bytes(size)} of text, "
            f"{len(folders)} folders ({', '.join(folders) or 'none'}){span}."
        )
        return payload, human

    _door(source_id, "preview", {"skip": list(skip)}, as_json=as_json, quiet=quiet, project=project)


@sources_group.command("enable")
@click.argument("source_id", type=SOURCE)
@_SKIP_OPTION
@click.option("--backfill", type=click.Choice(["all", "30d"]), default="30d", show_default=True, help="How far back to read.")
@click.option("--open-terminal", is_flag=True,
              help="iMessage and Signal: open the question in a Terminal window on macOS; no command typing needed.")
@_JSON_OPTION
@_QUIET_OPTION
def sources_enable(source_id: str, skip: tuple[str, ...], backfill: str, open_terminal: bool, as_json: bool,
                   quiet: bool) -> None:
    """Turn a source on: install it, start the backfill, and sync hourly where the source allows it.

    iMessage and Signal ask you first, on this terminal. A browser extension's source says what is yours to do.
    """
    entry = _other_kind(source_id)
    if entry is not None and entry.kind == KIND_EXTENSION:
        _enable_extension(entry, as_json=as_json, quiet=quiet)
    if entry is not None:
        _enable_messenger(entry, backfill, open_terminal=open_terminal, as_json=as_json, quiet=quiet)

    def project(result: dict) -> tuple[dict, str]:
        payload = {"source_id": source_id, "status": "enabled", "backfill": backfill, "skipped": list(skip)}
        return payload, f"{source_id} enabled; backfill {backfill} started, syncing hourly."

    _door(source_id, "enable", {"skip": list(skip), "backfill": backfill}, as_json=as_json, quiet=quiet, project=project)


def _toggle(action: str, past: str, help_text: str):
    @sources_group.command(action, help=help_text)
    @click.argument("source_id", type=SOURCE)
    @_JSON_OPTION
    @_QUIET_OPTION
    def command(source_id: str, as_json: bool, quiet: bool) -> None:
        entry = _other_kind(source_id)
        if entry is not None and entry.kind == KIND_EXTENSION:
            click.echo(f"{entry.name} is written by a browser extension: stop it in the extension, or revoke its "
                       f"key with `topos-node pushers disconnect {entry.app}`.", err=True)
            sys.exit(EXIT_REFUSED)
        if entry is not None and action == "resume":
            _confirmed_messenger_verb(entry, "resume", f"Turn {entry.name} back on?", "resumed",
                                      as_json=as_json, quiet=quiet)
        if entry is not None:
            _call(entry, "POST", f"/api/local/owner/sources/{source_id}/{action}", as_json=as_json)
            _echo({"source_id": source_id, "status": past}, f"{source_id} {past}.", as_json=as_json, quiet=quiet)
            sys.exit(EXIT_OK)

        def project(result: dict) -> tuple[dict, str]:
            return {"source_id": source_id, "status": past}, f"{source_id} {past}."

        _door(source_id, action, {}, as_json=as_json, quiet=quiet, project=project)

    return command


sources_pause = _toggle("pause", "paused", "Stop the hourly sync and the hooks for this source (keeps what was read).")
sources_resume = _toggle("resume", "resumed", "Resume the hourly sync and the hooks for a paused source.")


@sources_group.command("sync")
@click.argument("source_id", type=SOURCE)
@click.option("--session", "session_path", metavar="PATH", default=None, help="Sync one transcript file only.")
@_JSON_OPTION
@_QUIET_OPTION
def sources_sync(source_id: str, session_path: Optional[str], as_json: bool, quiet: bool) -> None:
    """Sync now (refused unless the source is enabled)."""
    entry = _other_kind(source_id)
    if entry is not None and entry.kind == KIND_EXTENSION:
        click.echo(f"{entry.name} is sent by a browser extension as it happens; there is nothing to sync from here.",
                   err=True)
        sys.exit(EXIT_REFUSED)
    if entry is not None:
        _confirmed_messenger_verb(entry, "sync", f"Read new {entry.name} messages now?", "queued",
                                  as_json=as_json, quiet=quiet)

    def project(result: dict) -> tuple[dict, str]:
        # The owner door queues durable work; an accepted job has not synced
        # anything yet. Do not project its missing counts as a completed sync.
        if result.get("job_id"):
            status = "already_running" if result.get("already_running") else "queued"
            return {"source_id": source_id, "status": status, "job_id": result["job_id"]}, (
                f"{source_id} sync {status.replace('_', ' ')} (job {result['job_id']})."
            )
        counts = _ints(result)
        ledger = result.get("ledger") if isinstance(result.get("ledger"), dict) else {}
        ledger_counts = _ints(ledger)
        dropped = _ints(ledger.get("dropped"))
        payload = {
            "source_id": source_id,
            "status": _word(result.get("status")) or "ok",
            "counts": counts,
            "ledger": {**ledger_counts, **({"dropped": dropped} if dropped else {})},
        }
        emitted = ledger_counts.get("turns_emitted")
        detail = f" {emitted} turns" if emitted is not None else ""
        return payload, f"{source_id} synced:{detail or ' done'}."

    body = {"session_path": session_path} if session_path else {}
    _door(source_id, "sync", body, as_json=as_json, quiet=quiet, project=project)


@sources_group.command("reclassify-touches")
@click.argument("source_id", type=SOURCE)
@click.option("--apply", "apply", is_flag=True,
              help="Clear the flag where no read of Topos came first. Asks you in this terminal first.")
@_JSON_OPTION
def sources_reclassify_touches(source_id: str, apply: bool, as_json: bool) -> None:
    """Re-judge stored replies: only a READ of Topos keeps later replies private now; a status check, health
    check or setup step is recorded as a system touch. Counts only, and a dry run unless --apply."""

    def project(result: dict) -> tuple[dict, str]:
        payload = {"source_id": source_id, "status": _word(result.get("status")) or "ok",
                   "applied": bool(result.get("applied")), **_ints(result)}
        if result.get("status") == "unsupported":
            return payload, (f"{source_id}: not re-judged by this build; "
                             f"its {payload.get('flagged_rows', 0)} kept replies stay kept.")
        if result.get("applied"):
            return payload, (
                f"{source_id}: {payload.get('cleared', 0)} of {payload.get('flagged_rows', 0)} kept replies are "
                f"shareable again; {payload.get('touches_recorded', 0)} touches recorded; derivation queued."
            )
        return payload, (
            f"{source_id}: {payload.get('flagged_rows', 0)} replies are kept owner-only. Under the new rule "
            f"{payload.get('would_clear', 0)} would be shareable again; {payload.get('kept_after_a_read', 0)} stay "
            f"(after a read of Topos), {payload.get('kept_no_transcript', 0)} stay (transcript gone). "
            f"Touches found: {payload.get('topos_reads', 0)} reads, {payload.get('topos_system_touches', 0)} system."
        )

    if not apply:
        _door(source_id, "reclassify-touches", {"apply": False}, as_json=as_json, quiet=False, project=project)
        return
    # Making replies shareable is a dangerous change on the socket (owner_door_routes): it needs the human, asked
    # here on a terminal, and a one-change confirmation token — never a flag an agent could pass.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        click.echo("--apply makes replies shareable again, so it asks you first: run it in your own terminal.", err=True)
        sys.exit(EXIT_REFUSED)
    sock = NodeSocket()
    path = f"/api/local/owner/sources/{source_id}/reclassify-touches"
    try:
        preview = sock.request_sync("POST", path, json_body={"apply": False})
        click.echo(project(preview)[1])
        if not int(preview.get("would_clear") or 0):
            click.echo("Nothing to change.")
            sys.exit(EXIT_OK)
        if not click.confirm("Make those replies shareable again?", default=False):
            sys.exit(EXIT_OK)
        from topos.uds import write_owner_confirmation

        result = sock.request_sync("POST", path, json_body={"apply": True},
                                   headers={"X-Topos-Owner-Confirm": write_owner_confirmation()})
    except NodeDown:
        click.echo(NODE_DOWN, err=True)
        sys.exit(EXIT_NODE_DOWN)
    except NodeRefused as exc:
        click.echo(f"Topos refused `reclassify-touches` for {source_id}: {exc.code}.", err=True)
        sys.exit(EXIT_REFUSED)
    payload, human = project(result)
    click.echo(json.dumps(payload, indent=2, sort_keys=True) if as_json else human)
    sys.exit(EXIT_OK)


# Private, unregistered definitions share the existing source command namespace.
from topos.cli.source_create_cmd import add_command, infer_command, inspect_command

sources_group.add_command(infer_command)
sources_group.add_command(add_command)
sources_group.add_command(inspect_command)
