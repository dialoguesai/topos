"""Pre-migration SQLite backup (PLAN_NODE_RELEASE_MIGRATIONS §4a.5)."""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

logger = logging.getLogger("topos.storage.db.migrations.backup")

#: How many pre-migration backups to keep PER TOPOS.
#:
#: Three rather than two, because RELEASING.md makes these the whole recovery
#: story — "Rollback is not package downgrade. Recovery = restore
#: ~/.topos/backups/database-pre-v{X}-*.db" — and two rungs means a bad upgrade
#: landing on top of an earlier bad upgrade leaves nowhere to stand. The third
#: costs roughly one database (~500 MB here), which is cheap against the only
#: copy of data that cannot be re-derived.
BACKUP_RETENTION = 3
_BACKUP_DIR_ENV = "TOPOS_BACKUP_DIR"


class InsufficientDiskForBackup(RuntimeError):
    """Raised when free disk is below 2× DB size and backup cannot proceed safely."""


def _safe_token(value: str) -> str:
    """Filename-safe form of a version or profile id."""
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


def backup_dir_for(db_path: Path) -> Path:
    override = os.environ.get(_BACKUP_DIR_ENV)
    if override:
        return Path(override)
    return db_path.expanduser().resolve().parent / "backups"


def connection_db_path(conn: sqlite3.Connection) -> Optional[Path]:
    """Return the on-disk path for the main DB, or None for :memory:/temp."""
    try:
        rows = conn.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error:
        return None
    for row in rows:
        # row: (seq, name, file)
        name = row[1] if not hasattr(row, "keys") else row["name"]
        file_path = row[2] if not hasattr(row, "keys") else row["file"]
        if str(name) != "main":
            continue
        if not file_path:
            return None
        path = Path(str(file_path))
        if not path.is_file():
            return None
        return path
    return None


def _free_bytes(path: Path) -> Optional[int]:
    try:
        usage = shutil.disk_usage(path if path.is_dir() else path.parent)
        return int(usage.free)
    except OSError:
        return None


#: ``database-pre-v<version>[--<profile>]-<UTC stamp>.db``. Parsed rather than
#: globbed: a glob for ``--q4-*`` also matches ``--q4-2-…``, so pruning one
#: Topos's backups would delete a differently-named Topos's — the same
#: cross-Topos bug one level down.
_BACKUP_NAME_RE = re.compile(
    r"^database-pre-v(?P<version>.+?)(?:--(?P<profile>.+))?-(?P<stamp>\d{8}T\d{6}Z)\.db$"
)


def backup_owner(name: str) -> Optional[str]:
    """The profile a backup filename belongs to, or None when it names none."""
    match = _BACKUP_NAME_RE.match(name)
    if match is None:
        return None
    return match.group("profile")


#: Distinguishes "caller said nothing" from "caller said: no version guard".
#: ``None`` is a real answer here — it turns the guard off — so it cannot also
#: be the default.
_UNSET: Any = object()


def installed_version() -> Optional[str]:
    """The version this node is running, or None when it cannot be read."""
    try:
        from ....__version__ import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001
        return None


def _parsed(value: Optional[str]) -> Optional[Any]:
    """A comparable version, or None for anything that is not one."""
    if not value:
        return None
    try:
        from packaging.version import InvalidVersion, Version
    except Exception:  # noqa: BLE001 — packaging is a dependency, not a promise
        return None
    try:
        return Version(str(value))
    except InvalidVersion:
        return None


def backup_version(name: str) -> Optional[str]:
    """The version tag a backup filename carries, or None when it carries none."""
    match = _BACKUP_NAME_RE.match(name)
    if match is None:
        return None
    return match.group("version")


