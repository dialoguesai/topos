"""`topos-node agents add|list|disconnect NAME` — enrol and revoke AI coding tools and the owner's apps.

An app is any other name. It is enrolled the same way and becomes the same class, ``owner_agent``, but says with
``--reads`` what it may read: the prompt shows that, the enrolment stores it (``mcp_clients.AppGrant``, DR-66), and it
reaches those reads and only the dispatch door on the socket. It is enrolled on a terminal only: the pairing approval
is for coding tools. A yes on that terminal is also what reconnects a disconnected app.

The label (CONTRACTS §1.1) is minted by lane L1's ``mint_agent_label`` and
written here, once, to ``~/.topos/agents/<client>.token`` (0700 dir, 0600 file).
It is never printed: the stdio server reads the file, nothing else does
(rev-1 security finding 18).

Enrolment is a human act. ``add`` asks for a yes on a TTY; an agent driving a
shell has no TTY and is told to hand the step to its owner. ``--approved`` is
the one way past the prompt, and only with the pairing record that
``topos-node pair --add-agent`` leaves behind after the approve page said yes
(§3). A revoked agent is never silently re-enrolled (finding 12): without that
record, ``add`` refuses, even on a TTY.

``disconnect`` is revoke + source off + hook refusal: the row is tombstoned
(checked on every request, nothing cached), every bundled transcript source is
paused through the §2.8 door so the hourly schedule and the hooks stop
ingesting, and the label file is removed.
"""
from __future__ import annotations

import json
import os
import shlex
import sqlite3
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click

from topos import mcp_server
from topos.mcp_clients import AppGrant, parse_app_reads
from topos.mcp_server import CLIENT_NAMES, DISPLAY_NAMES

EXIT_OK = 0
EXIT_NEEDS_HUMAN = 2

#: Pairing states that count as an approval for `--approved` (CONTRACTS §3).
_APPROVED_STATES = ("approved", "delivered")

_JSON_OPTION = click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")


def _can_prompt() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _open_approval_terminal(client: str) -> bool:
    """Launch the normal human-only TTY flow; launching alone grants nothing."""
    if sys.platform != "darwin":
        return False
    folder = Path(tempfile.mkdtemp(prefix="topos-agent-approval-"))
    script = folder / "Connect-to-TOPOS.command"
    command = shlex.join([sys.executable, "-m", "topos.cli", "agents", "add", client])
    # Only paths cross into the new terminal's environment, never credentials.
    path_env = {k: os.environ[k] for k in ("TOPOS_ENV_FILE", "TOPOS_DATABASE_PATH") if os.environ.get(k)}
    exports = "".join(f"export {k}={shlex.quote(v)}\n" for k, v in path_env.items())
    script.write_text(
        '#!/bin/sh\ntrap \'rm -f -- "$0"\' EXIT\n' + exports +
        'printf "\\nConnect your coding tool to your existing TOPOS account\\n\\n"\n' + command +
        '\nresult=$?\nif [ "$result" -eq 0 ]; then\n'
        'printf "\\nConnected. Return to your coding assistant to finish verification.\\n"\n'
        'else\nprintf "\\nConnection was not approved or could not finish. Return to your assistant.\\n"\nfi\n'
        'printf "\\nPress Return to close this setup window.\\n"\nread -r answer\nexit "$result"\n',
        encoding="utf-8",
    )
    script.chmod(0o700)
    try:
        subprocess.run(["/usr/bin/open", "-a", "Terminal", str(script)], check=True,
                       capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        script.unlink(missing_ok=True)
        folder.rmdir()
        return False
    return True


def _fail(message: str, code: int = EXIT_NEEDS_HUMAN) -> None:
    click.echo(message, err=True)
    sys.exit(code)


def _client_or_fail(name: str) -> str:
    try:
        return mcp_server.validate_agent(name)
    except ValueError:
        _fail(f"{name!r} can't be an agent name. Use letters, digits and hyphens, or one of: {', '.join(CLIENT_NAMES)}.")
        raise AssertionError  # unreachable


def _grant_or_fail(client: str, reads: Optional[str]) -> Optional[AppGrant]:
    """An app's ``--reads``, checked; None for a coding tool. An app must say what it reads, and a coding tool can't."""
    if client in CLIENT_NAMES:
        if reads:
            _fail(f"{DISPLAY_NAMES[client]} is a coding tool, so it takes no --reads.")
        return None
    try:
        grant = parse_app_reads((reads or "").split(","))
    except ValueError as exc:
        hint = "Say what it may read: --reads facts:<pack>,messages,journals." if not reads else str(exc)
        _fail(f"Could not connect {client}: {hint}")
        raise AssertionError  # unreachable
    return grant


def _unknown_packs(conn: sqlite3.Connection, grant: AppGrant) -> list[str]:
    """Packs a grant names that this node does not have: not bundled, and not in its registry (empty on a fresh node)."""
    from topos.features.derivation.registry import bundled_pack_dir

    known = {path.stem for path in Path(bundled_pack_dir()).glob("*.yaml")}
    try:
        known |= {str(r[0]) for r in conn.execute("SELECT pack_id FROM pack_registry").fetchall()}
    except sqlite3.Error:
        pass  # no registry yet: the bundled packs are what this node has
    return [pack for pack in grant.packs if pack not in known]


def _consent_question(client: str, display: str, grant: Optional[AppGrant] = None) -> str:
    """What the owner says yes to. An app reads what its grant says, and the question says what that is."""
    if grant is not None:
        return (f"Connect {display} to your Topos? As you, it can read: {grant.plain()}. "
                "Every read is recorded under its name.")
    return (f"Connect {display} to your Topos? It will read what you allow in Topos, as you, "
            "and every read is recorded under its name.")


def _registry():
    """Lane L1's functions (CONTRACTS §1.1), or a clear message on a build without them."""
    from topos import mcp_clients

    missing = [n for n in ("mint_agent_label", "revoke_agent", "list_agents") if not hasattr(mcp_clients, n)]
    if missing:
        raise click.ClickException(
            "this build of topos-node cannot manage agents: topos.mcp_clients lacks "
            + ", ".join(missing)
            + " (CONTRACTS §1.1, lane L1)"
        )
    return mcp_clients


def _database_path() -> Path:
    from topos.storage.db.paths import resolve_active_database

    return resolve_active_database().path


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path))


