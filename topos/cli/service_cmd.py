"""`topos-node service install|uninstall|status` — Topos as a login service.

CONTRACTS §4 and PLAN §2.2: a LaunchAgent on macOS (``com.dialogues.topos-node``)
or a systemd user unit on Linux (``topos-node.service``) that runs
``<absolute topos-node> start --headless-foreground``. The unit never embeds
the key or any other secret — the node reads ``~/.topos/.env`` itself.

Facts from the Day-0 spikes that shape every line here:

* P0.5: ``launchctl getenv PATH`` is empty and launchd jobs get
  ``/usr/bin:/bin:/usr/sbin:/sbin``. So the unit names the binary by absolute
  path (``topos.cli.binary``), sets ``HOME``, and prepends the binary's folder
  and uv's folder to ``PATH`` so the node's own self-update can find ``uv``.
* P0.3: a launchd-run node LOSES iMessage — Full Disk Access is attributed
  to the responsible process, and today iMessage works only because the node
  is a child of Topos.app. The AI-tool stores need no FDA. So ``install`` says
  so, and declines when Topos.app manages the node.
* KeepAlive restarts the node when it stops; the foreground variant waits
  (never exits) while another node is running, so it cannot restart-loop.

``install`` declines under Topos.app by two signs: a ``ToposShell`` process
(the app's executable, bundle ``ai.dialogues.topos.shell``), or a node on the
port whose parent process is that executable.
"""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import click

from topos.cli.binary import resolve_node_binary
from topos.defaults import DEFAULT_NODE_PORT

LAUNCHD_LABEL = "com.dialogues.topos-node"
SYSTEMD_UNIT = "topos-node.service"
TOPOS_APP_EXECUTABLE = "ToposShell"
TOPOS_APP_BUNDLE_PATH = "Topos.app/Contents/MacOS/"
LAUNCHD_PATH_TAIL = "/usr/bin:/bin:/usr/sbin:/sbin"

IMESSAGE_NOTE = (
    "Messages (iMessage) sync needs the Topos app; this service runs Topos for everything else."
)
UNSUPPORTED_MESSAGE = "`topos-node service` isn't available on this platform yet (macOS and Linux only)."


class Declined(click.ClickException):
    exit_code = 2


# --- facts about the machine (each a hook tests replace) -----------------------


def _platform() -> str:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return "other"


def _run(cmd: List[str]) -> Tuple[int, str]:
    """Run a launchctl/systemctl command; (returncode, combined output)."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, type(exc).__name__
    return out.returncode, (out.stdout or "") + (out.stderr or "")


def _process_table() -> List[Tuple[int, int, str]]:
    """``(pid, ppid, command)`` for every process ``ps`` will show us."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,comm="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    rows: List[Tuple[int, int, str]] = []
    for line in out.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            continue
    return rows


def _parent_command(pid: int) -> Optional[str]:
    """The parent's command for one pid, when the table did not show it."""
    try:
        out = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        ppid = int(out.stdout.strip() or 0)
        if ppid <= 1:
            return None
        parent = subprocess.run(["ps", "-o", "comm=", "-p", str(ppid)], capture_output=True, text=True, timeout=5)
        return parent.stdout.strip() or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _probe_node(port: int) -> Optional[Dict[str, Any]]:
    from topos.cli.commands import _probe_running_node

    return _probe_running_node("127.0.0.1", port)


def _uv_bin_dir() -> Optional[str]:
    from topos.runtime_update import resolve_uv_binary

    found = resolve_uv_binary()
    return str(Path(found).parent) if found else None


def _is_topos_app(command: Optional[str]) -> bool:
    if not command:
        return False
    return Path(command).name == TOPOS_APP_EXECUTABLE or TOPOS_APP_BUNDLE_PATH in command


#: Values ``TOPOS_SUPERVISOR`` may carry; anything else reads as unset.
SUPERVISORS = ("launchd", "systemd", "service", "headless")


def current_supervisor() -> str:
    """Who keeps THIS node process alive, for ``status.node.supervisor`` (§4).

    ``launchd`` / ``systemd`` when started by the unit this module writes
    (each unit sets ``TOPOS_SUPERVISOR``), ``service`` for a hand-run
    ``start --headless-foreground``, ``headless`` for a detached
    ``start --headless``, ``topos-app`` when the parent process is the Topos
    app, else ``terminal``.
    """
    declared = (os.environ.get("TOPOS_SUPERVISOR") or "").strip().lower()
    if declared in SUPERVISORS:
        return declared
    if _is_topos_app(_parent_command(os.getpid())):
        return "topos-app"
    return "terminal"


