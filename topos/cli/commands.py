"""CLI entry points for Topos."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import click
import uvicorn

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from topos.storage.db.paths import discover_databases

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
def main(db_path, topos_key, set_topos_key, discover, port, host) -> None:
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

    _load_env_file(USER_ENV_PATH)
    _load_env_file(LEGACY_ENV_PATH)

    if topos_key:
        os.environ["TOPOS_KEY"] = topos_key
    elif not os.getenv("TOPOS_KEY"):
        os.environ["TOPOS_KEY"] = "dev-key"
        click.echo("TOPOS_KEY not set; using local dev key")

    from topos.app import app

    if db_path:
        os.environ["TOPOS_DATABASE_PATH"] = db_path
        click.echo(f"Database path: {db_path}")

    click.echo(f"Starting topos API on {host}:{port}")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
