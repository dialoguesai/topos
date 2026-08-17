"""Multiple Topoi on one machine: profile archive/switch for ~/.topos.

A machine's bound Topos is two things in one folder: the ``TOPOS_KEY`` line in
``~/.topos/.env`` (identity) and the SQLite database plus ingestion state next
to it (data). Everything expensive — the engine runtime and the model cache —
is shared and profile-independent, so "switch the active Topos" is a
stop-node → move-files → start-node operation measured in seconds.

The ACTIVE profile stays exactly where every existing consumer already looks:
the top level of ``~/.topos``. Inactive profiles live under
``~/.topos/profiles/<slug>/`` with the same file layout plus a ``profile.json``
describing them. Switching therefore changes no path resolution anywhere in
the engine — only which files sit at the top level.

Only an explicit allowlist of files moves. ``~/.topos`` accumulates backups,
repair journals and scratch databases over a node's life; dragging those along
(or worse, missing a WAL sidecar) is exactly why the by-hand version of this
folder dance kept going wrong. Logs deliberately stay machine-global: they
interleave engine versions and belong to the machine, not the Topos.

Every move is a same-filesystem rename recorded in a journal file before it
happens, so a crash mid-switch is recoverable: the next profile operation
rolls the completed renames back and the machine wakes up on the profile it
started with.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .storage.db.paths import (  # one spelling of the layout, shared with the resolver
    ACTIVE_MARKER_FILENAME,
    DATABASE_FILENAME,
    PROFILES_DIRNAME,
    active_base,
)

PROFILE_META_FILENAME = "profile.json"
JOURNAL_FILENAME = ".profile-switch.json"
REBUILD_LOCK_FILENAME = f"{DATABASE_FILENAME}.rebuild.lock"

# What constitutes "the Topos" and moves on archive/switch. Directories move
# as a unit. Anything not listed stays at the top level of ~/.topos — user
# backups, old logs, scratch files — and is documented as machine-level.
MOVE_ALLOWLIST: tuple[str, ...] = (
    ".env",
    "database.db",
    "database.db-wal",
    "database.db-shm",
    "ingestion",
    "nightly",
    "config.yaml",
)

# The presence of any of these at the top level means there is an active
# profile worth archiving. ``config.yaml`` alone does not — a fresh install
# can carry one before it is ever paired or ingests a byte.
ACTIVE_SIGNIFIERS: tuple[str, ...] = (".env", "database.db")

# What may be deleted from an archived profile alongside the Topos itself.
# ``.DS_Store`` is there because Finder writes one into any folder a user
# opens, and refusing to delete a Topos over a Finder artefact is refusing
# over nothing.
REMOVABLE_EXTRAS: tuple[str, ...] = (PROFILE_META_FILENAME, ".DS_Store")


class ProfileError(Exception):
    """A profile operation that must not proceed. Message is user-facing."""


@dataclass
class ProfileInfo:
    profile_id: str
    name: Optional[str]
    path: str
    size_bytes: int
    active: bool = False

    def as_dict(self) -> dict:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "active": self.active,
        }


@dataclass
class _Journal:
    """Completed renames, oldest first. Recovery undoes them in reverse."""

    op: str
    moves: list[list[str]] = field(default_factory=list)


def default_base() -> Path:
    return active_base()


def _profiles_dir(base: Path) -> Path:
    return base / PROFILES_DIRNAME


def _journal_path(base: Path) -> Path:
    return base / JOURNAL_FILENAME


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def _active_size_bytes(base: Path) -> int:
    total = 0
    for name in MOVE_ALLOWLIST:
        item = base / name
        try:
            if item.is_file():
                total += item.stat().st_size
            elif item.is_dir():
                total += _dir_size_bytes(item)
        except OSError:
            continue
    return total


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-").lower()
    return slug or "topos"


def _unique_slug(base: Path, wanted: str) -> str:
    profiles = _profiles_dir(base)
    slug = wanted
    counter = 2
    while (profiles / slug).exists():
        slug = f"{wanted}-{counter}"
        counter += 1
    return slug


def node_is_running(port: int = 9000) -> bool:
    """True when something answers the node healthcheck on localhost.

    401/403 count as running — health auth being enabled means the node
    answered. Only used as a refusal guard: swapping the database out from
    under a live node corrupts the WAL story, so the caller must stop it first.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthcheck", timeout=1.5) as res:
            return res.status in (200, 401, 403)
    except urllib.error.HTTPError as exc:
        return exc.code in (200, 401, 403)
    except Exception:
        return False