def _agent_row(registry, conn: sqlite3.Connection, client: str) -> Optional[dict]:
    for row in registry.list_agents(conn):
        if str(row.get("client_id") or "") == f"agent-{client}":
            return row
    return None


def read_pairing_record() -> Optional[dict]:
    """``~/.topos/pairing.json`` as lane L7's `pair` leaves it (CONTRACTS §3):
    through its own reader when this build has one, else the file itself."""
    try:
        from topos.cli.pair_cmd import read_pairing  # type: ignore[import-not-found]
    except ImportError:
        read_pairing = None
    if read_pairing is not None:
        try:
            record = read_pairing()
        except Exception:  # noqa: BLE001 — an unreadable record is no record
            record = None
        return record if isinstance(record, dict) else None
    path = mcp_server.topos_dir() / "pairing.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _pairing_record_approves(client: str) -> bool:
    """The approve page said yes for THIS agent: `pair` records the agent as
    ``agent: {client}`` in both pairing modes, and the state is approved or
    delivered. Anything else — no record, another client, pending, denied,
    expired — is not an approval."""
    record = read_pairing_record()
    if not record:
        return False
    agent = record.get("agent") if isinstance(record.get("agent"), dict) else {}
    return (
        str(record.get("state") or "") in _APPROVED_STATES
        and str(agent.get("client") or "") == client
        # Spent by the enrolment it backed (`_spend_pairing_approval`): a disconnected agent re-enrols only on
        # a fresh approval, never by replaying the file (rev-1 security finding 12).
        and not agent.get("enrolled_at")
    )


def _spend_pairing_approval(client: str) -> None:
    """Record on the pairing file that its approval minted THIS client's label, so it is not an approval again."""
    record = read_pairing_record()
    if not record:
        return
    agent = dict(record.get("agent") or {}) if isinstance(record.get("agent"), dict) else {}
    if str(agent.get("client") or "") != client:
        return
    agent["enrolled_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    record = {**record, "agent": agent}
    try:
        from topos.cli.pair_cmd import write_pairing

        write_pairing(record)
    except Exception:  # noqa: BLE001 — a build without pair's writer: the file itself, 0600
        path = mcp_server.topos_dir() / "pairing.json"
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def write_label_file(client: str, label: str, *, agents_dir: Optional[Path] = None) -> Path:
    """0700 directory, 0600 file, one line. The only place a label is written."""
    path = mcp_server.label_path(client, agents_dir=agents_dir)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, stat.S_IRWXU)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(label + "\n")
    finally:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def _public_row(row: dict) -> dict:
    public = {
        "client": str(row.get("client_id") or "")[len("agent-"):],
        "enrolled_at": row.get("created_at"),
        "revoked": bool(row.get("revoked_at")),
    }
    return {**public, "reads": row["reads"]} if "reads" in row else public


def _echo(payload: dict | list, as_json: bool, human: str) -> None:
    click.echo(json.dumps(payload, indent=2, sort_keys=True) if as_json else human)


@click.group("agents")
def agents_group() -> None:
    """Connect, list and disconnect AI coding tools (Claude Code, Codex, Cursor, Gemini) and apps."""


