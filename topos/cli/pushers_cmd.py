"""`topos-node pushers add|list|disconnect APP` — let an app on this computer write into Topos directly.

A pusher is an app that pushes rows (the browser extension, voxterm) rather than
reads them; ``topos/local_pushers.py`` says what its key is worth. Enrolment is a
human act on a terminal, as it is for the coding agents: ``add`` asks for a yes
on a TTY, naming the sources the app may write and the extension origins it
calls from; an agent driving a shell has no TTY and is told to hand the step to
its owner. There is no ``--approved``: no pairing page approves a pusher.

The key is shown ONCE, on this terminal, because the app that needs it is a
browser extension with no file it could read; the owner pastes it into the
extension's options page. It is never logged. A revoked app re-enrols on a
fresh yes; nothing replays.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import click

EXIT_OK = 0
EXIT_NEEDS_HUMAN = 2

_JSON_OPTION = click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")


def _can_prompt() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _fail(message: str, code: int = EXIT_NEEDS_HUMAN) -> None:
    click.echo(message, err=True)
    sys.exit(code)


def _database_path() -> Path:
    from topos.storage.db.paths import resolve_active_database

    return resolve_active_database().path


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path))


def _source_registry():
    """Include custom runtime installs when granting a local producer its sources."""
    from topos.mcp_server import NodeSocket, NodeDown, NodeRefused
    from topos.sources.definitions import definition_from_payload
    try:
        result = NodeSocket().request_sync("GET", "/sources")
    except NodeDown:
        raise click.ClickException('Topos isn\'t running — say "start Topos".') from None
    except NodeRefused as exc:
        raise click.ClickException(f"Topos refused the source list ({exc.code or exc.status}).") from None
    return {row["source_id"]: definition_from_payload(row) for row in result.get("sources", [])}


def _echo(payload, as_json: bool, human: str) -> None:
    click.echo(json.dumps(payload, indent=2, sort_keys=True) if as_json else human)


def _consent_question(app: str, display: str, grant) -> str:
    return (f"Let {display} ({app}) write into your Topos? From this computer it may {grant.plain()}. "
            "Its rows are recorded under its name, as your own capture app's.")


@click.group("pushers")
def pushers_group() -> None:
    """Connect, list and disconnect apps that write into this Topos directly (browser extension, voxterm)."""


@pushers_group.command("add")
@click.argument("app")
@click.option("--sources", required=True,
              help="Sources the app may write, comma separated (e.g. browser_visits,browser_events).")
@click.option("--origin", "origins", multiple=True,
              help="An extension origin to answer CORS for (chrome-extension://<id>). Repeatable. Never a wildcard.")
@click.option("--name", "display_name", default="", help="How the app is listed.")
@_JSON_OPTION
def pushers_add(app: str, sources: str, origins: tuple, display_name: str, as_json: bool) -> None:
    """Let an app write the named sources into this Topos (asks you first; shows its key once)."""
    from topos import local_pushers

    try:
        _, short = local_pushers.pusher_ids(app)
    except ValueError:
        _fail(f"{app!r} can't be an app name. Use lowercase letters, digits and hyphens.")
        raise AssertionError  # unreachable
    display = str(display_name or "").strip() or local_pushers.PUSHER_DISPLAY_NAMES.get(short, short)
    db_path = _database_path()
    if not db_path.is_file():
        _fail("Topos hasn't been started on this computer yet. Run `topos-node start --headless` "
              "(or open Topos), then try again.")
    try:
        grant = local_pushers.PusherGrant(
            local_pushers.parse_sources(sources.split(","), registry=_source_registry()),
            local_pushers.parse_origins(origins),
        )
    except local_pushers.PusherRefused as exc:
        _fail(f"Could not connect {display}: {exc}")
        raise AssertionError  # unreachable
    if not _can_prompt():
        _fail(f"Connecting {display} needs a yes from you in a terminal. Run "
              f"`topos-node pushers add {short} --sources {sources}` yourself.")
    # The prompt goes to stderr so `--json` stdout stays one document.
    if not click.confirm(_consent_question(short, display, grant), err=True):
        _fail("Not connected.")
    conn = _connect(db_path)
    try:
        # The yes on this terminal is the approval, for a first enrolment and for a disconnected app alike.
        row = local_pushers.enrol_pusher(conn, app=short, grant=grant, approved=True, display_name=display)
    except local_pushers.PusherRefused as exc:
        _fail(f"Could not connect {display}: {exc}")
        raise AssertionError  # unreachable
    finally:
        conn.close()
    key = str(row.pop("key", "") or "")
    if not key:
        raise click.ClickException("the registry minted no key")
    payload = {
        "app": short,
        "display_name": display,
        "enrolled_at": row.get("created_at"),
        "sources": list(grant.sources),
        "origins": list(grant.origins),
        "key": key,
    }
    human = (f"Connected {display}. Paste this key into the app's settings; it is shown once and never again:\n\n"
             f"  {key}\n\n"
             f"It writes {', '.join(grant.sources)} to the Topos on this computer (http://127.0.0.1:8676) and nothing else. "
             f"Disconnect with `topos-node pushers disconnect {short}`.")
    _echo(payload, as_json, human)


@pushers_group.command("list")
@_JSON_OPTION
def pushers_list(as_json: bool) -> None:
    """Apps allowed to write into this Topos, with disconnected ones marked."""
    from topos import local_pushers

    db_path = _database_path()
    rows: list = []
    if db_path.is_file():
        conn = _connect(db_path)
        try:
            rows = [
                {"app": r["app"], "display_name": r.get("display_name"), "enrolled_at": r.get("created_at"),
                 "last_used_at": r.get("last_used_at"), "revoked": bool(r.get("revoked_at")),
                 "sources": r["sources"], "origins": r["origins"]}
                for r in local_pushers.list_pushers(conn)
            ]
        finally:
            conn.close()
    lines = [
        f"{'x ' if r['revoked'] else '  '}{r['app']:<26} writes {', '.join(r['sources']) or '-'}"
        + ("  (disconnected)" if r["revoked"] else "")
        for r in rows
    ]
    _echo(rows, as_json, "\n".join(lines) or "(no apps connected)")


@pushers_group.command("disconnect")
@click.argument("app")
@_JSON_OPTION
def pushers_disconnect(app: str, as_json: bool) -> None:
    """Revoke an app's key; its next write is refused."""
    from topos import local_pushers

    try:
        _, short = local_pushers.pusher_ids(app)
    except ValueError:
        _fail(f"{app!r} can't be an app name.")
        raise AssertionError  # unreachable
    db_path = _database_path()
    revoked = False
    if db_path.is_file():
        conn = _connect(db_path)
        try:
            revoked = local_pushers.revoke_pusher(conn, app=short)
        finally:
            conn.close()
    if not revoked:
        _fail(f"{short} isn't connected to Topos.")
    _echo({"app": short, "revoked": True}, as_json, f"Disconnected {short}. Its key no longer works.")
