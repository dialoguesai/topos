"""`topos-node truth weekly [--weeks N] [--json]` — the inbound truth lane's weekly numbers, for the owner.

The same aggregates ``truth_weekly_stats`` answers (``topos/query/truth_checks.py``): counts per ISO week, never a
claim, a name or a record id. It reads the active database directly through a read-only connection, so it works
with the node stopped, and it can neither migrate nor write (``mode=ro``). A database with no lane tables prints
zeros.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict

import click


def _read(weeks: int) -> Dict[str, Any]:
    from topos.config.truth_inbound import load
    from topos.query.truth_checks import weekly_stats
    from topos.storage.db.paths import resolve_active_database

    path = resolve_active_database().path
    if not path.is_file():
        raise click.ClickException(f"No Topos database at {path}.")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return {"settings": load(conn), **weekly_stats(conn, weeks)}
    finally:
        conn.close()


def _line(label: str, counts: Dict[str, int]) -> str:
    shown = ", ".join(f"{key} {value}" for key, value in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])) if value)
    return f"  {label}: {shown or 'none'}"


def render(result: Dict[str, Any]) -> str:
    settings = result["settings"]
    state = "on" if settings["enabled"] else "off"
    lines = [f"Truth lane: {state} ({settings['model']} model, up to {settings['rate_per_minute']} checks a minute; "
             f"sources: {', '.join(settings['sources']) or 'none'})"]
    for week in result["weeks"]:
        p50 = week["latency_ms_p50"]
        lines.append(f"{week['week']}: {week['checked']} checked, {week['backed']} backed, "
                     f"{week['nothing_found']} nothing found, {week['unable']} unable, {week['not_a_claim']} not a claim; "
                     f"{week['labelled']} labelled, {week['to_confirm']} to confirm; "
                     f"median check {'-' if p50 is None else f'{p50 / 1000:.1f} s'}")
        if week["checked"]:
            lines.append(_line("by source", week["by_source"]))
            lines.append(_line("by category", week["by_category"]))
            agreement = week["agreement"]
            lines.append(f"  your labels: backed and true {agreement['backed_true']}, backed but false "
                         f"{agreement['backed_false']}, nothing found but true {agreement['nothing_true']}")
    return "\n".join(lines)


@click.group("truth")
def truth_group() -> None:
    """The inbound truth lane (verify_claim on new messages, transcripts and journal entries)."""


@truth_group.command("weekly")
@click.option("--weeks", type=click.IntRange(1, 8), default=1, show_default=True, help="How many ISO weeks, newest first.")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def weekly_command(weeks: int, as_json: bool) -> None:
    """Counts per ISO week: checked, backed, nothing found, unable, not a claim, labelled. No claim text."""
    result = _read(weeks)
    click.echo(json.dumps(result, indent=1, sort_keys=True) if as_json else render(result))