def _superseded_by_version(name: str, installed: Optional[str]) -> bool:
    """Whether retention is allowed to count this backup out.

    ``database-pre-vX`` is the database as it stood BEFORE X's migrations ran,
    which makes it the only way back to pre-X schema. While the node is still on
    X — or has been downgraded to or below it — that file is the live rollback
    target RELEASING.md names, and a count is the wrong reason to delete it.
    Only a backup tagged strictly below the running version has been superseded.

    Unknown on either side means "superseded", deliberately. The guard can only
    ever keep MORE files than the count alone, so a guard that also fired on
    "could not tell" would turn one unparseable version into a directory that
    never prunes again — the failure this whole policy exists to prevent.
    """
    here = _parsed(installed)
    if here is None:
        return True
    theirs = _parsed(backup_version(name))
    if theirs is None:
        return True
    return theirs < here


def _newest_first(path: Path) -> Any:
    """Sort key: most recently written first, name as the tiebreak.

    Two backups can land in the same mtime tick on a coarse filesystem, and an
    unstable order there would make retention pick a different victim each run.
    """
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (-mtime, path.name)


def _condemned_in_group(
    candidates: List[Path], *, keep: int, installed: Optional[str]
) -> List[Path]:
    """Which of one Topos's backups retention drops, newest-first input.

    Two rules, and a backup survives if either one holds it:

      * **Count.** The newest ``keep`` stay, which is the ladder RELEASING.md
        tells an owner to climb down.
      * **Version.** The newest backup for each version at or above the running
        one stays regardless of count, because it is the only route back to
        that version's pre-migration schema and a downgraded node has no other.

    The version rule is deliberately per VERSION, not per file. Protecting every
    file tagged ``vX`` would be unbounded: a migration that fails on each boot
    re-backs-up under the same tag every time, and the guard would defend the
    whole pile — filling the disk fastest exactly when a broken upgrade has the
    node in a boot loop. One rollback point per version is all a rollback needs.
    """
    protected_versions: set = set()
    condemned: List[Path] = []
    for index, path in enumerate(candidates):
        if not _superseded_by_version(path.name, installed):
            tag = str(backup_version(path.name))
            if tag not in protected_versions:
                protected_versions.add(tag)
                continue
        if index < max(0, keep):
            continue
        condemned.append(path)
    return condemned


def prune_old_backups(
    directory: Path,
    *,
    keep: int = BACKUP_RETENTION,
    profile_id: Optional[str] = None,
    installed: Any = _UNSET,
) -> List[Path]:
    """Keep the newest ``keep`` backups BELONGING TO ONE TOPOS; delete older ones.

    Every Topos takes its turn in the same active slot, so all of them write
    into the same ``~/.topos/backups``. Pruning "the newest 2" across the whole
    directory therefore meant that switching to a second Topos and letting it
    migrate DELETED the first one's pre-upgrade backups — the safety net for a
    Topos that was not even running, with nothing said about it.

    Backups written before the name carried a profile (and by machines that
    have never switched) form their own group: a namespaced backup never prunes
    them, because nothing can prove which Topos they came from.

    Count is not the only thing that keeps a backup — see
    ``_superseded_by_version``. Pass ``installed=None`` to drop that guard and
    prune on recency alone.
    """
    if not directory.is_dir():
        return []
    resolved = installed_version() if installed is _UNSET else installed
    wanted = _safe_token(profile_id) if profile_id else None
    candidates = [
        path
        for path in directory.glob("database-pre-v*.db")
        if _BACKUP_NAME_RE.match(path.name) and backup_owner(path.name) == wanted
    ]
    candidates.sort(key=_newest_first)
    removed: List[Path] = []
    for stale in _condemned_in_group(candidates, keep=keep, installed=resolved):
        try:
            stale.unlink()
            removed.append(stale)
            logger.info("Pruned old backup %s", stale.name)
        except OSError as exc:
            logger.warning("Failed to prune backup %s: %s", stale, exc)
    return removed


