"""CLI helper: reprocess raw retention into canonical tables."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import click

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


@click.command("reprocess")
@click.argument("source_id")
@click.option(
    "--dataset-id",
    required=True,
    help="Dataset id (e.g. user_id:topos:topos_…).",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Only the newest N raw rows (by created_at).",
)
@click.option(
    "--from-stage",
    type=click.Choice(["raw", "canonical"]),
    default="raw",
    show_default=True,
)
@click.option(
    "--no-enrichment",
    is_flag=True,
    help="Skip signal/enrichment after canonical remap (faster recovery).",
)
@click.option(
    "--db-path",
    help="SQLite database path (default: auto-discovery).",
)
def reprocess_command(
    source_id: str,
    dataset_id: str,
    limit: int | None,
    from_stage: str,
    no_enrichment: bool,
    db_path: str | None,
) -> None:
    """Re-run raw→canonical for a source (idempotent recovery utility).

    Examples:

      topos-node reprocess grow_journal \\
        --dataset-id "$DATASET_ID" --limit 100 --no-enrichment
    """
    _load_env_file(USER_ENV_PATH)
    _load_env_file(LEGACY_ENV_PATH)
    if db_path:
        os.environ["TOPOS_DATABASE_PATH"] = db_path

    from topos.ingestion.reprocess import reprocess_source
    from topos.sources.install_service import rehydrate_active_installs_runtime

    rehydrate_active_installs_runtime(source_id=str(source_id).strip() or None)
    result = asyncio.run(
        reprocess_source(
            source_id=source_id,
            dataset_id=dataset_id,
            from_stage=from_stage,  # type: ignore[arg-type]
            limit=limit,
            run_enrichment=not no_enrichment,
        )
    )
    click.echo(json.dumps(result, indent=2, sort_keys=True))
