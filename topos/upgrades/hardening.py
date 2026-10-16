"""Keep ``~/.topos`` private to this account (upgrade kind ``local_hardening``).

Security review finding 13 (AI-tool on-ramp, 2026-10-15): ``~/.topos`` was
0755 and ``database.db``, its ``-wal``/``-shm`` and the logs 0644, so every
other account on the computer could read the node's whole model of the owner
— while the AI-tool transcripts the node now copies in are 0700 at their
source. This step clamps the folder to 0700 and the data files to 0600, and
records whether the owner key persisted (DIGEST J6: an unwritable ``~/.topos``
leaves the node in legacy mode with every direct-HTTP door open).

Folders are clamped to 0700, never 0600: a directory without its execute bit
cannot be traversed, which would lock the node out of its own data.

Idempotent and resumable like every manifest step: chmod to a fixed mode, so a
second run reports ``changed == 0``. Symlinks are never followed — a link out
of ``~/.topos`` must not change the mode of something that is not ours. The
report names relative paths and counts only; never a file's content.

Also callable directly — the transcript-source enable door runs it before the
first sync (PLAN §2.2), and ``doctor`` reads :func:`owner_key_persisted`.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..owner_key import owner_key_persisted

logger = logging.getLogger("topos.upgrades.hardening")

DIR_MODE = 0o700
FILE_MODE = 0o600

#: Files at the top of ``~/.topos`` (and the DB trio inside each profile dir).
_DB_TRIO = ("database.db", "database.db-wal", "database.db-shm")
_TOP_FILES = _DB_TRIO + (".env", "pairing.json", "install-id", "active-profile.json")
#: Folders whose direct children are data files: clamp the folder and each file.
_FILE_FOLDERS = ("logs", "agents", "backups")

OWNER_KEY_FINDING = (
    "owner key not persisted: ~/.topos/.env has no TOPOS_OWNER_KEY line, so the node runs in legacy "
    "mode (every direct-HTTP door open). Make ~/.topos writable and restart Topos."
)


def _clamp(path: Path, mode: int, changed: List[str], same: List[str], skipped: List[str], base: Path) -> None:
    rel = str(path.relative_to(base)) if path != base else "."
    try:
        st = os.lstat(path)
    except OSError:
        return
    if stat.S_ISLNK(st.st_mode):
        return  # never follow a link out of the folder
    if stat.S_IMODE(st.st_mode) == mode:
        same.append(rel)
        return
    try:
        os.chmod(path, mode)
    except OSError as exc:
        skipped.append(f"{rel}: {type(exc).__name__}")
        return
    changed.append(rel)


def _clamp_file_folder(folder: Path, base: Path, acc: Tuple[List[str], List[str], List[str]]) -> None:
    changed, same, skipped = acc
    if not folder.is_dir() or folder.is_symlink():
        return
    _clamp(folder, DIR_MODE, changed, same, skipped, base)
    try:
        children = sorted(folder.iterdir())
    except OSError:
        return
    for child in children:
        if child.is_symlink():
            continue
        if child.is_file():
            _clamp(child, FILE_MODE, changed, same, skipped, base)
        elif child.is_dir():
            _clamp_file_folder(child, base, acc)


def _env_file_for(base: Path, env_path: Optional[Path]) -> Path:
    """The env file the owner-key check reads: the caller's, else ``TOPOS_ENV_FILE`` (the shell app and the
    tests point it elsewhere; ``owner_key._env_path`` honours the same variable), else ``<base>/.env``."""
    if env_path is not None:
        return Path(env_path)
    override = (os.environ.get("TOPOS_ENV_FILE") or "").strip()
    return Path(override).expanduser() if override else base / ".env"


def harden_topos_dir(topos_dir: Optional[Path] = None, *, env_path: Optional[Path] = None) -> Dict[str, Any]:
    """Clamp modes under ``topos_dir`` (default ``~/.topos``) and check the owner key.

    Returns ``{"dir", "changed", "already_clamped", "changed_paths", "skipped",
    "owner_key_persisted", "finding"?}``. Paths are relative; nothing in the
    report is file content.
    """
    base = (topos_dir or (Path.home() / ".topos")).expanduser()
    changed: List[str] = []
    same: List[str] = []
    skipped: List[str] = []
    acc = (changed, same, skipped)
    if not base.is_dir():
        return {
            "dir": str(base),
            "changed": 0,
            "already_clamped": 0,
            "changed_paths": [],
            "skipped": ["missing"],
            "owner_key_persisted": owner_key_persisted(_env_file_for(base, env_path)),
        }

    _clamp(base, DIR_MODE, changed, same, skipped, base)
    for name in _TOP_FILES:
        p = base / name
        if p.is_file() and not p.is_symlink():
            _clamp(p, FILE_MODE, changed, same, skipped, base)
    for name in _FILE_FOLDERS:
        _clamp_file_folder(base / name, base, acc)
    profiles = base / "profiles"
    if profiles.is_dir() and not profiles.is_symlink():
        _clamp(profiles, DIR_MODE, changed, same, skipped, base)
        try:
            archived = sorted(p for p in profiles.iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            archived = []
        for profile in archived:
            _clamp(profile, DIR_MODE, changed, same, skipped, base)
            for name in _DB_TRIO:
                p = profile / name
                if p.is_file() and not p.is_symlink():
                    _clamp(p, FILE_MODE, changed, same, skipped, base)
            _clamp_file_folder(profile / "logs", base, acc)

    persisted = owner_key_persisted(_env_file_for(base, env_path))
    report: Dict[str, Any] = {
        "dir": str(base),
        "changed": len(changed),
        "already_clamped": len(same),
        "changed_paths": changed,
        "skipped": skipped,
        "owner_key_persisted": persisted,
    }
    if not persisted:
        report["finding"] = OWNER_KEY_FINDING
        logger.warning("hardening: %s", OWNER_KEY_FINDING)
    if changed:
        logger.info("hardening: clamped %d path(s) under %s", len(changed), base)
    return report


def exec_local_hardening(step: Dict[str, Any], conn: Optional[sqlite3.Connection]) -> Dict[str, Any]:
    """Runner executor for kind ``local_hardening``: the live node's own ``~/.topos``."""
    return harden_topos_dir()