def topos_app_manages_node(port: int) -> Optional[str]:
    """Why Topos.app owns the node here, or None when it does not."""
    table = _process_table()
    by_pid = {pid: (ppid, cmd) for pid, ppid, cmd in table}
    if any(_is_topos_app(cmd) for _, _, cmd in table):
        return "Topos.app is running"
    running = _probe_node(port)
    if running:
        pid = running.get("pid")
        try:
            pid = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            pid = None
        if pid is not None:
            parent_cmd = None
            if pid in by_pid:
                ppid = by_pid[pid][0]
                parent_cmd = by_pid.get(ppid, (None, None))[1]
            if parent_cmd is None:
                parent_cmd = _parent_command(pid)
            if _is_topos_app(parent_cmd):
                return "the running node was started by Topos.app"
    return None


# --- the units -------------------------------------------------------------------


def unit_path(platform_name: str) -> Path:
    if platform_name == "macos":
        return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_UNIT


def service_log_path() -> Path:
    return Path.home() / ".topos" / "logs" / "service.log"


def _service_path_env(binary: Path) -> str:
    parts = [str(binary.parent)]
    uv_dir = _uv_bin_dir()
    if uv_dir and uv_dir not in parts:
        parts.append(uv_dir)
    return ":".join(parts + [LAUNCHD_PATH_TAIL])


def program_arguments(binary: Path, port: int) -> List[str]:
    return [str(binary), "start", "--headless-foreground", "--port", str(port)]


def render_launchd_plist(binary: Path, port: int) -> bytes:
    home = Path.home()
    log = service_log_path()
    payload = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": program_arguments(binary, port),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "WorkingDirectory": str(home),
        "EnvironmentVariables": {
            "HOME": str(home),
            "PATH": _service_path_env(binary),
            "TOPOS_SKIP_UPDATE_CHECK": "1",
            "TOPOS_SUPERVISOR": "launchd",
        },
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    return plistlib.dumps(payload, sort_keys=False)


def render_systemd_unit(binary: Path, port: int) -> str:
    home = Path.home()
    return (
        "[Unit]\n"
        "Description=Topos node (your personal data engine)\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={' '.join(program_arguments(binary, port))}\n"
        "Restart=always\n"
        "RestartSec=5\n"
        f"Environment=HOME={home}\n"
        f"Environment=PATH={_service_path_env(binary)}\n"
        "Environment=TOPOS_SKIP_UPDATE_CHECK=1\n"
        "Environment=TOPOS_SUPERVISOR=systemd\n"
        f"WorkingDirectory={home}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _assert_no_secret(unit_text: str) -> None:
    """Belt and braces: the unit is built from paths only, but refuse to write
    one that carries a value from ``.env`` however it got there."""
    from topos.cli.commands import _user_env_path

    env_path = _user_env_path()
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    except OSError:
        lines = []
    for line in lines:
        if "=" in line:
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if len(value) >= 8 and value in unit_text:
                raise Declined("refusing to write a service unit that would contain a secret")


def _launchctl_domain() -> str:
    return f"gui/{os.getuid()}"


# --- commands ----------------------------------------------------------------------


_JSON_OPTION = click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
_PORT_OPTION = click.option("--port", default=DEFAULT_NODE_PORT, show_default=True, help="Port the node serves.")


@click.group("service")
def service_group() -> None:
    """Run Topos as a login service (LaunchAgent on macOS, systemd user unit on Linux)."""


@service_group.command("install")
@_PORT_OPTION
@click.pass_context
def service_install(ctx, port: int) -> None:
    """Install and start the service. Exit 2 when it declines."""
    platform_name = _platform()
    if platform_name not in ("macos", "linux"):
        raise Declined(UNSUPPORTED_MESSAGE)
    reason = topos_app_manages_node(port)
    if reason:
        raise Declined(
            f"Topos.app manages Topos on this computer ({reason}), so no service was installed. "
            f"{IMESSAGE_NOTE} Quit Topos.app first if you want the service instead."
        )
    binary = resolve_node_binary()
    if binary is None:
        raise Declined(
            "topos-node isn't installed where the service can find it (~/.local/bin, "
            "~/.topos/runtime/bin, /opt/homebrew/bin, /usr/local/bin). Install it with "
            "`uv tool install topos-node`, or set TOPOS_NODE_BIN to its path."
        )
    path = unit_path(platform_name)
    if platform_name == "macos":
        rendered = render_launchd_plist(binary, port)
        _assert_no_secret(rendered.decode("utf-8", errors="replace"))
    else:
        text = render_systemd_unit(binary, port)
        _assert_no_secret(text)
        rendered = text.encode("utf-8")
    service_log_path().parent.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(rendered)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass

    if platform_name == "macos":
        _run(["launchctl", "bootout", _launchctl_domain(), str(path)])
        rc, _out = _run(["launchctl", "bootstrap", _launchctl_domain(), str(path)])
        if rc != 0:
            raise Declined(
                f"The service file was written to {path}, but launchctl could not load it. "
                "Log out and back in, or load it yourself: "
                f"`launchctl bootstrap {_launchctl_domain()} {path}`."
            )
    else:
        rc, _out = _run(["systemctl", "--user", "daemon-reload"])
        if rc == 0:
            rc, _out = _run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT])
        if rc != 0:
            raise Declined(
                f"The unit was written to {path}, but systemctl could not enable it. "
                f"Try `systemctl --user enable --now {SYSTEMD_UNIT}` yourself."
            )

    click.echo(f"Installed {path.name}: Topos starts when you log in and restarts if it stops.")
    click.echo(f"Logs: {service_log_path()} and {Path.home() / '.topos' / 'logs' / 'node.log'}")
    click.echo(IMESSAGE_NOTE)
    click.echo("Check it with `topos-node status`; remove it with `topos-node service uninstall`.")


