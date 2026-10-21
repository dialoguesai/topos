"""`topos-node setup --client NAME` — the whole on-ramp in one re-runnable command.

An AI coding tool (Claude Code, Codex, Cursor) runs this once per turn until it
exits 0. Every run reads where this computer stands and does the next steps it
can do alone:

1. **key** — no Topos key yet: start device pairing (the person signs in and
   approves a code in the browser; the key lands in ``~/.topos/.env`` without
   passing through this process's output). Pairing comes BEFORE starting the
   node: a node with no key exits at once.
2. **account** — after a fresh pairing, the person confirms the masked account
   that approved ("is that you?") and the agent re-runs with
   ``--confirm-account``.
3. **node** — not running: open Topos.app when it is installed, else install
   the login service (``--no-service``: a detached headless start).
4. **agent** — this client is not enrolled: consume the pairing's approval when
   it named this client, else open the human-only approval prompt in Terminal
   (macOS) or start an add-agent pairing.
5. **source** — this client's own session history is not enabled: enable it
   with the chosen backfill (``--skip`` folders left out).

It stops only where a person must act, and says exactly what: exit 2 with
``step`` and ``human``. Exit 0 means every step is done; 1 a step failed;
4 the node would not start; 5 the person denied pairing.

``--plan`` changes nothing: it reports which steps remain (and, when the node is
running, the source preview counts) so the agent can ask ONE itemized yes before
the first real run.

What never happens here: no key, label or device code is printed or logged; the
sub-commands this runs are read for their status words only.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import click

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NEEDS_HUMAN = 2
EXIT_NODE_DOWN = 4
EXIT_DENIED = 5

CLIENT_SOURCES = {
    "claude-code": "claude_code_sessions",
    "codex": "codex_sessions",
    "cursor": "cursor_sessions",
}
CLIENTS = ("claude-code", "codex", "cursor", "gemini")
DISPLAY = {"claude-code": "Claude Code", "codex": "Codex", "cursor": "Cursor", "gemini": "Gemini"}

#: Seconds to wait for a node this command started to open its socket.
NODE_START_TIMEOUT_SECONDS = 90.0
#: A Terminal approval window opened less than this long ago is not opened again on a re-run.
APPROVAL_WINDOW_REUSE_SECONDS = 600
STEPS = ("key", "account", "node", "agent", "source")


# --- small facts ----------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _has_key() -> bool:
    from topos.cli.commands import _read_env_file_key, _user_env_path

    return bool(_read_env_file_key(_user_env_path()))


def _status() -> dict:
    from topos.cli.status_cmd import collect_status

    return collect_status()


def _node_argv() -> List[str]:
    from topos.cli.binary import resolve_node_binary

    found = resolve_node_binary()
    return [str(found)] if found is not None else [sys.executable, "-m", "topos.cli"]


def _run_self(args: List[str], timeout: float = 120.0) -> tuple[int, Optional[dict]]:
    """Run one of this binary's own commands with ``--json`` and return (exit code, parsed document).

    The document is read for status words only; nothing of it is echoed whole.
    """
    try:
        proc = subprocess.run(_node_argv() + args, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return EXIT_FAILED, None
    try:
        doc = json.loads(proc.stdout) if proc.stdout.strip() else None
    except ValueError:
        doc = None
    return proc.returncode, doc if isinstance(doc, dict) else None


def _approval_marker(client: str) -> Path:
    from topos.cli.pair_cmd import topos_dir

    return topos_dir() / f"agent-approval-{client}.json"


def _recent_terminal_approval(client: str) -> bool:
    path = _approval_marker(client)
    try:
        launched = float(json.loads(path.read_text(encoding="utf-8")).get("launched_at") or 0)
    except (OSError, ValueError, AttributeError):
        return False
    return 0 <= time.time() - launched < APPROVAL_WINDOW_REUSE_SECONDS


def _note_terminal_approval(client: str) -> None:
    from topos.cli.pair_cmd import _write_private

    _write_private(_approval_marker(client), json.dumps({"launched_at": time.time()}))


# --- results --------------------------------------------------------------------


class Outcome:
    def __init__(self, client: str) -> None:
        self.client = client
        self.source_id = CLIENT_SOURCES.get(client)
        self.done: List[str] = []
        self.notes: List[str] = []

    def result(self, *, state: str, step: str, exit_code: int, human: Optional[dict] = None,
               message: str = "", extra: Optional[dict] = None) -> dict:
        doc = {
            "client": self.client,
            "source_id": self.source_id,
            "state": state,
            "step": step,
            "done": list(self.done),
            "human": human,
            "message": message,
            "notes": list(self.notes),
            "exit_code": exit_code,
        }
        if extra:
            doc.update(extra)
        return doc


def _pairing_human(record: dict) -> dict:
    return {
        "action": "approve_code",
        "link": record.get("verification_url"),
        "code": record.get("user_code"),
        "expires_at": record.get("expires_at"),
        "say": (f"Go to {record.get('verification_url')} and type this code: {record.get('user_code')}. "
                "Sign in or create an account if asked, read the page, then click Approve. Only approve "
                "if you started this on your own computer in the last 10 minutes."),
    }


def _start_pairing(client: str, mode: str) -> dict:
    from topos.cli.pair_cmd import _note_poller_pid, _spawn_poller, start_pairing, write_pairing

    record = start_pairing(client_name=client, mode=mode)
    write_pairing(record)
    pid = _spawn_poller()
    record["poller_pid"] = pid
    _note_poller_pid(record, pid)
    return record


# --- the steps ------------------------------------------------------------------


def _step_key(out: Outcome, *, plan: bool) -> Optional[dict]:
    from topos.cli.pair_cmd import NeedsHuman, read_pairing, _reusable_pending

    if _has_key():
        out.done.append("key")
        return None
    record = read_pairing() or {}
    state = str(record.get("state") or "")
    if record.get("mode") == "create" and state == "denied":
        return out.result(state="failed", step="key", exit_code=EXIT_DENIED,
                          message="The pairing was denied. Nothing was created. Run setup again to start over.")
    if plan:
        return out.result(state="planned", step="key", exit_code=EXIT_NEEDS_HUMAN,
                          message="This computer has no Topos yet: setup will show a code to approve in the browser.")
    if record.get("mode") == "create" and _reusable_pending(record):
        return out.result(state="needs_human", step="key", exit_code=EXIT_NEEDS_HUMAN,
                          human=_pairing_human(record), message="Waiting for the person to approve the code.")
    if record.get("mode") == "create" and state in ("approved", "delivered"):
        return out.result(state="failed", step="key", exit_code=EXIT_FAILED,
                          message="Pairing was approved but no key was saved; run setup again to pair again.")
    try:
        record = _start_pairing(out.client, "create")
    except NeedsHuman as exc:
        return out.result(state="needs_human", step="key", exit_code=EXIT_NEEDS_HUMAN,
                          human={"action": "run_in_own_terminal", "command": "topos-node pair --manual",
                                 "say": str(exc.message)}, message=str(exc.message))
    except click.ClickException as exc:
        return out.result(state="failed", step="key", exit_code=EXIT_FAILED, message=str(exc.message))
    return out.result(state="needs_human", step="key", exit_code=EXIT_NEEDS_HUMAN,
                      human=_pairing_human(record), message="Showing the person a code to approve.")


def _step_account(out: Outcome, *, confirm: bool) -> Optional[dict]:
    """After a fresh pairing, the person confirms the masked account before anything reads their data."""
    from topos.cli.pair_cmd import read_pairing, write_pairing

    record = read_pairing() or {}
    fresh = record.get("mode") == "create" and record.get("state") in ("approved", "delivered")
    if not fresh or record.get("account_confirmed_at"):
        out.done.append("account")
        return None
    if not confirm:
        masked = record.get("masked_account") or "an account"
        return out.result(state="needs_human", step="account", exit_code=EXIT_NEEDS_HUMAN,
                          human={"action": "confirm_account", "masked_account": record.get("masked_account"),
                                 "say": f"Approved by {masked} — is that you?"},
                          message="Ask the person; re-run with --confirm-account only after they say yes.")
    record["account_confirmed_at"] = _now().isoformat()
    write_pairing(record)
    out.done.append("account")
    return None


def _topos_app_installed() -> bool:
    return sys.platform == "darwin" and Path("/Applications/Topos.app").is_dir()


def _open_topos_app() -> None:
    subprocess.run(["/usr/bin/open", "-a", "Topos"], capture_output=True, timeout=15)


def _wait_for_node(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _status()["node"]["running"]:
            return True
        time.sleep(1.0)
    return False


def _step_node(out: Outcome, *, service: bool, plan: bool, port: int) -> Optional[dict]:
    if _status()["node"]["running"]:
        out.done.append("node")
        return None
    if plan:
        return out.result(state="planned", step="node", exit_code=EXIT_NEEDS_HUMAN,
                          message="Topos is not running: setup will start it"
                                  + (" as a login service." if service else " in the background."))
    started_by = None
    from topos.defaults import DEFAULT_NODE_PORT

    if port == DEFAULT_NODE_PORT and _topos_app_installed():
        # Topos.app supervises its own node (and is the only way iMessage stays readable).
        _open_topos_app()
        started_by = "topos_app"
    elif service:
        code, _doc = _run_self(["service", "install", "--port", str(port)], timeout=60)
        started_by = "service" if code == 0 else None
    if started_by is None:
        code, _doc = _run_self(["start", "--headless", "--port", str(port)], timeout=NODE_START_TIMEOUT_SECONDS + 10)
        started_by = "headless" if code in (0, 75) else None
    if started_by is None or not _wait_for_node(NODE_START_TIMEOUT_SECONDS):
        return out.result(state="failed", step="node", exit_code=EXIT_NODE_DOWN,
                          message="Topos would not start. See ~/.topos/logs/node.log, or run `topos-node doctor`.")
    out.notes.append(f"started Topos ({started_by})")
    out.done.append("node")
    return None


def _agent_state(client: str) -> str:
    for row in _status().get("agents") or []:
        if row.get("client") == client:
            return "revoked" if row.get("revoked") else "enrolled"
    return "absent"


def _step_agent(out: Outcome, *, plan: bool, open_terminal: bool) -> Optional[dict]:
    from topos.cli.agents_cmd import _pairing_record_approves
    from topos.cli.pair_cmd import NeedsHuman, read_pairing, _reusable_pending

    state = _agent_state(out.client)
    if state == "enrolled":
        out.done.append("agent")
        return None
    display = DISPLAY[out.client]
    if plan:
        return out.result(state="planned", step="agent", exit_code=EXIT_NEEDS_HUMAN,
                          message=f"{display} is not connected yet: the person approves it once.")
    if _pairing_record_approves(out.client):
        code, _doc = _run_self(["agents", "add", out.client, "--approved", "--json"], timeout=60)
        if code == 0 and _agent_state(out.client) == "enrolled":
            out.notes.append(f"{display} connected by the approval the person gave in the browser")
            out.done.append("agent")
            return None
        return out.result(state="failed", step="agent", exit_code=EXIT_FAILED,
                          message=f"The approval was on record but {display} could not be connected; "
                                  "run `topos-node doctor`.")
    if state != "revoked" and open_terminal and sys.platform == "darwin":
        if not _recent_terminal_approval(out.client):
            code, _doc = _run_self(["agents", "add", out.client, "--open-terminal", "--json"], timeout=30)
            if code == 0:
                _note_terminal_approval(out.client)
        if _recent_terminal_approval(out.client):
            return out.result(state="needs_human", step="agent", exit_code=EXIT_NEEDS_HUMAN,
                              human={"action": "approve_in_terminal",
                                     "say": f"A Terminal window asks whether to connect {display} to your Topos. "
                                            "Answer it there, then come back."},
                              message="Waiting for the person to answer the Terminal prompt.")
    record = read_pairing() or {}
    if record.get("mode") == "add_agent" and record.get("client") == out.client and _reusable_pending(record):
        return out.result(state="needs_human", step="agent", exit_code=EXIT_NEEDS_HUMAN,
                          human=_pairing_human(record), message=f"Waiting for the person to approve {display}.")
    try:
        record = _start_pairing(out.client, "add_agent")
    except NeedsHuman as exc:
        return out.result(state="needs_human", step="agent", exit_code=EXIT_NEEDS_HUMAN,
                          human={"action": "run_in_own_terminal", "command": f"topos-node agents add {out.client}",
                                 "say": str(exc.message)}, message=str(exc.message))
    except click.ClickException as exc:
        return out.result(state="failed", step="agent", exit_code=EXIT_FAILED, message=str(exc.message))
    return out.result(state="needs_human", step="agent", exit_code=EXIT_NEEDS_HUMAN,
                      human=_pairing_human(record), message=f"Showing the person a code to approve {display}.")


def _source_row(source_id: str) -> Optional[dict]:
    for row in _status().get("sources") or []:
        if row.get("source_id") == source_id:
            return row
    return None


def _owner_door(method: str, path: str, body: Optional[dict] = None) -> tuple[Optional[dict], Optional[str]]:
    """(document, refusal code) from the node's socket-only owner doors."""
    from topos.mcp_server import NodeDown, NodeRefused, NodeSocket

    try:
        return NodeSocket().request_sync(method, path, json_body=body), None
    except NodeDown:
        return None, "node_down"
    except NodeRefused as exc:
        return None, str(exc.code or exc.status)