def rebuild_in_progress(base: Path) -> bool:
    """Whether a graph rebuild currently HOLDS the lock beside the database.

    Presence of the file means nothing. ``rebuild_subprocess`` takes an
    advisory ``flock`` on it and never deletes it — the OS drops the *lock*
    when the child exits, but the *file* stays behind forever. Treating the
    file as the signal therefore blocks profile switching on every machine
    that has ever rebuilt its graph, permanently; the machine this was found
    on had a week-old empty lock and a switch that could never succeed.

    So ask the OS: if the lock can be taken, nobody holds it. Taken
    non-blocking and released immediately — the point is the answer, not the
    lock. A file that cannot be opened at all is not treated as a rebuild;
    refusing on an unreadable file would be the same permanent block by
    another route.
    """
    lock_path = base / REBUILD_LOCK_FILENAME
    if not lock_path.exists():
        return False
    try:
        import fcntl
    except ImportError:
        # Non-POSIX. `_acquire_rebuild_lock` has no fcntl either and falls back
        # to a plain open, so nothing ever holds this file on Windows and its
        # existence says nothing at all. Reporting a rebuild here would block
        # switching permanently on the first machine that ever rebuilt — the
        # very bug this function exists to fix, reintroduced one platform over.
        # The node must be stopped for a switch regardless, which is the real
        # protection.
        return False
    try:
        with open(lock_path, "a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True  # somebody holds it: a real rebuild is running
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            return False
    except OSError:
        return False


def _assert_safe_to_mutate(base: Path, *, port: int, skip_node_check: bool = False) -> None:
    if not skip_node_check and node_is_running(port):
        raise ProfileError(
            f"A Topos node is running on port {port}. Stop it first "
            "(Quit Topos Node in the menu bar), then retry."
        )
    if rebuild_in_progress(base):
        raise ProfileError(
            "A database rebuild is running. Wait for it to finish, then retry."
        )


# -- journal ---------------------------------------------------------------


def _journal_start(base: Path, op: str) -> _Journal:
    journal = _Journal(op=op)
    _write_json(_journal_path(base), {"op": journal.op, "moves": journal.moves})
    return journal


def _journal_record(base: Path, journal: _Journal, src: Path, dst: Path) -> None:
    journal.moves.append([str(src), str(dst)])
    _write_json(_journal_path(base), {"op": journal.op, "moves": journal.moves})


def _journal_finish(base: Path) -> None:
    _journal_path(base).unlink(missing_ok=True)


def recover_interrupted_switch(base: Optional[Path] = None) -> bool:
    """Roll back a switch that died mid-move. Returns True if anything was undone.

    The journal lists completed renames oldest-first; undoing them in reverse
    restores the exact pre-switch layout. Called automatically at the top of
    every profile operation so a crashed switch never strands the machine in a
    half-moved state longer than the next attempt.
    """
    base = base or default_base()
    journal_file = _journal_path(base)
    payload = _read_json(journal_file)
    moves = payload.get("moves") or []
    if not journal_file.exists():
        return False
    undone = False
    for src_str, dst_str in reversed(moves):
        src, dst = Path(src_str), Path(dst_str)
        if dst.exists() and not src.exists():
            src.parent.mkdir(parents=True, exist_ok=True)
            dst.rename(src)
            undone = True
    journal_file.unlink(missing_ok=True)
    return undone


def _move_allowlisted(base: Path, journal: _Journal, src_dir: Path, dst_dir: Path) -> int:
    """Rename every allowlisted entry present in src_dir into dst_dir."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for name in MOVE_ALLOWLIST:
        src = src_dir / name
        if not src.exists():
            continue
        dst = dst_dir / name
        if dst.exists():
            raise ProfileError(f"Refusing to overwrite {dst} — resolve by hand.")
        try:
            src.rename(dst)
        except OSError as exc:
            raise ProfileError(
                f"Could not move {src} → {dst} ({exc}). Profiles must live on the "
                "same filesystem as ~/.topos."
            ) from exc
        _journal_record(base, journal, src, dst)
        moved += 1
    return moved


# -- queries ---------------------------------------------------------------


def has_active_profile(base: Optional[Path] = None) -> bool:
    base = base or default_base()
    return any((base / name).exists() for name in ACTIVE_SIGNIFIERS)


def current_profile(base: Optional[Path] = None) -> Optional[ProfileInfo]:
    base = base or default_base()
    recover_interrupted_switch(base)
    if not has_active_profile(base):
        return None
    marker = _read_json(base / ACTIVE_MARKER_FILENAME)
    return ProfileInfo(
        profile_id=str(marker.get("profile_id") or "default"),
        name=(str(marker["topos_name"]) if marker.get("topos_name") else None),
        path=str(base),
        size_bytes=_active_size_bytes(base),
        active=True,
    )


def list_profiles(base: Optional[Path] = None) -> list[ProfileInfo]:
    """Active profile first (when one exists), then archived ones by name."""
    base = base or default_base()
    recover_interrupted_switch(base)
    result: list[ProfileInfo] = []
    active = current_profile(base)
    if active is not None:
        result.append(active)
    profiles = _profiles_dir(base)
    if profiles.is_dir():
        for entry in sorted(profiles.iterdir(), key=lambda p: p.name):
            if not entry.is_dir():
                continue
            meta = _read_json(entry / PROFILE_META_FILENAME)
            result.append(
                ProfileInfo(
                    profile_id=str(meta.get("profile_id") or entry.name),
                    name=(str(meta["topos_name"]) if meta.get("topos_name") else None),
                    path=str(entry),
                    size_bytes=_dir_size_bytes(entry),
                )
            )
    return result


# -- mutations -------------------------------------------------------------


def _archive_slug_for_active(base: Path, name_hint: Optional[str]) -> str:
    marker = _read_json(base / ACTIVE_MARKER_FILENAME)
    wanted = name_hint or marker.get("topos_name") or marker.get("profile_id")
    if wanted:
        return _unique_slug(base, _slugify(str(wanted)))
    return _unique_slug(base, time.strftime("topos-%Y%m%d-%H%M%S"))


def _archive_active(base: Path, journal: _Journal, *, name_hint: Optional[str]) -> Optional[str]:
    """Move the active profile into profiles/<slug>/. Returns the slug, or
    None when there was nothing to archive."""
    if not has_active_profile(base):
        return None
    slug = _archive_slug_for_active(base, name_hint)
    dest = _profiles_dir(base) / slug
    _move_allowlisted(base, journal, base, dest)
    marker = _read_json(base / ACTIVE_MARKER_FILENAME)
    _write_json(
        dest / PROFILE_META_FILENAME,
        {
            "profile_id": slug,
            "topos_name": marker.get("topos_name") or name_hint,
            "created_at": marker.get("created_at") or _now_iso(),
            "last_active_at": _now_iso(),
        },
    )
    (base / ACTIVE_MARKER_FILENAME).unlink(missing_ok=True)
    return slug


def _assert_openable_by_this_build(target: Path) -> None:
    """Refuse to activate a Topos that a newer engine has already migrated.

    The engine's downgrade guard fires when the database is OPENED — by then
    the switch has moved the files and the failure reads as "the node won't
    start", with no obvious way back. Checking here turns it into a refused
    switch with an instruction the user can act on. Read-only: it opens the
    archived database with ``mode=ro`` and closes it again.

    Fails OPEN. A database this cannot read — corrupt, encrypted, mid-write,
    or not SQLite at all — is not evidence of a version problem, and blocking
    the switch on "cannot tell" would strand people on the profile they are on.
    """
    from .storage.db.paths import read_database_user_version

    target_db = target / DATABASE_FILENAME
    stamped = read_database_user_version(target_db)
    if stamped is None:
        return
    try:
        from .storage.db.migrations import max_migration_order

        known = max_migration_order()
    except Exception:  # noqa: BLE001 — never block a switch on an import
        return
    if stamped > known:
        raise ProfileError(
            "This Topos was last used by a newer version of Topos "
            f"(its database is at schema {stamped}, this version understands {known}). "
            "Update Topos, then switch again."
        )


def new_profile(
    base: Optional[Path] = None,
    *,
    name: Optional[str] = None,
    port: int = 9000,
    skip_node_check: bool = False,
) -> dict:
    """Archive the active Topos (if any) and leave ~/.topos fresh and unbound.

    The zero-click "New Topos" primitive: after this, pairing writes a new key
    into an empty .env — the AlreadyBound refusal cannot fire — and the node
    creates a fresh database on first start. A machine with nothing active is
    a no-op success, so the desktop flow can call this unconditionally.
    """
    base = base or default_base()
    recover_interrupted_switch(base)
    _assert_safe_to_mutate(base, port=port, skip_node_check=skip_node_check)
    journal = _journal_start(base, "new")
    try:
        slug = _archive_active(base, journal, name_hint=name)
    except ProfileError:
        recover_interrupted_switch(base)
        raise
    _journal_finish(base)
    return {"archived": slug is not None, "archived_as": slug}


def switch_profile(
    profile_id: str,
    base: Optional[Path] = None,
    *,
    port: int = 9000,
    skip_node_check: bool = False,
) -> dict:
    """Archive the active Topos and activate profiles/<profile_id>/.

    One journalled operation: if anything fails part-way (or the process
    dies), the recovery pass restores the pre-switch layout. The node must be
    stopped — the caller owns the stop/start lifecycle around this call.
    """
    base = base or default_base()
    recover_interrupted_switch(base)
    target = _profiles_dir(base) / profile_id
    if not target.is_dir():
        known = ", ".join(p.name for p in _profiles_dir(base).iterdir()) if _profiles_dir(base).is_dir() else ""
        raise ProfileError(f"No profile named '{profile_id}'. Known: {known or '(none)'}")
    _assert_safe_to_mutate(base, port=port, skip_node_check=skip_node_check)

    _assert_openable_by_this_build(target)

    target_meta = _read_json(target / PROFILE_META_FILENAME)
    journal = _journal_start(base, "switch")
    try:
        archived_as = _archive_active(base, journal, name_hint=None)
        _move_allowlisted(base, journal, target, base)
    except ProfileError:
        recover_interrupted_switch(base)
        raise
    # Meta files are recreated, not moved, so do them after the journal closes:
    # a rollback must not resurrect a half-written marker.
    _journal_finish(base)
    _write_json(
        base / ACTIVE_MARKER_FILENAME,
        {
            "profile_id": str(target_meta.get("profile_id") or profile_id),
            "topos_name": target_meta.get("topos_name"),
            "created_at": target_meta.get("created_at"),
            "activated_at": _now_iso(),
        },
    )
    (target / PROFILE_META_FILENAME).unlink(missing_ok=True)
    try:
        target.rmdir()  # only succeeds when the profile dir is now empty
    except OSError:
        pass  # non-allowlisted leftovers stay put, visibly, in the profile dir
    return {"activated": profile_id, "archived_as": archived_as}


def remove_profile(profile_id: str, base: Optional[Path] = None) -> dict:
    """Delete an archived Topos and its data from this machine, permanently.

    The one profile operation that does not move files, and therefore the only
    one that cannot be rolled back: there is no journal here because there is
    nothing left to roll back to. That asymmetry is the whole reason callers
    put a confirmation in front of it.

    Deliberately does NOT require the node to be stopped. Every other mutation
    moves the ACTIVE slot out from under a running engine; this one touches
    only ``profiles/<id>/``, which by construction nothing has open. Making
    someone quit Topos to delete a Topos they are not using would be ceremony,
    and the restart would cost them a graph rebuild.

    Refuses rather than guesses: a file inside the profile that is not part of
    a Topos is named back to the caller and nothing at all is deleted — the
    same principle as the move allowlist, for the stronger reason that this
    folder is somebody's only copy.
    """
    base = base or default_base()
    recover_interrupted_switch(base)

    # A profile id names one folder inside profiles/, never a path. Without
    # this, '../..' resolves to the active Topos — or to anything else on disk.
    if profile_id != Path(profile_id).name or profile_id in ("", ".", ".."):
        raise ProfileError(f"'{profile_id}' is not a profile name.")

    target = _profiles_dir(base) / profile_id
    if not target.is_dir():
        active = current_profile(base)
        if active is not None and active.profile_id == profile_id:
            raise ProfileError(
                f"'{profile_id}' is the Topos this machine is using now. Switch to "
                "another one first, or disconnect this Mac, and then remove it."
            )
        known = (
            ", ".join(p.name for p in _profiles_dir(base).iterdir())
            if _profiles_dir(base).is_dir()
            else ""
        )
        raise ProfileError(f"No profile named '{profile_id}'. Known: {known or '(none)'}")

    removable = set(MOVE_ALLOWLIST) | set(REMOVABLE_EXTRAS)
    unknown = sorted(entry.name for entry in target.iterdir() if entry.name not in removable)
    if unknown:
        raise ProfileError(
            f"'{profile_id}' also holds files that are not part of a Topos: "
            f"{', '.join(unknown)}. Nothing was deleted — move those out first "
            "if you meant to delete this Topos."
        )

    meta = _read_json(target / PROFILE_META_FILENAME)
    freed = _dir_size_bytes(target)
    for entry in sorted(target.iterdir()):
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    target.rmdir()
    return {
        "removed": profile_id,
        "name": (str(meta["topos_name"]) if meta.get("topos_name") else None),
        "freed_bytes": freed,
    }


def set_active_name(name: str, base: Optional[Path] = None) -> None:
    """Record the bound Topos's user-chosen name on the active marker.

    The name lives in the control plane, not the node; the shell learns it
    from /device_info and mirrors it here so menus can label profiles with no
    network round-trip. Never required for correctness — a missing name
    renders as the profile id."""
    base = base or default_base()
    marker = _read_json(base / ACTIVE_MARKER_FILENAME)
    marker["topos_name"] = name
    if not marker.get("profile_id"):
        marker["profile_id"] = _slugify(name)
    if not marker.get("created_at"):
        marker["created_at"] = _now_iso()
    _write_json(base / ACTIVE_MARKER_FILENAME, marker)


def adopt_legacy(base: Optional[Path] = None) -> dict:
    """Fold a pre-profile legacy database into the (empty) active slot.

    Old installs left databases at ~/.topos_engine or under Application
    Support; the engine used to resolve to them in place, which is how a new
    Topos ended up running against a database no profile owned. Startup now
    adopts automatically on a machine that has never had a profile; this stays
    as the manual escape hatch for the case startup deliberately refuses —
    a machine that DOES use profiles and wants an old database pulled in.

    Copies (never moves) the newest legacy database into place, only when the
    active slot has none — the legacy copy stays behind as its own backup.
    """
    from .storage.db.paths import adopt_into_slot, newest_legacy_database

    base = base or default_base()
    active_db = base / DATABASE_FILENAME
    if active_db.exists():
        return {"adopted": None, "reason": "active database already present"}
    newest = newest_legacy_database()
    if newest is None:
        return {"adopted": None, "reason": "no legacy database found"}
    if not adopt_into_slot(newest, active_db):
        return {"adopted": None, "reason": f"could not copy {newest}"}
    return {"adopted": str(newest), "reason": None}
