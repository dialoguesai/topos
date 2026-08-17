"""Pre-migration SQLite backup (PLAN_NODE_RELEASE_MIGRATIONS §4a.5)."""

from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("topos.storage.db.migrations.backup")

BACKUP_RETENTION = 2
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


def prune_old_backups(
    directory: Path,
    *,
    keep: int = BACKUP_RETENTION,
    profile_id: Optional[str] = None,
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
    """
    if not directory.is_dir():
        return []
    wanted = _safe_token(profile_id) if profile_id else None
    candidates = [
        path
        for path in directory.glob("database-pre-v*.db")
        if _BACKUP_NAME_RE.match(path.name) and backup_owner(path.name) == wanted
    ]
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    removed: List[Path] = []
    for stale in candidates[keep:]:
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