def _preview(source_id: str, skip: List[str]) -> Optional[dict]:
    doc, _err = _owner_door("POST", f"/api/local/owner/sources/{source_id}/preview", {"skip": skip})
    if not isinstance(doc, dict):
        return None
    return {k: doc.get(k) for k in ("sessions", "bytes_text", "folders", "oldest", "newest")}


def _step_source(out: Outcome, *, plan: bool, backfill: str, skip: List[str]) -> Optional[dict]:
    if out.source_id is None:
        out.notes.append(f"{DISPLAY[out.client]} has no session-history source yet")
        out.done.append("source")
        return None
    row = _source_row(out.source_id)
    if row and row.get("enabled"):
        if row.get("paused"):
            out.notes.append(f"{out.source_id} is paused (your choice); resume with "
                             f"`topos-node sources resume {out.source_id}`")
        out.done.append("source")
        return None
    if plan:
        return out.result(state="planned", step="source", exit_code=EXIT_NEEDS_HUMAN,
                          message=f"{DISPLAY[out.client]}'s session history will become a Topos source "
                                  f"(backfill {backfill}).", extra={"preview": _preview(out.source_id, skip)})
    doc, err = _owner_door("POST", f"/api/local/owner/sources/{out.source_id}/enable",
                           {"skip": skip, "backfill": backfill})
    if err == "not_paired":
        return out.result(state="needs_human", step="source", exit_code=EXIT_NEEDS_HUMAN,
                          human={"action": "wait", "say": "Topos is connecting to your account for the first "
                                                          "time. Run setup again in a minute."},
                          message="The node has its key but has not finished its first connection.")
    if err == "disk_floor":
        return out.result(state="needs_human", step="source", exit_code=EXIT_NEEDS_HUMAN,
                          human={"action": "free_disk",
                                 "say": "Topos keeps 10 GB of disk free and there is less than that now. "
                                        "Free up space, then ask me to finish setting up Topos."},
                          message="Refused below the disk floor.")
    if err is not None:
        return out.result(state="failed", step="source", exit_code=EXIT_FAILED,
                          message=f"Topos refused to enable {out.source_id} ({err}).")
    out.notes.append(f"{out.source_id} enabled; backfill {backfill} started, syncing hourly")
    out.done.append("source")
    return None


