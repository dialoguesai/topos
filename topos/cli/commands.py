"""CLI entry points for Topos."""

from __future__ import annotations

import os
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

    raise click.ClickException(
        "TOPOS_KEY is not configured. Run `topos-node --set-topos-key <YOUR_TOPOS_KEY>` "
        "or provide `--topos-key` for this run."
    )


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


@click.group(invoke_without_command=True)
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

    if set_topos_key:
        env_path = _save_topos_key(set_topos_key)
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

    if skip_update_check:
        os.environ["TOPOS_SKIP_UPDATE_CHECK"] = "1"

    if _maybe_offer_self_update(skip_update_check=skip_update_check):
        return

    _load_env_file(USER_ENV_PATH)
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
        uvicorn.run(app, host=host, port=port, log_config=get_uvicorn_log_config())


from topos.cli.reprocess_cmd import reprocess_command  # noqa: E402
from topos.cli.profile_cmd import profile_group  # noqa: E402
from topos.cli.setup_models_cmd import setup_models_command  # noqa: E402

main.add_command(reprocess_command)
main.add_command(profile_group)
main.add_command(setup_models_command)


if __name__ == "__main__":
    main()
