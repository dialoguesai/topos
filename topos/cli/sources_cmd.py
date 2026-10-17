"""`topos-node sources preview|enable|pause|resume|sync SOURCE` — the §2.8 doors.

A thin client of lane L5's socket-only owner doors
(``POST /api/local/owner/sources/{id}/<verb>``). The plugin's hooks call
``sync --session PATH --quiet`` after every agent turn; a human calls
``preview`` and ``enable`` once. Exit codes: 0 ok, 3 not paired, 4 node
down (CONTRACTS §4); any other refusal is 1 with the door's code.

What is printed is a projection — counts, status words, the source id — never
the door's response body, which on a sync carries a ledger and may one day
carry more. The guard tests plant a secret in every body to hold that line.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Callable, Optional

import click

from topos.mcp_server import NODE_DOWN, NodeDown, NodeRefused, NodeSocket

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_NOT_PAIRED = 3
EXIT_NODE_DOWN = 4

_SOURCE_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_JSON_OPTION = click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
_QUIET_OPTION = click.option("--quiet", is_flag=True, help="Print nothing on success (hooks).")
_SKIP_OPTION = click.option("--skip", "skip", multiple=True, metavar="DIR", help="Project folder name to leave out (repeatable).")


class _SourceId(click.ParamType):
    name = "source"

    def convert(self, value, param, ctx):
        text = str(value or "").strip()
        if not _SOURCE_ID_RE.match(text):
            self.fail(f"{value!r} is not a source id (lowercase letters, digits, underscores)", param, ctx)
        return text


SOURCE = _SourceId()


def _ints(mapping: Any) -> dict[str, int]:
    if not isinstance(mapping, dict):
        return {}
    return {str(k): v for k, v in mapping.items() if isinstance(v, int) and not isinstance(v, bool)}


def _word(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    return text if re.match(r"^[a-z0-9_]{1,32}$", text) else None


def _door(
    source_id: str,
    action: str,
    body: dict,
    *,
    as_json: bool,
    quiet: bool,
    project: Callable[[dict], tuple[dict, str]],
) -> None:
    sock = NodeSocket()
    try:
        result = sock.request_sync("POST", f"/api/local/owner/sources/{source_id}/{action}", json_body=body)
    except NodeDown:
        click.echo(NODE_DOWN, err=True)
        sys.exit(EXIT_NODE_DOWN)
    except NodeRefused as exc:
        if exc.code == "not_paired":
            click.echo("This computer isn't paired with a Topos yet. Run `topos-node pair`.", err=True)
            sys.exit(EXIT_NOT_PAIRED)
        if quiet and exc.status == 409:
            # The plugin's hook after every turn: a paused or not-yet-enabled
            # source is the owner's choice, not an error worth a line anywhere.
            sys.exit(EXIT_OK)
        click.echo(f"Topos refused `{action}` for {source_id}: {exc.code}.", err=True)
        sys.exit(EXIT_REFUSED)
    payload, human = project(result)
    if as_json:
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    elif not quiet:
        click.echo(human)
    sys.exit(EXIT_OK)


def _human_bytes(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f} GB"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} MB"
    if n >= 1_000:
        return f"{n / 1_000:.1f} KB"
    return f"{n} B"


@click.group("sources")
def sources_group() -> None:
    """Preview, enable, pause, resume and sync a transcript source."""


@sources_group.command("preview")
@click.argument("source_id", type=SOURCE)
@_SKIP_OPTION
@_JSON_OPTION
@_QUIET_OPTION
def sources_preview(source_id: str, skip: tuple[str, ...], as_json: bool, quiet: bool) -> None:
    """Counts only: how many sessions, how much text, which folders, the date span."""

    def project(result: dict) -> tuple[dict, str]:
        sessions = int(result.get("sessions") or 0)
        size = int(result.get("bytes_text") or 0)
        folders = [str(f) for f in (result.get("folders") or []) if isinstance(f, str)]
        oldest = str(result["oldest"]) if result.get("oldest") else None
        newest = str(result["newest"]) if result.get("newest") else None
        payload = {"sessions": sessions, "bytes_text": size, "folders": folders, "oldest": oldest, "newest": newest}
        span = f", {oldest[:10]} to {newest[:10]}" if oldest and newest else ""
        human = (
            f"{source_id}: {sessions} sessions, {_human_bytes(size)} of text, "
            f"{len(folders)} folders ({', '.join(folders) or 'none'}){span}."
        )
        return payload, human

    _door(source_id, "preview", {"skip": list(skip)}, as_json=as_json, quiet=quiet, project=project)


@sources_group.command("enable")
@click.argument("source_id", type=SOURCE)
@_SKIP_OPTION
@click.option("--backfill", type=click.Choice(["all", "30d"]), default="30d", show_default=True, help="How far back to read.")
@_JSON_OPTION
@_QUIET_OPTION
def sources_enable(source_id: str, skip: tuple[str, ...], backfill: str, as_json: bool, quiet: bool) -> None:
    """Install the source, schedule hourly syncs and start the backfill."""

    def project(result: dict) -> tuple[dict, str]:
        payload = {"source_id": source_id, "status": "enabled", "backfill": backfill, "skipped": list(skip)}
        return payload, f"{source_id} enabled; backfill {backfill} started, syncing hourly."

    _door(source_id, "enable", {"skip": list(skip), "backfill": backfill}, as_json=as_json, quiet=quiet, project=project)


def _toggle(action: str, past: str, help_text: str):
    @sources_group.command(action, help=help_text)
    @click.argument("source_id", type=SOURCE)
    @_JSON_OPTION
    @_QUIET_OPTION
    def command(source_id: str, as_json: bool, quiet: bool) -> None:
        def project(result: dict) -> tuple[dict, str]:
            return {"source_id": source_id, "status": past}, f"{source_id} {past}."

        _door(source_id, action, {}, as_json=as_json, quiet=quiet, project=project)

    return command


sources_pause = _toggle("pause", "paused", "Stop the hourly sync and the hooks for this source (keeps what was read).")
sources_resume = _toggle("resume", "resumed", "Resume the hourly sync and the hooks for a paused source.")


@sources_group.command("sync")
@click.argument("source_id", type=SOURCE)
@click.option("--session", "session_path", metavar="PATH", default=None, help="Sync one transcript file only.")
@_JSON_OPTION
@_QUIET_OPTION
def sources_sync(source_id: str, session_path: Optional[str], as_json: bool, quiet: bool) -> None:
    """Sync now (refused unless the source is enabled)."""

    def project(result: dict) -> tuple[dict, str]:
        # The owner door queues durable work; an accepted job has not synced
        # anything yet. Do not project its missing counts as a completed sync.
        if result.get("job_id"):
            status = "already_running" if result.get("already_running") else "queued"
            return {"source_id": source_id, "status": status, "job_id": result["job_id"]}, (
                f"{source_id} sync {status.replace('_', ' ')} (job {result['job_id']})."
            )
        counts = _ints(result)
        ledger = result.get("ledger") if isinstance(result.get("ledger"), dict) else {}
        ledger_counts = _ints(ledger)
        dropped = _ints(ledger.get("dropped"))
        payload = {
            "source_id": source_id,
            "status": _word(result.get("status")) or "ok",
            "counts": counts,
            "ledger": {**ledger_counts, **({"dropped": dropped} if dropped else {})},
        }
        emitted = ledger_counts.get("turns_emitted")
        detail = f" {emitted} turns" if emitted is not None else ""
        return payload, f"{source_id} synced:{detail or ' done'}."

    body = {"session_path": session_path} if session_path else {}
    _door(source_id, "sync", body, as_json=as_json, quiet=quiet, project=project)