def run_setup(*, client: str, plan: bool = False, confirm_account: bool = False, service: bool = True,
              open_terminal: bool = True, backfill: str = "all", skip: Optional[List[str]] = None,
              port: Optional[int] = None) -> dict:
    from topos.defaults import DEFAULT_NODE_PORT

    port = int(port or DEFAULT_NODE_PORT)
    out = Outcome(client)
    skip = list(skip or [])
    for halt in (
        lambda: _step_key(out, plan=plan),
        lambda: None if plan else _step_account(out, confirm=confirm_account),
        lambda: _step_node(out, service=service, plan=plan, port=port),
        lambda: _step_agent(out, plan=plan, open_terminal=open_terminal),
        lambda: _step_source(out, plan=plan, backfill=backfill, skip=skip),
    ):
        stop = halt()
        if stop is not None:
            return stop
    if plan:
        return out.result(state="done", step="done", exit_code=EXIT_OK, message="Nothing left to set up.")
    extra: Dict[str, Any] = {}
    if out.source_id:
        doc, _err = _owner_door("GET", f"/api/local/owner/sources/{out.source_id}")
        if isinstance(doc, dict):
            extra["processing"] = doc.get("processing")
            extra["last_sync_at"] = doc.get("last_sync_at")
    return out.result(state="done", step="done", exit_code=EXIT_OK, extra=extra,
                      message=f"{DISPLAY[client]} is connected to your Topos"
                              + (" and its history is a source." if out.source_id else "."))


