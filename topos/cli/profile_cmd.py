"""CLI: manage multiple Topos profiles on one machine.

The shell apps (macOS/Windows trays) shell out to these commands for every
mutation so the swap logic lives in exactly one place; they read the profile
folders directly for menu rendering. ``--json`` exists for those callers.
All commands run offline — no server import. The only database access is
``switch``'s preflight, which reads ``PRAGMA user_version`` from the archived
target read-only to refuse a Topos this build is too old to open.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import click

from topos import profiles
from topos.defaults import DEFAULT_NODE_PORT


def _echo(payload: dict, as_json: bool, human: str) -> None:
    click.echo(json.dumps(payload, indent=2, sort_keys=True) if as_json else human)


def _base_from(base: Optional[str]) -> Optional[Path]:
    return Path(base) if base else None


def _human_size(size_bytes: int) -> str:
    """Whole MB below a tenth of a GB, so a small Topos does not read '0.0 GB'."""
    size_gb = size_bytes / 1_000_000_000
    return f"{size_gb:.1f} GB" if size_gb >= 0.1 else f"{size_bytes // 1_000_000} MB"


_BASE_OPTION = click.option(
    "--base",
    default=None,
    hidden=True,
    help="Override ~/.topos (tests and recovery only).",
)
_JSON_OPTION = click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
_PORT_OPTION = click.option(
    "--port", default=DEFAULT_NODE_PORT, show_default=True, help="Port probed for a running node."
)


@click.group("profile")
def profile_group() -> None:
    """Manage multiple Topoi on this machine (list, new, switch, remove)."""


@profile_group.command("list")
@_JSON_OPTION
@_BASE_OPTION
def profile_list(as_json: bool, base: Optional[str]) -> None:
    """All profiles: the active one first, then archived ones."""
    infos = profiles.list_profiles(_base_from(base))
    payload = {"profiles": [i.as_dict() for i in infos]}
    lines = []
    for info in infos:
        marker = "* " if info.active else "  "
        lines.append(
            f"{marker}{info.profile_id}  {info.name or '(unnamed)'}  "
            f"{_human_size(info.size_bytes)}"
        )
    _echo(payload, as_json, "\n".join(lines) or "(no profiles)")


@profile_group.command("current")
@_JSON_OPTION
@_BASE_OPTION
def profile_current(as_json: bool, base: Optional[str]) -> None:
    """The active profile, or nothing when the machine is fresh/unbound."""
    info = profiles.current_profile(_base_from(base))
    payload = {"profile": info.as_dict() if info else None}
    _echo(payload, as_json, info.profile_id if info else "(none)")


@profile_group.command("new")
@click.option("--name", default=None, help="Slug for the archived current profile.")
@_PORT_OPTION
@_JSON_OPTION
@_BASE_OPTION
def profile_new(name: Optional[str], port: int, as_json: bool, base: Optional[str]) -> None:
    """Archive the active Topos and leave this machine fresh for a new pairing.

    Data is moved (never deleted) into ~/.topos/profiles/<slug>/ and can be
    reactivated any time with `topos-node profile switch <slug>`.
    """
    try:
        result = profiles.new_profile(_base_from(base), name=name, port=port)
    except profiles.ProfileError as exc:
        raise click.ClickException(str(exc)) from exc
    human = (
        f"Archived active Topos as '{result['archived_as']}'. This machine is now unbound."
        if result["archived"]
        else "Nothing active to archive — machine already fresh."
    )
    _echo(result, as_json, human)


@profile_group.command("switch")
@click.argument("profile_id")
@_PORT_OPTION
@_JSON_OPTION
@_BASE_OPTION
def profile_switch(profile_id: str, port: int, as_json: bool, base: Optional[str]) -> None:
    """Activate an archived profile (archiving the current one first).

    The node must be stopped; whatever supervises it (the menu-bar app or you)
    restarts it afterwards so the new Topos is what serves.
    """
    try:
        result = profiles.switch_profile(profile_id, _base_from(base), port=port)
    except profiles.ProfileError as exc:
        raise click.ClickException(str(exc)) from exc
    human = f"Activated '{result['activated']}'"
    if result["archived_as"]:
        human += f" (previous Topos archived as '{result['archived_as']}')"
    _echo(result, as_json, human + ". Start the node to use it.")


@profile_group.command("remove")
@click.argument("profile_id")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt (for shells).")
@_JSON_OPTION
@_BASE_OPTION
def profile_remove(profile_id: str, yes: bool, as_json: bool, base: Optional[str]) -> None:
    """Permanently delete an archived Topos and its data from this machine.

    Unlike `new` and `switch`, which move data and can be undone by moving it
    back, this deletes it. The active Topos cannot be removed — switch to
    another one, or disconnect this Mac, first.

    The node does not need to be stopped: an archived Topos is not the one the
    running engine has open.
    """
    base_path = _base_from(base)
    if not yes:
        info = next(
            (
                p
                for p in profiles.list_profiles(base_path)
                if p.profile_id == profile_id and not p.active
            ),
            None,
        )
        label = (info.name or profile_id) if info else profile_id
        size = _human_size(info.size_bytes) if info else "unknown size"
        click.confirm(
            f"Permanently delete '{label}' and its data ({size}) from this machine?",
            abort=True,
        )
    try:
        result = profiles.remove_profile(profile_id, base_path)
    except profiles.ProfileError as exc:
        raise click.ClickException(str(exc)) from exc
    _echo(
        result,
        as_json,
        f"Deleted '{result['name'] or result['removed']}' and freed "
        f"{_human_size(result['freed_bytes'])}.",
    )


@profile_group.command("adopt")
@_JSON_OPTION
@_BASE_OPTION
def profile_adopt(as_json: bool, base: Optional[str]) -> None:
    """Copy a legacy (pre-profile) database into the active slot, if empty."""
    result = profiles.adopt_legacy(_base_from(base))
    human = (
        f"Adopted legacy database from {result['adopted']} (original left in place)."
        if result["adopted"]
        else f"Nothing adopted: {result['reason']}."
    )
    _echo(result, as_json, human)
