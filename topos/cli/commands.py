"""CLI entry points for Topos."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import click
import uvicorn
from packaging.version import InvalidVersion, Version

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from topos.storage.db.paths import discover_databases
from topos.startup_banner import emit_startup_banner

USER_ENV_PATH = Path.home() / ".topos" / ".env"
LEGACY_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


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


def _can_prompt_for_input() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


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


def _get_installed_package_version(package_name: str) -> str | None:
    try:
        return package_version(package_name)
    except PackageNotFoundError:
        return None


def _get_module_version() -> str | None:
    try:
        from topos.__version__ import __version__
    except Exception:
        return None
    return __version__ or None


def _get_runtime_version(package_name: str = "topos-node") -> str:
    return _get_module_version() or _get_installed_package_version(package_name) or "unknown"


def _emit_startup_banner(host: str, port: int, package_name: str = "topos-node") -> None:
    runtime_version = _get_runtime_version(package_name=package_name)
    emit_startup_banner(
        click.echo,
        version=runtime_version,
        mode="cli",
        bind=f"{host}:{port}",
    )
    # Prevent duplicate banner when app startup runs in same process.
    os.environ["TOPOS_STARTUP_BANNER_EMITTED"] = "1"


def _get_latest_pypi_version(package_name: str, timeout_seconds: float = 2.0) -> str | None:
    url = f"https://pypi.org/pypi/{package_name}/json"
    try:
        with urlopen(url, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError):
        return None
    return str(payload.get("info", {}).get("version") or "").strip() or None


def _should_skip_update_check(skip_update_check: bool) -> bool:
    if skip_update_check:
        return True
    env_value = (os.getenv("TOPOS_SKIP_UPDATE_CHECK") or "").strip().lower()
    return env_value in {"1", "true", "yes", "on"}


def _maybe_offer_self_update(
    skip_update_check: bool,
    package_name: str = "topos-node",
) -> bool:
    if _should_skip_update_check(skip_update_check):
        return False

    installed = _get_installed_package_version(package_name)
    if not installed:
        return False

    latest = _get_latest_pypi_version(package_name)
    if not latest:
        return False

    try:
        if Version(latest) <= Version(installed):
            return False
    except InvalidVersion:
        return False

    click.echo(f"Update available for {package_name}: {installed} -> {latest}")
    if not _can_prompt_for_input():
        click.echo(f"Run `uv tool upgrade {package_name}` to update.")
        return False

    if not click.confirm("Install the latest version now?", default=True):
        return False

    click.echo(f"Updating {package_name}...")
    result = subprocess.run(["uv", "tool", "upgrade", package_name], check=False)
    if result.returncode != 0:
        click.echo("Update failed. Continuing with current version.")
        return False

    click.echo("Update installed. Please re-run `topos-node` to use the latest version.")
    return True


@click.command()
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
    default=9000,
    help="Server port (default: 9000)",
)
@click.option(
    "--host",
    default="0.0.0.0",
    help="Host to bind to (default: 0.0.0.0)",
)
@click.option(
    "--skip-update-check",
    is_flag=True,
    help="Skip checking PyPI for a newer topos-node version.",
)
def main(db_path, topos_key, set_topos_key, discover, port, host, skip_update_check) -> None:
    """Topos Control Plane API entry point."""
    if set_topos_key:
        env_path = _save_topos_key(set_topos_key)
        click.echo(f"Saved TOPOS_KEY to {env_path}")
        click.echo("You can now run: topos-node")
        return

    if discover:
        databases = discover_databases()
        if databases:
            click.echo("Discovered databases:")
            for db in databases:
                click.echo(f"  - {db}")
        else:
            click.echo("No existing databases found")
        return

    if _maybe_offer_self_update(skip_update_check=skip_update_check):
        return

    _load_env_file(USER_ENV_PATH)
    _load_env_file(LEGACY_ENV_PATH)

    _resolve_topos_key(topos_key)

    from topos.app import app

    if db_path:
        os.environ["TOPOS_DATABASE_PATH"] = db_path
        click.echo(f"Database path: {db_path}")

    _emit_startup_banner(host=host, port=port)
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