def backup_database_before_migrations(
    conn: sqlite3.Connection,
    *,
    shipped_version: str,
    keep: int = BACKUP_RETENTION,
) -> Optional[Path]:
    """Copy the live DB to ``~/.topos/backups/database-pre-v{ver}-{ts}.db``.

    Returns the backup path, or None when there is no on-disk DB (in-memory /
    test fixtures). Raises ``InsufficientDiskForBackup`` when free space is
    below 2× the DB size (skip-with-loud-warning is the caller's choice — we
    raise so boot can decide fail-loud vs warn).
    """
    db_path = connection_db_path(conn)
    if db_path is None:
        return None

    dest_dir = backup_dir_for(db_path)
    dest_dir.mkdir(parents=True, exist_ok=True)

    size = db_path.stat().st_size
    free = _free_bytes(dest_dir)
    if free is not None and size > 0 and free < 2 * size:
        raise InsufficientDiskForBackup(
            f"free disk {free} bytes < 2× database size {size} bytes; "
            f"refusing to migrate without a pre-upgrade backup under {dest_dir}"
        )

    from ..paths import active_profile_id_for

    profile_id = active_profile_id_for(db_path)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_ver = _safe_token(shipped_version)
    owner = f"--{_safe_token(profile_id)}" if profile_id else ""
    dest = dest_dir / f"database-pre-v{safe_ver}{owner}-{stamp}.db"
    if dest.exists():
        dest.unlink()

    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    prune_old_backups(dest_dir, keep=keep, profile_id=profile_id)
    logger.info(
        "Pre-migration backup written to %s (%.1f MB, Topos %s)",
        dest,
        dest.stat().st_size / 1e6,
        profile_id or "unnamed",
    )
    return dest


# ---------------------------------------------------------------------------
# What the directory holds, for the disk floor to reason about
#
# `disk_space` decides whether there is room, and `model_manager` decides what
# to give up when there is not. Before this, neither could see the backup
# directory at all, so a node under its floor would delete an Ollama model —
# re-downloadable, and the only reproducible large thing on the volume — while
# gigabytes of superseded database copies sat beside it untouched. These
# functions are the missing half of that judgement: what is here, what policy
# has already condemned, and what only the owner may decide about.
# ---------------------------------------------------------------------------

#: Sidecars SQLite leaves beside a file. They belong to the snapshot that names
#: them and are counted with it, never on their own.
_SIDECAR_SUFFIXES = ("-wal", "-shm")


class BackupEntry(NamedTuple):
    """One governed backup — what it is, whose it is, and what it costs."""

    path: Path
    size_bytes: int
    profile: Optional[str]
    version: Optional[str]


def _size_of(path: Path) -> int:
    try:
        return int(path.stat().st_size)
    except OSError:
        return 0


def _entry(path: Path) -> BackupEntry:
    return BackupEntry(
        path=path,
        size_bytes=_size_of(path),
        profile=backup_owner(path.name),
        version=backup_version(path.name),
    )


def governed_backups(directory: Path) -> Dict[Optional[str], List[Path]]:
    """Every ``database-pre-v*`` backup, grouped by owning Topos, newest first."""
    if not directory.is_dir():
        return {}
    groups: Dict[Optional[str], List[Path]] = {}
    for path in directory.glob("database-pre-v*.db"):
        if _BACKUP_NAME_RE.match(path.name) is None:
            continue
        groups.setdefault(backup_owner(path.name), []).append(path)
    for paths in groups.values():
        paths.sort(key=_newest_first)
    return groups


def condemned_backups(
    directory: Path,
    *,
    keep: int = BACKUP_RETENTION,
    installed: Any = _UNSET,
) -> List[BackupEntry]:
    """Backups retention has already superseded, across every Topos on the disk.

    Grouped by owner and counted per group, which is what keeps this from being
    the cross-Topos bug ``prune_old_backups`` documents. That bug was one budget
    SHARED between owners: a second Topos migrating counted the first one's
    files and deleted its newest backups. Giving every owner its own budget of
    ``keep`` leaves each Topos a full ladder, and condemns only what that
    Topos's own retention would drop the next time it migrates. Nothing here is
    a new deletion — it is the same deletion, sooner.
    """
    resolved = installed_version() if installed is _UNSET else installed
    condemned: List[BackupEntry] = []
    for paths in governed_backups(directory).values():
        condemned.extend(
            _entry(path)
            for path in _condemned_in_group(paths, keep=keep, installed=resolved)
        )
    return condemned


