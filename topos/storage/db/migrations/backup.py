"""Pre-migration SQLite backup (PLAN_NODE_RELEASE_MIGRATIONS §4a.5)."""

from __future__ import annotations

import logging
import os
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


def prune_old_backups(directory: Path, *, keep: int = BACKUP_RETENTION) -> List[Path]:
    """Keep the newest ``keep`` ``database-pre-v*.db`` files; delete older ones."""
    if not directory.is_dir():
        return []
    candidates = sorted(
        directory.glob("database-pre-v*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed: List[Path] = []
    for stale in candidates[keep:]:
        try:
            stale.unlink()
            removed.append(stale)
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

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_ver = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in shipped_version)
    dest = dest_dir / f"database-pre-v{safe_ver}-{stamp}.db"
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

    prune_old_backups(dest_dir, keep=keep)
    logger.info("Pre-migration backup written to %s (%.1f MB)", dest, dest.stat().st_size / 1e6)
    return dest