def _human_lines(doc: dict) -> List[str]:
    lines = []
    if doc.get("human") and doc["human"].get("say"):
        lines.append(doc["human"]["say"])
    if doc.get("message"):
        lines.append(doc["message"])
    lines.extend(doc.get("notes") or [])
    lines.append("Done: " + (", ".join(doc.get("done") or []) or "nothing yet") + f" (next: {doc['step']}).")
    return lines


@click.command("setup")
@click.option("--client", "client", type=click.Choice(CLIENTS), required=True,
              help="Which AI coding tool is setting up Topos.")
@click.option("--plan", is_flag=True, help="Change nothing: report the remaining steps and the preview counts.")
@click.option("--confirm-account", is_flag=True,
              help="The person confirmed the masked account that approved the pairing.")
@click.option("--backfill", type=click.Choice(["all", "30d"]), default="all", show_default=True,
              help="How much of the tool's history to read on first enable.")
@click.option("--skip", "skip", multiple=True, metavar="DIR", help="Project folder name to leave out (repeatable).")
@click.option("--no-service", is_flag=True, help="Start Topos in the background instead of as a login service.")
@click.option("--no-open-terminal", is_flag=True, help="Approve the agent in the browser instead of a Terminal prompt.")
@click.option("--port", type=int, default=None, help="Node port (default 8676). Topos.app is used only on the default port.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (never a key).")
def setup_command(client: str, plan: bool, confirm_account: bool, backfill: str, skip: tuple,
                  no_service: bool, no_open_terminal: bool, port: Optional[int], as_json: bool) -> None:
    """Set up Topos for an AI coding tool in one re-runnable command.

    Exit codes: 0 everything is set up · 2 a person needs to act (the output
    says what) · 1 a step failed · 4 Topos would not start · 5 pairing denied.
    """
    doc = run_setup(client=client, plan=plan, confirm_account=confirm_account, service=not no_service,
                    open_terminal=not no_open_terminal, backfill=backfill, skip=list(skip), port=port)
    if as_json:
        click.echo(json.dumps(doc, indent=2, sort_keys=True))
    else:
        for line in _human_lines(doc):
            click.echo(line)
    sys.exit(int(doc["exit_code"]))