def prune_to_retention(
    directory: Path,
    *,
    keep: int = BACKUP_RETENTION,
    installed: Any = _UNSET,
) -> Tuple[List[Path], int]:
    """Delete every condemned backup; return what went and the bytes it freed.

    The whole-disk counterpart to ``prune_old_backups``, which runs at migration
    time for the Topos doing the migrating. This one is for the disk floor,
    which has no profile of its own and needs the volume, not one slot, brought
    back under the line.
    """
    removed: List[Path] = []
    freed = 0
    for entry in condemned_backups(directory, keep=keep, installed=installed):
        try:
            entry.path.unlink()
        except OSError as exc:
            logger.warning("Failed to prune backup %s: %s", entry.path, exc)
            continue
        removed.append(entry.path)
        freed += entry.size_bytes
        logger.info(
            "Pruned superseded backup %s (%.1f MB, Topos %s)",
            entry.path.name,
            entry.size_bytes / 1e6,
            entry.profile or "unnamed",
        )
    return removed, freed


def untracked_snapshots(directory: Path) -> List[Path]:
    """Loose database files in the backup directory that no policy governs.

    Hand-made snapshots — ``pre-unify-…``, ``database-pre-case-backfill-…`` —
    written by one-off scripts before a risky backfill. They carry no version
    tag, so there is nothing here to reason with: no ladder to keep three rungs
    of, no "still rolls back the running version" to check. They are reported
    and never deleted. The node did not write them, cannot know what state they
    were taken to protect, and destroying an owner's manual safety net to make
    room for a model it could re-download is the exact trade this module exists
    to refuse.

    Non-recursive on purpose: ``orphaned-legacy-*/`` holds an adopted database,
    which is data rather than a snapshot of it.
    """
    if not directory.is_dir():
        return []
    found: List[Path] = []
    for path in sorted(directory.glob("*.db")):
        if not path.is_file() or _BACKUP_NAME_RE.match(path.name):
            continue
        found.append(path)
        for suffix in _SIDECAR_SUFFIXES:
            sidecar = path.with_name(path.name + suffix)
            if sidecar.is_file():
                found.append(sidecar)
    return found


def retention_report(
    directory: Path,
    *,
    keep: int = BACKUP_RETENTION,
    installed: Any = _UNSET,
) -> Dict[str, Any]:
    """What the backup directory costs, split by who is allowed to reclaim it.

    Three numbers because they answer three different questions, and collapsing
    them into one "reclaimable" figure would license the wrong deletion:

      * ``prunable_bytes`` — the node's to take, already condemned by retention.
      * ``retained_bytes`` — the rollback ladder. Not reclaimable at any price;
        it is the recovery path RELEASING.md sends owners to.
      * ``manual_bytes`` — untagged snapshots. Real space, real candidates, but
        only the owner can say so.
    """
    condemned = condemned_backups(directory, keep=keep, installed=installed)
    prunable = {entry.path for entry in condemned}
    retained = [
        path
        for paths in governed_backups(directory).values()
        for path in paths
        if path not in prunable
    ]
    manual = untracked_snapshots(directory)
    return {
        "path": str(directory),
        "keep": int(keep),
        "prunable_bytes": sum(entry.size_bytes for entry in condemned),
        "prunable_count": len(condemned),
        "retained_bytes": sum(_size_of(path) for path in retained),
        "retained_count": len(retained),
        "manual_bytes": sum(_size_of(path) for path in manual),
        "manual_count": sum(1 for path in manual if path.suffix == ".db"),
    }
