"""`topos-node pair` — connect this computer to a Topos account; the key never shows.

CONTRACTS §3/§4 (AI-tool on-ramp, D3). The command asks the control plane for
a short code, prints the link and the code, and leaves a DETACHED poller
behind that writes ``~/.topos/pairing.json`` (0600) through every state —
pending → approved → delivered — saves the key to ``~/.topos/.env`` through
``_save_topos_key`` and then acks. A blocking pair would die with the agent's
two-minute tool call (critic M8), so ``pair`` itself exits 2 ("needs a human")
the moment the code is on screen; ``pair --wait`` and ``status`` read the file.

What never happens here: the key is never printed, never logged, never put in
``pairing.json`` or any file but ``.env``; ``device_code`` (the capability to
receive the key) never goes on a command line, so ``ps`` cannot show it —
the poller reads it back from the 0600 file. ``--manual`` is a hidden prompt
that refuses without a terminal: an agent cannot answer it, and the only
other way to "help" would be pasting the key into the chat.
"""

from __future__ import annotations

import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional

import click
import httpx

from topos.runtime_update import can_prompt_for_input

PAIRING_FILENAME = "pairing.json"
INSTALL_ID_FILENAME = "install-id"
CLIENTS = ("claude-code", "codex", "cursor", "gemini", "cli")
TERMINAL_STATES = frozenset({"approved", "delivered", "denied", "expired", "error"})

CP_404_MESSAGE = (
    "pairing isn't available on this control plane yet; "
    "run `topos-node pair --manual` in your own terminal"
)
MANUAL_NEEDS_TTY_MESSAGE = (
    "`topos-node pair --manual` needs a terminal. "
    "Open your own terminal (not the assistant's) and run it there."
)
CP_BUSY_MESSAGE = "the control plane is busy right now; try `topos-node pair` again in a few minutes"
CP_UNREACHABLE_MESSAGE = "could not reach the control plane; check the network and try `topos-node pair` again"

#: Poll errors tolerated before the poller gives up (each costs one interval).
MAX_POLL_ERRORS = 5
#: RFC 8628 §3.5: a `slow_down` adds 5 seconds to the interval.
SLOW_DOWN_STEP = 5
#: An expired code restarts the pairing from `start` (L8); this many times, then the poller stops.
MAX_RESTARTS = 3

# Re-exported so tests can monkeypatch pair_cmd._can_prompt_for_input.
_can_prompt_for_input = can_prompt_for_input
#: Test hook: an httpx transport for the fake control plane.
_transport: Optional[httpx.BaseTransport] = None


class NeedsHuman(click.ClickException):
    exit_code = 2


class Denied(click.ClickException):
    exit_code = 5


# --- paths and small facts -----------------------------------------------------


def topos_dir() -> Path:
    return Path.home() / ".topos"


def pairing_path() -> Path:
    return topos_dir() / PAIRING_FILENAME