@service_group.command("uninstall")
@click.pass_context
def service_uninstall(ctx) -> None:
    """Stop and remove the service."""
    platform_name = _platform()
    if platform_name not in ("macos", "linux"):
        raise Declined(UNSUPPORTED_MESSAGE)
    path = unit_path(platform_name)
    if not path.exists():
        click.echo("The Topos service is not installed.")
        return
    if platform_name == "macos":
        _run(["launchctl", "bootout", _launchctl_domain(), str(path)])
    else:
        _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT])
    try:
        path.unlink()
    except OSError as exc:
        raise Declined(f"Could not remove {path}: {type(exc).__name__}")
    if platform_name == "linux":
        _run(["systemctl", "--user", "daemon-reload"])
    click.echo(f"Removed {path.name}. Topos will no longer start on its own.")


@service_group.command("status")
@_PORT_OPTION
@_JSON_OPTION
@click.pass_context
def service_status(ctx, port: int, as_json: bool) -> None:
    """Is the service installed, and is a node answering? Exit 0 when a node runs under this service or under Topos.app, else 2."""
    platform_name = _platform()
    path = unit_path(platform_name) if platform_name in ("macos", "linux") else None
    installed = bool(path and path.exists())
    loaded: Optional[bool] = None
    if installed and platform_name == "macos":
        rc, _out = _run(["launchctl", "print", f"{_launchctl_domain()}/{LAUNCHD_LABEL}"])
        loaded = rc == 0
    elif installed and platform_name == "linux":
        rc, _out = _run(["systemctl", "--user", "is-enabled", SYSTEMD_UNIT])
        loaded = rc == 0
    running = _probe_node(port)
    node_running = running is not None
    payload: Dict[str, Any] = {
        "platform": platform_name,
        "unit": str(path) if path else None,
        "installed": installed,
        "loaded": loaded,
        "node_running": node_running,
        "node_pid": (running or {}).get("pid"),
        "node_version": (running or {}).get("version"),
        "managed_by_app": topos_app_manages_node(port),
    }
    # 0 when Topos is being kept running — by this service or by Topos.app
    # (the plugin's setup check skips the service in that case); 2 otherwise,
    # including a node someone started by hand in a terminal.
    exit_code = 0 if node_running and (installed or payload["managed_by_app"]) else 2
    payload["exit_code"] = exit_code
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if not path:
            click.echo(UNSUPPORTED_MESSAGE)
        elif installed:
            click.echo(f"Service installed: {path}" + ("" if loaded in (None, True) else " (not loaded)"))
        else:
            click.echo("Service not installed. Install it with `topos-node service install`.")
        if node_running:
            click.echo(f"Topos is running on port {port}" + (f" (pid {payload['node_pid']})" if payload["node_pid"] else "") + ".")
        else:
            click.echo(f"No Topos node is answering on port {port}.")
        if payload["managed_by_app"]:
            # Both words on one line: the plugin's check keys on "Topos.app" or "managed".
            click.echo(f"Topos.app manages the node ({payload['managed_by_app']}) — managed by the app, no service needed.")
    ctx.exit(exit_code)