@agents_group.command("add")
@click.argument("name")
@_JSON_OPTION
@click.option(
    "--approved",
    is_flag=True,
    help="Enrol on the approval `topos-node pair --add-agent` recorded (no prompt).",
)
@click.option("--open-terminal", is_flag=True, help="Open the approval prompt in Terminal on macOS; no command typing needed.")
@click.option("--reads", default=None, help="For an app: what it may read, comma separated: facts:<pack>, messages, journals.")
def agents_add(name: str, as_json: bool, approved: bool, open_terminal: bool, reads: Optional[str]) -> None:
    """Connect an AI coding tool or an app to this Topos (asks you first)."""
    client = _client_or_fail(name)
    display = DISPLAY_NAMES.get(client, client)
    is_app = client not in CLIENT_NAMES
    if is_app and (approved or open_terminal):
        _fail(f"{display} is connected from a terminal: run `topos-node agents add {client} --reads ...` yourself.")
    grant = _grant_or_fail(client, reads)
    if open_terminal:
        if approved:
            raise click.UsageError("Use either --approved or --open-terminal.")
        if not _open_approval_terminal(client):
            _fail("Could not open the approval window. Open Topos on this Mac, then try again.")
        _echo({"client": client, "status": "approval_pending"}, as_json,
              f"Opened the {display} approval prompt. Confirm it in Terminal, then return here.")
        return
    registry = _registry()
    db_path = _database_path()
    if not db_path.is_file():
        _fail(
            "Topos hasn't been started on this computer yet. Run `topos-node start --headless` "
            "(or open Topos), then try again."
        )
    conn = _connect(db_path)
    try:
        unknown = _unknown_packs(conn, grant) if grant else []
        if unknown:
            _fail(f"This Topos has no pack named {', '.join(unknown)}.")
        existing = _agent_row(registry, conn, client)
        revoked = bool(existing and existing.get("revoked_at"))
        if approved:
            if not _pairing_record_approves(client):
                _fail(
                    f"No approval on record for {display}. Run `topos-node pair --add-agent --client {client}` "
                    "and approve it in the Topos app, or run `topos-node agents add "
                    f"{client}` yourself in a terminal."
                )
        else:
            if revoked and not is_app:
                _fail(
                    f"{display} was disconnected from Topos. Re-connecting needs a fresh approval: "
                    f"run `topos-node pair --add-agent --client {client}`."
                )
            if not _can_prompt():
                _fail(
                    f"Connecting {display} needs a yes from you in a terminal. Run "
                    f"`topos-node agents add {client}` yourself"
                    + (" with the same --reads." if is_app else f", or `topos-node pair --add-agent --client {client}`.")
                )
            # The prompt goes to stderr so `--json` stdout stays one document.
            if not click.confirm(_consent_question(client, display, grant), err=True):
                _fail("Not connected.")
        # L1's registry refuses a revoked row unless told the approval happened;
        # the gates above are what decide that, so pass exactly their verdict.
        try:
            # An app has no pairing approval: the yes on this terminal is its fresh approval.
            row = dict(registry.mint_agent_label(conn, client=client, approved=approved or is_app, grant=grant))
        except ValueError as exc:
            _fail(f"Could not connect {display}: {exc}")
            raise AssertionError  # unreachable
        label = str(row.pop("label", "") or "")
        if not label:
            raise click.ClickException("the registry minted no label")
        path = write_label_file(client, label)
        del label
        if approved:
            _spend_pairing_approval(client)
    finally:
        conn.close()
    payload = {
        "client": client,
        "display_name": display,
        "enrolled_at": row.get("created_at"),
        "label_file": str(path),
    }
    _echo(payload, as_json, f"Connected {display}. Its label is in {path} (never share that file).")


@agents_group.command("list")
@_JSON_OPTION
def agents_list(as_json: bool) -> None:
    """Agents enrolled on this Topos, with revoked ones marked."""
    registry = _registry()
    db_path = _database_path()
    rows: list[dict] = []
    if db_path.is_file():
        conn = _connect(db_path)
        try:
            rows = [
                _public_row(r)
                for r in registry.list_agents(conn)
                if str(r.get("client_id") or "").startswith("agent-")
            ]
        finally:
            conn.close()
    lines = [
        f"{'x ' if r['revoked'] else '  '}{r['client']:<12} enrolled {r['enrolled_at'] or '?'}"
        + ("  (disconnected)" if r["revoked"] else "")
        for r in rows
    ]
    _echo(rows, as_json, "\n".join(lines) or "(no agents connected)")


@agents_group.command("disconnect")
@click.argument("name")
@_JSON_OPTION
def agents_disconnect(name: str, as_json: bool) -> None:
    """Revoke an agent, pause the transcript sources and remove its label."""
    from topos.agent_lifecycle import disconnect_agent

    client = _client_or_fail(name)
    display = DISPLAY_NAMES.get(client, client)
    _registry()
    db_path = _database_path()
    conn = _connect(db_path) if db_path.is_file() else None
    try:
        result = disconnect_agent(conn, client)
    finally:
        if conn is not None:
            conn.close()
    if not result.was_connected:
        _fail(f"{display} isn't connected to Topos.")
    human = f"Disconnected {display}."
    if result.sources_paused:
        human += " Paused: " + ", ".join(result.sources_paused) + "."
    for problem in result.problems:
        human += "\n" + problem
    _echo(result.as_dict(), as_json, human)
    if result.problems:
        sys.exit(EXIT_NEEDS_HUMAN)