def _write_private(path: Path, text: str) -> None:
    """Atomic 0600 write: a crash mid-write leaves the old record, never half of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def node_install_id() -> str:
    """A uuid minted once per install (``~/.topos/install-id``, 0600): the CP's
    idempotency key, so two polls after one approval create one Topos."""
    path = topos_dir() / INSTALL_ID_FILENAME
    try:
        existing = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except OSError:
        existing = ""
    if existing and re.fullmatch(r"[0-9a-f-]{36}", existing):
        return existing
    fresh = str(uuid.uuid4())
    _write_private(path, fresh + "\n")
    return fresh


def _computer_name() -> str:
    """The name the approve page shows as *claimed*: the owner's typed name on
    macOS, else the short host name de-mangled the way the web app does."""
    name = ""
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=2)
            if out.returncode == 0:
                name = out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            name = ""
    if not name:
        host = socket.gethostname().split(".")[0]
        name = re.sub(r"[-_]+", " ", host).strip()
    return (name or "This computer")[:64]


def current_platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _pid_alive(pid: Any) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


# --- pairing.json ---------------------------------------------------------------


_FORBIDDEN_RECORD_KEYS = ("topos_key", "key", "token", "label")


def read_pairing() -> Optional[Dict[str, Any]]:
    path = pairing_path()
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_pairing(record: Dict[str, Any]) -> None:
    """Persist the §3 record, 0600. Refuses any key-shaped field outright."""
    for name in _FORBIDDEN_RECORD_KEYS:
        if name in record:
            raise ValueError(f"pairing.json must never carry {name!r}")
    record = dict(record)
    record["updated_at"] = _now().isoformat()
    _write_private(pairing_path(), json.dumps(record, indent=2, sort_keys=True) + "\n")


def public_view(record: Dict[str, Any]) -> Dict[str, Any]:
    """What a command may print: the record without the device code."""
    return {k: v for k, v in record.items() if k != "device_code"}


# --- control plane -------------------------------------------------------------


def _cp_http_base() -> str:
    """The HTTP origin of the control plane the node dials over websocket."""
    # Pairing is unauthenticated and must work before a node key exists.
    # Importing Settings here validates TOPOS_KEY and prevents first pairing.
    from topos.defaults import DEFAULT_TOPOS_CONTROL_PLANE_URL

    raw = str(os.environ.get("TOPOS_CONTROL_PLANE_URL") or DEFAULT_TOPOS_CONTROL_PLANE_URL or "").strip()
    if not raw:
        return "https://cp.logu3s.com"
    if raw.startswith("wss://"):
        raw = "https://" + raw[len("wss://"):]
    elif raw.startswith("ws://"):
        raw = "http://" + raw[len("ws://"):]
    for marker in ("/ws/engine", "/ws"):
        if raw.endswith(marker):
            raw = raw[: -len(marker)]
            break
    return raw.rstrip("/")


def _http_client() -> httpx.Client:
    kwargs: Dict[str, Any] = {"base_url": _cp_http_base(), "timeout": 15.0}
    if _transport is not None:
        kwargs["transport"] = _transport
    return httpx.Client(**kwargs)


def start_pairing(*, client_name: str, mode: str) -> Dict[str, Any]:
    body = {
        "computer_name": _computer_name(),
        "platform": current_platform(),
        "client": client_name,
        "node_install_id": node_install_id(),
        # The control plane stores the mode and the approve page shows it
        # (contract change 22:05Z). One approval covers the Topos AND the
        # agent: the poll result carries `agent: {client}` in both modes.
        "mode": mode,
    }
    try:
        with _http_client() as http:
            response = http.post("/v1/topos/device-pairing/start", json=body)
    except httpx.HTTPError:
        raise NeedsHuman(CP_UNREACHABLE_MESSAGE)
    if response.status_code == 404:
        if mode == "add_agent":
            raise NeedsHuman(
                "browser approval isn't available on this control plane yet; "
                f"open its approval prompt with `topos-node agents add {client_name} --open-terminal` on macOS"
            )
        raise NeedsHuman(CP_404_MESSAGE)
    if response.status_code in (429, 503):
        raise NeedsHuman(CP_BUSY_MESSAGE)
    if response.status_code != 200:
        # Never the body: it is the control plane's text, not ours to print.
        raise NeedsHuman(f"the control plane refused to start pairing (HTTP {response.status_code}); try again later")
    try:
        data = response.json()
    except ValueError:
        raise NeedsHuman("the control plane answered with something that isn't pairing data; try again later")
    for field in ("device_code", "user_code", "verification_url", "expires_at"):
        if not data.get(field):
            raise NeedsHuman("the control plane answered with incomplete pairing data; try again later")
    return {
        "state": "pending",
        "user_code": str(data["user_code"]),
        "verification_url": str(data["verification_url"]),
        "device_code": str(data["device_code"]),
        "expires_at": str(data["expires_at"]),
        "interval": int(data.get("interval") or 5),
        "started_at": _now().isoformat(),
        "mode": mode,
        "client": client_name,
        "poller_pid": None,
    }


# --- the detached poller --------------------------------------------------------


def _poller_argv() -> list:
    from topos.cli.binary import resolve_node_binary

    found = resolve_node_binary()
    base = [str(found)] if found is not None else [sys.executable, "-m", "topos.cli"]
    return base + ["pair", "--poll"]


def _spawn_poller() -> int:
    """Start the poller in its own session so the parent (and the agent's tool
    call) can return. Output is discarded: the poller writes a file, not a terminal."""
    proc = subprocess.Popen(
        _poller_argv(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        cwd=str(Path.home()),
        start_new_session=True,
        close_fds=True,
    )
    return proc.pid


def run_poller(*, sleep: Callable[[float], None] = time.sleep, now: Callable[[], datetime] = _now) -> str:
    """Poll until a terminal state; return it. Reads the device code from the file.

    On ``approved`` in create mode the key is saved FIRST (``_save_topos_key``,
    0600), the record is written without it, and only then is the ack sent —
    the control plane archives a Topos that is approved but never acked, so
    the ack must mean "the key is on disk".
    """
    from topos.cli.commands import _save_topos_key, _user_env_path

    record = read_pairing()
    if not record or record.get("state") != "pending" or not record.get("device_code"):
        return "error"
    record["poller_pid"] = os.getpid()
    write_pairing(record)
    device_code = str(record["device_code"])
    interval = max(1, int(record.get("interval") or 5))
    expires_at = _parse_iso(record.get("expires_at"))
    errors = 0
    restarts = int(record.get("restarts") or 0)

    def _finish(state: str, **fields: Any) -> str:
        record.update(fields)
        record["state"] = state
        write_pairing(record)
        return state

    def _restart() -> bool:
        """An expired code: ask for a fresh one and keep going (L8), at most MAX_RESTARTS times."""
        nonlocal record, device_code, interval, expires_at, restarts, errors
        if restarts >= MAX_RESTARTS:
            return False
        try:
            fresh = start_pairing(client_name=str(record.get("client") or "cli"), mode=str(record.get("mode") or "create"))
        except click.ClickException as exc:
            record["error"] = exc.message
            return False
        restarts += 1
        fresh.update(restarts=restarts, restarted_from=record.get("user_code"), poller_pid=os.getpid())
        if record.get("notice"):
            fresh["notice"] = record["notice"]
        record = fresh
        write_pairing(record)
        device_code = str(record["device_code"])
        interval = max(1, int(record.get("interval") or 5))
        expires_at = _parse_iso(record.get("expires_at"))
        errors = 0
        return True

    with _http_client() as http:
        while True:
            sleep(interval)
            if expires_at is not None and now() >= expires_at:
                if _restart():
                    continue
                return _finish("expired")
            try:
                response = http.post("/v1/topos/device-pairing/poll", json={"device_code": device_code})
            except httpx.HTTPError:
                errors += 1
                if errors >= MAX_POLL_ERRORS:
                    return _finish("error", error=CP_UNREACHABLE_MESSAGE)
                continue
            if response.status_code == 429:
                interval += SLOW_DOWN_STEP
                record["interval"] = interval
                write_pairing(record)
                continue
            if response.status_code == 404:
                if _restart():
                    continue
                return _finish("expired")
            if response.status_code == 409:
                # The five-Topos cap (L8): say what the control plane said, keep polling at
                # `interval` — the owner may archive a Topos and the next poll creates ours.
                try:
                    detail = response.json().get("detail")
                except ValueError:
                    detail = None
                record["notice"] = str(detail or "the control plane could not create the Topos yet")[:300]
                write_pairing(record)
                continue
            if response.status_code != 200:
                errors += 1
                if errors >= MAX_POLL_ERRORS:
                    return _finish("error", error=f"the control plane kept answering HTTP {response.status_code}")
                continue
            try:
                data = response.json()
            except ValueError:
                errors += 1
                continue
            status = str(data.get("status") or "")
            if status == "pending":
                errors = 0
                continue
            if status == "denied":
                return _finish("denied")
            if status == "expired":
                if _restart():
                    continue
                return _finish("expired")
            if status == "delivered":
                # Terminal-ok (L8): the key was handed over on an earlier poll; nothing to ack.
                return _finish(
                    "delivered",
                    masked_account=data.get("masked_account") or record.get("masked_account"),
                    topos_id=data.get("topos_id") or record.get("topos_id"),
                    agent=data.get("agent") if isinstance(data.get("agent"), dict) else record.get("agent"),
                    delivered_at=now().isoformat(),
                )
            if status != "approved":
                return _finish("error", error="the control plane answered with an unknown pairing state")

            key = str(data.get("topos_key") or "")
            if record.get("mode") == "create":
                if not key:
                    return _finish("error", error="approved, but the control plane sent no key")
                try:
                    _save_topos_key(key, env_path=_user_env_path())
                except OSError as exc:
                    # The one-time key could not land: say where and why (the kind only), so `--wait` and
                    # `status` show the fix instead of "the helper stopped".
                    del key
                    return _finish("error", error=f"approved, but the key could not be saved to {_user_env_path()} "
                                                  f"({type(exc).__name__}); make ~/.topos writable and pair again")
            del key
            # `agent` is recorded in BOTH modes: a fresh pairing started by an
            # assistant approves that assistant too, and `agents add --approved`
            # (L3) reads this record either way.
            agent = data.get("agent") if isinstance(data.get("agent"), dict) else None
            record.update(
                masked_account=data.get("masked_account"),
                topos_id=data.get("topos_id"),
                agent=agent,
                approved_at=now().isoformat(),
            )
            _finish("approved")
            for attempt in range(3):
                try:
                    ack = http.post("/v1/topos/device-pairing/ack", json={"device_code": device_code})
                except httpx.HTTPError:
                    ack = None
                if ack is not None and ack.status_code in (200, 204):
                    return _finish("delivered", delivered_at=now().isoformat())
                if attempt < 2:
                    sleep(interval)
            return _finish("approved", ack_error="the control plane did not confirm delivery; the key is saved")


def _note_poller_pid(record: Dict[str, Any], pid: int) -> None:
    """Put the child's pid on the record the parent just wrote, unless the child got there first.

    The poller records its own pid, but it takes a moment to start, and a record without one reads as a
    helper that died: `pair --wait` in one command, or a `pair` re-run a moment later, would give up (or
    start a second pairing) before the child wrote anything. Only the still-pending record the parent left
    (same code, no pid) is touched: a child that already advanced the state is never rolled back."""
    current = read_pairing()
    if (not current or current.get("state") != "pending" or current.get("poller_pid")
            or current.get("user_code") != record.get("user_code")):
        return
    current["poller_pid"] = pid
    write_pairing(current)


# --- the command ---------------------------------------------------------------


def _print_code(record: Dict[str, Any]) -> None:
    click.echo("")
    click.echo(f"Open {record['verification_url']} and type this code:")
    click.echo("")
    click.echo(f"    {record['user_code']}")
    click.echo("")
    if record.get("mode") == "add_agent":
        click.echo(f"Approving lets {record.get('client')} read from your Topos.")
    else:
        click.echo("Approving creates a Topos on this computer; your data stays here.")
    expires = _parse_iso(record.get("expires_at"))
    if expires is not None:
        minutes = max(0, int((expires - _now()).total_seconds() // 60))
        click.echo(f"The code expires in {minutes} minutes.")
    click.echo("Topos finishes pairing on its own. Run `topos-node pair --wait` or `topos-node status` to see progress.")


def _emit(record: Dict[str, Any], *, as_json: bool, exit_code: int) -> None:
    if as_json:
        payload = public_view(record)
        payload["pairing_file"] = str(pairing_path())
        payload["exit_code"] = exit_code
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_code(record)


def _reusable_pending(record: Optional[Dict[str, Any]]) -> bool:
    if not record or record.get("state") != "pending":
        return False
    expires = _parse_iso(record.get("expires_at"))
    if expires is None or _now() >= expires:
        return False
    return _pid_alive(record.get("poller_pid"))


def _wait_sleep(seconds: float) -> None:
    time.sleep(seconds)


def _wait(ctx: click.Context, *, as_json: bool, sleep: Optional[Callable[[float], None]] = None) -> None:
    sleep = sleep or _wait_sleep
    deadline = None
    shown_notice = None
    shown_code = None
    while True:
        record = read_pairing()
        if record is None:
            raise NeedsHuman("No pairing in progress. Run `topos-node pair` to start one.")
        expires = _parse_iso(record.get("expires_at"))
        if deadline is None or str(record.get("user_code")) != shown_code:
            # A restarted pairing (expired code) carries a new code and a new expiry.
            deadline = (expires or (_now() + timedelta(minutes=10))) + timedelta(seconds=60)
        state = str(record.get("state") or "")
        if state == "pending" and not as_json:
            if record.get("user_code") != shown_code:
                shown_code = str(record.get("user_code"))
                _print_code(record)
            notice = record.get("notice")
            if notice and notice != shown_notice:
                shown_notice = notice
                click.echo(f"Control plane: {notice}")
        elif state == "pending":
            shown_code = str(record.get("user_code"))
        if state in ("approved", "delivered"):
            if as_json:
                _emit(record, as_json=True, exit_code=0)
            else:
                account = record.get("masked_account") or "your account"
                click.echo(f"Paired with {account}. Is that you?")
                click.echo(
                    "If not, stop here: delete ~/.topos/.env, run `topos-node service uninstall` if you "
                    "installed the service, and run `topos-node pair` again from your own terminal."
                )
                agent = record.get("agent") or {}
                if agent.get("client"):
                    click.echo(f"{agent['client']} was approved too: `topos-node agents add {agent['client']} --approved`.")
                if record.get("mode") != "add_agent":
                    click.echo("Next: `topos-node start --headless` (or `topos-node service install`).")
            ctx.exit(0)
        if state == "denied":
            if as_json:
                _emit(record, as_json=True, exit_code=5)
                ctx.exit(5)
            raise Denied("Pairing was denied from the account's side. Nothing was changed on this computer.")
        if state == "expired":
            if as_json:
                _emit(record, as_json=True, exit_code=2)
                ctx.exit(2)
            raise NeedsHuman("The code expired before it was approved. Run `topos-node pair` for a new one.")
        if state == "error":
            if as_json:
                _emit(record, as_json=True, exit_code=2)
                ctx.exit(2)
            raise NeedsHuman(f"Pairing stopped: {record.get('error') or 'unknown error'}. Run `topos-node pair` to try again.")
        if not _pid_alive(record.get("poller_pid")):
            raise NeedsHuman("The pairing helper stopped before the code was approved. Run `topos-node pair` again.")
        if _now() > deadline:
            raise NeedsHuman("Still waiting for approval. Run `topos-node pair --wait` again, or `topos-node pair` for a new code.")
        sleep(1.0)


def _manual(ctx: click.Context) -> None:
    from topos.cli.commands import _save_topos_key, _user_env_path

    if not _can_prompt_for_input():
        raise NeedsHuman(MANUAL_NEEDS_TTY_MESSAGE)
    click.echo("Paste the key from the Topos web app. It is not shown as you type.")
    while True:
        key = click.prompt("TOPOS_KEY", hide_input=True).strip()
        if key:
            break
        click.echo("The key cannot be empty. Please try again.")
    saved = _save_topos_key(key, env_path=_user_env_path())
    del key
    click.echo(f"Saved to {saved}.")
    click.echo("Next: `topos-node start --headless` (or `topos-node service install`).")
    ctx.exit(0)


@click.command("pair")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output (never the key).")
@click.option("--wait", "wait", is_flag=True, help="Block until approved, denied or expired.")
@click.option("--manual", is_flag=True, help="Type the key yourself. Needs a terminal.")
@click.option("--client", "client_name", type=click.Choice(CLIENTS), default="cli", show_default=True,
              help="Which assistant is asking (shown on the approve page).")
@click.option("--add-agent", is_flag=True, help="Let the named assistant read from an already-paired Topos.")
@click.option("--poll", "poll", is_flag=True, hidden=True)
@click.pass_context
def pair_command(ctx, as_json: bool, wait: bool, manual: bool, client_name: str, add_agent: bool, poll: bool) -> None:
    """Connect this computer to your Topos account without ever showing the key.

    Exit codes: 0 paired · 2 a person needs to approve the code (or type the
    key) · 5 denied.
    """
    if poll:
        run_poller()
        ctx.exit(0)
    if manual:
        _manual(ctx)
        return

    mode = "add_agent" if add_agent else "create"
    if add_agent:
        if client_name == "cli":
            raise NeedsHuman("`--add-agent` needs `--client` (claude-code, codex, cursor or gemini).")
        from topos.cli.commands import _read_env_file_key, _user_env_path

        if not _read_env_file_key(_user_env_path()):
            raise NeedsHuman("Pair this computer first (`topos-node pair`), then add the assistant.")

    record = read_pairing()
    if _reusable_pending(record) and record.get("mode") == mode and record.get("client") == client_name:
        if wait:
            _wait(ctx, as_json=as_json)
            return
        _emit(record, as_json=as_json, exit_code=2)
        ctx.exit(2)

    record = start_pairing(client_name=client_name, mode=mode)
    write_pairing(record)
    pid = _spawn_poller()
    record["poller_pid"] = pid
    _note_poller_pid(record, pid)
    if wait:
        _wait(ctx, as_json=as_json)
        return
    _emit(record, as_json=as_json, exit_code=2)
    ctx.exit(2)
