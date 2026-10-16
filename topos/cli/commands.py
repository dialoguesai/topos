"""CLI entry points for Topos."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import click
import uvicorn

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from topos.defaults import DEFAULT_NODE_PORT
from topos.runtime_update import (
    DEFAULT_PACKAGE_NAME,
    apply_package_update,
    can_prompt_for_input,
    check_for_update,
    get_installed_package_version,
    get_latest_pypi_version,
    get_module_version,
    should_skip_update_check,
)
from topos.storage.db.paths import discover_databases
from topos.extensions import load_extensions
from topos.startup_banner import emit_startup_banner

load_extensions()

USER_ENV_PATH = Path.home() / ".topos" / ".env"
LEGACY_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# Re-exported for tests that monkeypatch commands.*
_get_installed_package_version = get_installed_package_version
_get_latest_pypi_version = get_latest_pypi_version
_get_module_version = get_module_version
_can_prompt_for_input = can_prompt_for_input


class NeedsHumanError(click.ClickException):
    """Exit 2 — "needs a human" in the §4 exit-code table (click's default is 1)."""

    exit_code = 2


#: `--set-topos-key KEY` / `--topos-key KEY` put the key in argv, `ps`, shell
#: history and the agent's transcript (security finding 9); without a terminal
#: the only caller is an agent, so refuse and name the flow that never shows it.
KEY_FLAGS_NEED_TTY_MESSAGE = (
    "`--set-topos-key` and `--topos-key` need a terminal. Run `topos-node pair` instead — it "
    "connects this computer without ever showing the key."
)
KEY_NOT_CONFIGURED_MESSAGE = (
    "TOPOS_KEY is not configured. Run `topos-node pair` to connect this computer (the key never "
    "appears on screen), or `topos-node pair --manual` in your own terminal."
)


def _user_env_path() -> Path:
    """``~/.topos/.env``, or ``$TOPOS_ENV_FILE`` when set.

    Resolved at call time, not import time: the HOME the node serves is the
    one it is started under, and the shell app and the test suite both point
    ``TOPOS_ENV_FILE`` elsewhere (the same hook ``owner_key.py`` honours).
    """
    from topos.owner_key import _env_path

    return _env_path()


def _read_env_file_key(env_path: Path) -> str | None:
    try:
        text = env_path.read_text(encoding="utf-8") if env_path.is_file() else ""
    except OSError:
        return None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("TOPOS_KEY="):
            value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            return value or None
    return None


def _apply_env_file_key_policy(env_path: Path) -> str | None:
    """Make the env FILE the source of truth for ``TOPOS_KEY``; return a warning to show, or None.

    Resolution order for the node's key, highest first (DIGEST J5, critic N7):

    1. ``--topos-key KEY`` (terminal only): this run, not saved.
    2. ``TOPOS_KEY`` in ``~/.topos/.env`` (or ``$TOPOS_ENV_FILE``): the key this
       computer is paired with. It wins over the shell.
    3. ``TOPOS_KEY`` exported in the shell: used only when the file has none
       (containers, CI). When both exist and differ, the exported value is
       IGNORED and this warning is shown — before this change it silently won,
       so a stale ``export TOPOS_KEY=…`` in a dotfile ran the node as another
       Topos than the one the file (and the Topos app) named.
    4. The legacy ``.env`` beside the package: fills variables still unset.
    5. A terminal prompt, saved to the file.
    6. No terminal: the ``KEY_NOT_CONFIGURED_MESSAGE`` error, which names ``pair``.
    """
    file_key = _read_env_file_key(env_path)
    if not file_key:
        return None
    exported = (os.environ.get("TOPOS_KEY") or "").strip()
    os.environ["TOPOS_KEY"] = file_key
    if exported and exported != file_key:
        return (
            "Ignoring TOPOS_KEY from the shell environment: it differs from the key in "
            f"{env_path}, which is the one this computer is paired with. Unset it or run "
            "`topos-node pair` again."
        )
    return None


def _get_runtime_version(package_name: str = DEFAULT_PACKAGE_NAME) -> str:
    return _get_module_version() or _get_installed_package_version(package_name) or "unknown"


def _load_env_file(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _save_topos_key(topos_key: str, env_path: Path = USER_ENV_PATH) -> Path:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    updated = False
    new_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("TOPOS_KEY="):
            new_lines.append(f"TOPOS_KEY={topos_key}")
            updated = True
        else:
            new_lines.append(line)

    if not updated:
        if new_lines and new_lines[-1].strip():
            new_lines.append("")
        new_lines.append(f"TOPOS_KEY={topos_key}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    try:
        env_path.chmod(0o600)
    except OSError:
        # Best-effort permissions (e.g. may fail on some filesystems).
        pass
    return env_path


def _prompt_for_topos_key() -> str:
    click.echo("TOPOS_KEY is required to connect Topos Node.")
    click.echo("Enter your TOPOS_KEY to save it locally and continue.")
    while True:
        key = click.prompt("TOPOS_KEY", hide_input=True).strip()
        if key:
            return key
        click.echo("TOPOS_KEY cannot be empty. Please try again.")


def _resolve_topos_key(cli_topos_key: str | None, env_path: Path = USER_ENV_PATH) -> str:
    if cli_topos_key:
        os.environ["TOPOS_KEY"] = cli_topos_key
        return cli_topos_key

    existing_key = (os.getenv("TOPOS_KEY") or "").strip()
    if existing_key:
        os.environ["TOPOS_KEY"] = existing_key
        return existing_key

    if _can_prompt_for_input():
        prompted_key = _prompt_for_topos_key()
        saved_path = _save_topos_key(prompted_key, env_path=env_path)
        os.environ["TOPOS_KEY"] = prompted_key
        click.echo(f"Saved TOPOS_KEY to {saved_path}")
        click.echo("Connecting with saved TOPOS_KEY...")
        return prompted_key

    raise NeedsHumanError(KEY_NOT_CONFIGURED_MESSAGE)


def _emit_startup_banner(host: str, port: int, package_name: str = DEFAULT_PACKAGE_NAME) -> None:
    runtime_version = _get_runtime_version(package_name=package_name)
    emit_startup_banner(
        click.echo,
        version=runtime_version,
        mode="cli",
        bind=f"{host}:{port}",
    )
    # Prevent duplicate banner when app startup runs in same process.
    os.environ["TOPOS_STARTUP_BANNER_EMITTED"] = "1"


def _resolve_bind_host(explicit: str | None) -> str:
    """Bind LOOPBACK by default — a personal node is not a LAN service.

    The old default was 0.0.0.0, which published the node's whole authenticated
    API to every device on the network: the only thing between a coffee-shop
    peer and the owner's data was a long-lived bearer token that lives in a
    dotfile, rides along in backups, and used to be pasted into MCP configs.
    Loopback removes the network as an attack surface entirely.

    Escapes, in order: an explicit --host wins; TOPOS_HOST covers containers
    and deliberate LAN exposure; hosted-pool nodes still need 0.0.0.0 because
    their traffic arrives from outside the container's loopback.
    """
    if explicit:
        return explicit
    env_host = str(os.environ.get("TOPOS_HOST") or "").strip()
    if env_host:
        return env_host
    try:
        from topos.config.settings import settings as _s

        if bool(getattr(_s, "hosted_pool_lease_enabled", False)):
            return "0.0.0.0"
    except Exception:  # noqa: BLE001 — settings trouble must not change the bind
        pass
    return "127.0.0.1"


def _probe_running_node(host: str, port: int) -> dict | None:
    """Return the running node's shell status (or minimal version dict) if one
    is already serving this port; None when the port is free or not a topos node."""
    import httpx

    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    base = f"http://{probe_host}:{port}"
    try:
        response = httpx.get(f"{base}/v1/shell/status", timeout=2.0)
        if response.status_code == 200 and response.json().get("name") == "topos-node":
            return response.json()
    except Exception:
        return None
    try:
        # Older node without the shell endpoint.
        response = httpx.get(f"{base}/version", timeout=2.0)
        if response.status_code == 200 and "version" in response.json():
            return {"version": response.json()["version"]}
    except Exception:
        return None
    return None


def _maybe_offer_self_update(
    skip_update_check: bool,
    package_name: str = DEFAULT_PACKAGE_NAME,
) -> bool:
    if should_skip_update_check(cli_skip=skip_update_check):
        return False

    info = check_for_update(package_name)
    if not info:
        return False

    click.echo(f"Update available for {package_name}: {info.installed} -> {info.latest}")
    if not _can_prompt_for_input():
        click.echo(f"Run `uv tool upgrade {package_name}` to update.")
        return False

    if not click.confirm("Install the latest version now?", default=True):
        return False

    click.echo(f"Updating {package_name}...")
    if not apply_package_update(package_name):
        click.echo("Update failed. Continuing with current version.")
        return False

    click.echo("Update installed. Please re-run `topos-node` to use the latest version.")
    return True


def _print_version(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    """`topos-node --version`: exactly one line, `topos-node <version>` (the plugin's setup check parses it)."""
    if not value or ctx.resilient_parsing:
        return
    click.echo(f"topos-node {_get_runtime_version()}")
    ctx.exit(0)


@click.group(invoke_without_command=True)
@click.option(
    "--version",
    is_flag=True,
    expose_value=False,
    is_eager=True,
    callback=_print_version,
    help="Print the installed topos-node version and exit.",
)
@click.option(
    "--db-path",
    help="Database file path (SQLite). If not specified, uses auto-discovery.",
)
@click.option(
    "--topos-key",
    help="Topos key for this run (overrides saved key).",
)
@click.option(
    "--set-topos-key",
    metavar="KEY",
    help="Save TOPOS_KEY to ~/.topos/.env and exit.",
)
@click.option(
    "--discover",
    is_flag=True,
    help="Show discovered databases and exit",
)
@click.option(
    "--port",
    default=DEFAULT_NODE_PORT,
    help=f"Server port (default: {DEFAULT_NODE_PORT})",
)
@click.option(
    "--host",
    default=None,
    help="Host to bind to (default: 127.0.0.1; 0.0.0.0 in hosted mode or via TOPOS_HOST).",
)
@click.option(
    "--skip-update-check",
    is_flag=True,
    help="Skip checking PyPI for a newer topos-node version.",
)
@click.option(
    "--tray/--no-tray",
    "tray",
    default=None,
    help="Show a menu-bar/system-tray status icon (default: auto-detect GUI).",
)
@click.option(
    "--app",
    "app_mode",
    is_flag=True,
    help="App mode: tray icon only, logs to ~/.topos/logs/node.log instead of the terminal.",
)
@click.pass_context
def main(
    ctx,
    db_path,
    topos_key,
    set_topos_key,
    discover,
    port,
    host,
    skip_update_check,
    tray,
    app_mode,
) -> None:
    """Topos Control Plane API entry point.

    Subcommands:
      reprocess   Re-run raw→canonical for a source (recovery utility).
      profile     Manage multiple Topoi on this machine (list, new, switch, remove).
    """
    if ctx.invoked_subcommand is not None:
        return

    if (set_topos_key or topos_key) and not _can_prompt_for_input():
        raise NeedsHumanError(KEY_FLAGS_NEED_TTY_MESSAGE)

    if set_topos_key:
        env_path = _save_topos_key(set_topos_key, env_path=_user_env_path())
        click.echo(f"Saved TOPOS_KEY to {env_path}")
        click.echo("You can now run: topos-node")
        return

    if discover:
        # Says which database is SERVED and flags any that belong to no Topos.
        # A bare list of paths was the wrong shape for the question people ask
        # this: it named a legacy stray first and never mentioned the active
        # slot, so the answer was not just incomplete, it was misleading.
        from topos.storage.db.paths import (
            SOURCE_LEGACY,
            existing_legacy_databases,
            resolve_active_database,
        )

        active = resolve_active_database()
        owner = f" — Topos '{active.profile_id}'" if active.profile_id else ""
        state = "" if active.path.is_file() else "  (not created yet)"
        click.echo(f"Active database: {active.path}{owner}{state}")
        if active.source == SOURCE_LEGACY:
            click.echo(
                "  WARNING: this database is outside ~/.topos and belongs to no Topos. "
                "Switching Topoi will not carry it. Run `topos-node profile adopt` "
                "to pull it into the active Topos."
            )
        strays = [p for p in existing_legacy_databases() if p != active.path]
        if strays:
            click.echo("Databases from older installs (not served):")
            for db in strays:
                click.echo(f"  - {db}")
        other = [
            db for db in discover_databases() if db != active.path and db not in strays
        ]
        for db in other:
            click.echo(f"Also on disk: {db}")
        return

    _run_node(
        db_path=db_path,
        topos_key=topos_key,
        port=port,
        host=host,
        skip_update_check=skip_update_check,
        tray=tray,
        app_mode=app_mode,
    )


def _run_node(
    *,
    db_path: str | None,
    topos_key: str | None,
    port: int,
    host: str | None,
    skip_update_check: bool,
    tray: bool | None,
    app_mode: bool,
) -> None:
    """Boot the node in THIS process (the body of a bare ``topos-node``)."""
    if skip_update_check:
        os.environ["TOPOS_SKIP_UPDATE_CHECK"] = "1"

    if _maybe_offer_self_update(skip_update_check=skip_update_check):
        return

    user_env_path = _user_env_path()
    ignored_key_warning = _apply_env_file_key_policy(user_env_path)
    if ignored_key_warning:
        click.echo(ignored_key_warning, err=True)
    _load_env_file(user_env_path)
    _load_env_file(LEGACY_ENV_PATH)

    host = _resolve_bind_host(host)

    if app_mode and tray is None:
        tray = True

    # Attach, don't double-start: two nodes on one ~/.topos DB is a corruption
    # risk, and for launchers "make sure Topos is running" should be idempotent.
    running = _probe_running_node(host, port)
    if running is not None:
        running_version = running.get("version", "unknown")
        from topos.cli import tray as tray_module

        if tray_module.should_enable_tray(cli_flag=tray):
            click.echo(
                f"Topos Node v{running_version} is already running on port {port} — "
                "attaching tray to it (closing this tray leaves the node running)."
            )
            log_file = running.get("log_file")
            tray_module.attach_tray(
                host=host,
                port=port,
                version=running_version,
                package_name=DEFAULT_PACKAGE_NAME,
                log_path=Path(log_file) if log_file else None,
            )
            return
        raise click.ClickException(
            f"Topos Node v{running_version} is already running on port {port}. "
            "Use --port to start a second instance."
        )

    _resolve_topos_key(topos_key)

    if app_mode:
        # Must happen before `topos.app` is imported — configure_logging() runs
        # at import time and reads TOPOS_LOG_FILE to pick file vs stdout.
        from topos.core.logging import default_node_log_path

        os.environ.setdefault("TOPOS_LOG_FILE", str(default_node_log_path()))
        click.echo(f"App mode: logs → {os.environ['TOPOS_LOG_FILE']}")

    from topos.app import app
    from topos.core.logging import get_uvicorn_log_config

    if db_path:
        os.environ["TOPOS_DATABASE_PATH"] = db_path
        click.echo(f"Database path: {db_path}")

    from topos.runtime_housekeeping import start_background_housekeeping

    start_background_housekeeping()

    _emit_startup_banner(host=host, port=port)
    try:
        from topos.core.state import get_db_connection
        from topos.upgrades.runner import runner_status

        _conn = get_db_connection()
        if _conn is not None:
            _pending = (runner_status(_conn).get("pending_consent_steps") or [])
            if _pending:
                click.echo("")
                click.echo("Upgrade steps waiting for your consent:")
                for step in _pending:
                    click.echo(
                        f"  • {step.get('id')}: {step.get('title') or step.get('why') or ''}"
                        f" (cost={step.get('cost') or 'slow'})"
                    )
                click.echo(
                    "Approve with: POST /v1/upgrade/consent {\"step_id\": \"...\"} "
                    "or the Engine settings banner."
                )
                click.echo("")
    except Exception:
        pass

    from topos.cli import tray as tray_module

    if tray_module.should_enable_tray(cli_flag=tray):
        from topos.core.logging import get_log_file_path

        tray_module.serve_with_tray(
            app,
            host=host,
            port=port,
            log_config=get_uvicorn_log_config(),
            version=_get_runtime_version(),
            package_name=DEFAULT_PACKAGE_NAME,
            log_path=get_log_file_path(),
        )
    else:
        from topos import node_stop

        node_stop.enable_exit_deadline()
        uvicorn.run(app, host=host, port=port, log_config=get_uvicorn_log_config(), **node_stop.uvicorn_options())


#: How long the launchd foreground variant waits between looks at a node that
#: is already running (CONTRACTS §4: wait, never exit — KeepAlive would loop).
HEADLESS_FOREGROUND_WAIT_SECONDS = 30


#: How long `start --headless` waits for the spawned node to answer /v1/shell/status.
HEADLESS_START_TIMEOUT_SECONDS = 60.0  # a first boot runs migrations and the upgrade steps before it binds (P0.8)


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _monotonic() -> float:
    import time

    return time.monotonic()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _wait_for_node(host: str, port: int, pid: int, timeout: float) -> str:
    """``running`` once /v1/shell/status answers; ``dead`` if the child exits first; ``silent`` at the timeout."""
    started = _monotonic()
    while True:
        if _probe_running_node(host, port) is not None:
            return "running"
        if not _pid_alive(pid):
            return "dead"
        if _monotonic() - started >= timeout:
            return "silent"
        _sleep(0.5)


def _port_accepts_connections(host: str, port: int) -> bool:
    """A raw TCP connect — no HTTP, no app import, no database."""
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _single_node_preflight(host: str | None, port: int) -> tuple[int, str] | None:
    """Before ANY boot work: is something already on the port or the socket?

    Spike P0.8: uvicorn binds only after the app's lifespan has opened the
    database, run housekeeping and the upgrade steps — so a port held by
    another program used to cost a full boot before "[Errno 48] address
    already in use". Returns ``(exit_code, message)``: 75 when a Topos node
    already answers (a supervisor may wait for it), 4 when the port belongs to
    something else, None when the way is clear.
    """
    probe_host = "127.0.0.1" if host in (None, "", "0.0.0.0", "::") else host
    if _port_accepts_connections(probe_host, port):
        running = _probe_running_node(probe_host, port)
        if running is not None:
            pid = running.get("pid")
            suffix = f" (pid {pid})" if pid else ""
            return 75, f"Topos is already running on port {port}{suffix}"
        return 4, f"port {port} is in use by another program"
    from topos.uds import socket_is_live, socket_path

    sock = socket_path()
    if sock.exists() and socket_is_live(sock):
        return 75, f"Topos is already running (socket {sock})"
    return None


def _node_argv() -> list[str]:
    """The absolute command that starts this node, for a detached child."""
    from topos.cli.binary import resolve_node_binary

    found = resolve_node_binary()
    if found is not None:
        return [str(found)]
    return [sys.executable, "-m", "topos.cli"]


def _spawn_detached(argv: list[str], env: dict[str, str], log_path: Path) -> int:
    """Start ``argv`` in its own session with stdout+stderr appended to ``log_path``.

    The log folder is 0700 and the file 0600 from the first byte: the node's
    log names sources, counts and errors, and the hardening step clamps the
    same paths later — this just never leaves a window.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(log_path.parent, 0o700)
    except OSError:
        pass
    with open(log_path, "ab") as log:
        try:
            os.chmod(log_path, 0o600)
        except OSError:
            pass
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=str(Path.home()),
            start_new_session=True,
            close_fds=True,
        )
    return proc.pid


@click.command("start")
@click.option(
    "--headless",
    is_flag=True,
    help="Start in the background: no tray, no update prompt, logs to ~/.topos/logs/node.log.",
)
@click.option(
    "--headless-foreground",
    is_flag=True,
    hidden=True,
    help="The service variant: same flags, but stays in the foreground for launchd/systemd.",
)
@click.option("--port", default=DEFAULT_NODE_PORT, show_default=True, help="Server port.")
@click.option("--host", default=None, help="Host to bind to (default: 127.0.0.1).")
@click.option("--db-path", default=None, help="Database file path (SQLite).")
@click.pass_context
def start_command(ctx, headless: bool, headless_foreground: bool, port: int, host: str | None, db_path: str | None) -> None:
    """Start Topos on this computer.

    Exit codes: 0 started · 4 the port is held by another program · 75 Topos
    is already running (a supervisor can tell the two apart).
    """
    from topos.core.logging import default_node_log_path

    if not headless and not headless_foreground:
        _run_node(db_path=db_path, topos_key=None, port=port, host=host,
                  skip_update_check=False, tray=None, app_mode=False)
        return

    log_path = default_node_log_path()

    if headless_foreground:
        while True:
            verdict = _single_node_preflight(host, port)
            if verdict is None:
                break
            code, message = verdict
            if code == 75:
                click.echo(f"{message} — waiting for it to stop.", err=True)
                _sleep(HEADLESS_FOREGROUND_WAIT_SECONDS)
                continue
            click.echo(f"Error: {message}", err=True)
            ctx.exit(code)
        os.environ["TOPOS_LOG_FILE"] = str(log_path)
        os.environ["TOPOS_SKIP_UPDATE_CHECK"] = "1"
        # The unit names launchd/systemd; a hand-run foreground start is "service".
        os.environ.setdefault("TOPOS_SUPERVISOR", "service")
        _run_node(db_path=db_path, topos_key=None, port=port, host=host,
                  skip_update_check=True, tray=False, app_mode=False)
        return

    verdict = _single_node_preflight(host, port)
    if verdict is not None:
        code, message = verdict
        click.echo(f"Error: {message}", err=True)
        ctx.exit(code)

    # The child reads the same file; its env must not carry a differing shell key.
    warning = _apply_env_file_key_policy(_user_env_path())
    if warning:
        click.echo(warning, err=True)
    env = dict(os.environ)
    env["TOPOS_LOG_FILE"] = str(log_path)
    env["TOPOS_SUPERVISOR"] = "headless"
    argv = _node_argv() + ["--no-tray", "--skip-update-check", "--port", str(port)]
    if host:
        argv += ["--host", host]
    if db_path:
        argv += ["--db-path", db_path]
    pid = _spawn_detached(argv, env, log_path)
    probe_host = "127.0.0.1" if host in (None, "", "0.0.0.0", "::") else host
    outcome = _wait_for_node(probe_host, port, pid, HEADLESS_START_TIMEOUT_SECONDS)
    if outcome == "dead":
        click.echo(f"Error: Topos stopped right after starting (pid {pid}). See the log: {log_path}", err=True)
        ctx.exit(4)
    if outcome == "silent":
        click.echo(
            f"Error: Topos did not answer on port {port} within {int(HEADLESS_START_TIMEOUT_SECONDS)} s (pid {pid}). "
            f"See the log: {log_path}",
            err=True,
        )
        ctx.exit(4)
    click.echo(f"Topos is running (pid {pid}).")
    click.echo(f"Logs: {log_path}")
    click.echo("Check it with `topos-node status`.")


from topos.cli.reprocess_cmd import reprocess_command  # noqa: E402
from topos.cli.profile_cmd import profile_group  # noqa: E402
from topos.cli.setup_models_cmd import setup_models_command  # noqa: E402
from topos.cli.agents_cmd import agents_group  # noqa: E402
from topos.cli.sources_cmd import sources_group  # noqa: E402
from topos.cli.status_cmd import doctor_command, status_command  # noqa: E402
from topos.mcp_server import mcp_command  # noqa: E402
from topos.cli.pair_cmd import pair_command  # noqa: E402
from topos.cli.service_cmd import service_group  # noqa: E402

main.add_command(reprocess_command)
main.add_command(profile_group)
main.add_command(setup_models_command)
main.add_command(agents_group)
main.add_command(sources_group)
main.add_command(status_command)
main.add_command(doctor_command)
main.add_command(mcp_command)
main.add_command(start_command)
main.add_command(pair_command)
main.add_command(service_group)


if __name__ == "__main__":
    main()
