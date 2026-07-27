"""Best-effort local housekeeping run at node start.

Motivation (2026-07-27): pytest tmp roots (``$TMPDIR/pytest-of-<user>``) filled
the dev machine to 100%, which is fatal for a sqlite-backed node — ENOSPC
mid-write is how databases corrupt. pytest prunes its own old roots, but a
run that is killed mid-flight leaves a ``.lock`` behind and pytest skips
locked roots forever, so they accumulate. The engine repo also bounds
retention (``tmp_path_retention_count``), but that only helps runs that
finish.

``sweep_stale_pytest_tmp`` removes numbered ``pytest-*`` roots whose mtime is
older than ``TOPOS_TMP_SWEEP_MAX_AGE_HOURS`` (default 24; ``0`` disables the
sweep). The age gate is the safety: an in-flight run's root was just created,
so only abandoned roots are ever touched. Everything is wrapped so
housekeeping can never break node startup, and the caller runs it on a daemon
thread so a slow disk never delays boot.
"""

from __future__ import annotations

import getpass
import logging
import os
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger("topos.runtime_housekeeping")

_DEFAULT_MAX_AGE_HOURS = 24.0


def _max_age_hours() -> float:
    raw = os.environ.get("TOPOS_TMP_SWEEP_MAX_AGE_HOURS", "")
    if not raw.strip():
        return _DEFAULT_MAX_AGE_HOURS
    try:
        return max(0.0, float(raw))
    except ValueError:
        return _DEFAULT_MAX_AGE_HOURS


def _dir_size_bytes(path: Path) -> int:
    total = 0
    try:
        for child in path.rglob("*"):
            try:
                if child.is_file() and not child.is_symlink():
                    total += child.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def sweep_stale_pytest_tmp(max_age_hours: float | None = None) -> Dict[str, Any]:
    """Remove abandoned pytest tmp roots for the current user. Never raises.

    Returns ``{"removed": int, "freed_bytes": int, "skipped": int}``.
    """
    result: Dict[str, Any] = {"removed": 0, "freed_bytes": 0, "skipped": 0}
    try:
        age_hours = _max_age_hours() if max_age_hours is None else max_age_hours
        if age_hours <= 0:
            return result
        root = Path(tempfile.gettempdir()) / f"pytest-of-{getpass.getuser()}"
        if not root.is_dir():
            return result
        cutoff = time.time() - age_hours * 3600.0
        for entry in root.iterdir():
            try:
                if not entry.is_dir() or entry.is_symlink():
                    result["skipped"] += 1
                    continue
                if entry.stat().st_mtime >= cutoff:
                    result["skipped"] += 1
                    continue
                freed = _dir_size_bytes(entry)
                shutil.rmtree(entry, ignore_errors=True)
                if entry.exists():  # partially removed (permissions?) — count as skip
                    result["skipped"] += 1
                    continue
                result["removed"] += 1
                result["freed_bytes"] += freed
            except OSError:
                result["skipped"] += 1
        # Drop the now-empty parent so the next pytest run starts fresh.
        try:
            if result["removed"] and not any(root.iterdir()):
                root.rmdir()
        except OSError:
            pass
        if result["removed"]:
            logger.info(
                "housekeeping: removed %d stale pytest tmp root(s), freed %.1f MB",
                result["removed"],
                result["freed_bytes"] / (1024 * 1024),
            )
    except Exception as exc:  # noqa: BLE001 — housekeeping must never break startup
        logger.debug("housekeeping sweep failed: %s", exc)
    return result


def start_background_housekeeping() -> None:
    """Fire-and-forget housekeeping on a daemon thread (called at node start)."""
    try:
        threading.Thread(
            target=sweep_stale_pytest_tmp,
            name="topos-housekeeping",
            daemon=True,
        ).start()
    except Exception as exc:  # noqa: BLE001
        logger.debug("housekeeping thread failed to start: %s", exc)
