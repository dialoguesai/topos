"""Is there room to download this model, and what to say when there is not.

A model pull is the largest write this node ever asks a machine to make — the
curated starter alone is 2.0 GB — and until now nothing checked. Two things go
wrong when the disk is full, and the second is the serious one:

  * the download fails, late, after the owner has watched a progress bar reach
    97%; and
  * the disk fills. The node's SQLite lives on the same volume, and
    ``runtime_housekeeping`` already records what that costs: "fatal for a
    sqlite-backed node — ENOSPC mid-write is how databases corrupt." Refusing a
    pull is cheap. Corrupting the owner's Topos is not.

So this module answers before the transfer starts, and again as soon as the
stream reveals the real size.

Two judgements it deliberately declines to make:

  * **A remote Ollama is not our disk.** `engine_ollama_base_url` can point at
    another machine, and refusing that owner's pull because THIS box is full
    would be inventing a problem. `space_check_applies` says so, and every
    caller treats "not applicable" as "go ahead".
  * **An unreadable disk is not a full disk.** `shutil.disk_usage` can fail on
    an odd mount; None means "did not check", which reads as go-ahead, matching
    the None-is-not-a-finding rule the pack resolver is built on.

The reserve exists because "enough room for the model" is not the same as
"enough room afterwards". Landing a 2 GB model with 40 MB to spare leaves the
node one enrichment batch away from the corruption above.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Protocol, Tuple
from urllib.parse import urlparse

logger = logging.getLogger("topos.engine.disk_space")

#: Headroom to leave AFTER the model lands, for the node's own database and the
#: enrichment it runs continuously, when the owner has not set a floor of their
#: own. Not tuned to a measurement — it is the smallest number that is obviously
#: more than "a few writes", raised to 10 GB when the floor became a setting so
#: the shipped default matches what Settings -> General shows.
DEFAULT_MIN_FREE_BYTES = 10_000_000_000

#: Historical spelling, kept because callers and tests import it by this name.
DEFAULT_RESERVE_BYTES = DEFAULT_MIN_FREE_BYTES

#: Hosts that mean "the machine this node runs on".
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", ""})


class SpaceVerdict(NamedTuple):
    """Why a pull was refused, in the numbers the owner needs to act."""

    needed_bytes: int
    free_bytes: int
    reserve_bytes: int
    path: str

    @property
    def shortfall_bytes(self) -> int:
        return max(0, (self.needed_bytes + self.reserve_bytes) - self.free_bytes)

    def message(self, tag: str = "") -> str:
        """One sentence naming the model, the gap, and where to free it."""
        subject = f"{tag} needs" if tag else "This download needs"
        return (
            f"Not enough disk space. {subject} {format_bytes(self.needed_bytes)} "
            f"and {format_bytes(self.reserve_bytes)} should stay free for your Topos, "
            f"but {self.path} has {format_bytes(self.free_bytes)} left — "
            f"about {format_bytes(self.shortfall_bytes)} short."
        )


def format_bytes(count: Any) -> str:
    """Human size, in the GB/MB the owner sees in their file manager."""
    try:
        value = float(count or 0)
    except (TypeError, ValueError):
        # Same answer as a genuine zero: "we have nothing to report" and "there
        # is nothing" are the same sentence to a reader, and two spellings of it
        # is just a seam for a test to disagree with itself over.
        return "0 bytes"
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f} GB"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.0f} MB"
    return f"{max(0, int(value))} bytes"


def ollama_models_dir() -> Path:
    """Where Ollama writes models on this machine.

    `OLLAMA_MODELS` wins when set — an owner who moved their models to a second
    drive has done exactly the thing that makes checking the home volume wrong.
    """
    override = str(os.environ.get("OLLAMA_MODELS") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".ollama" / "models"


def _existing_ancestor(path: Path) -> Optional[Path]:
    """The nearest directory that exists — a models dir may not be created yet."""
    candidate = path.expanduser()
    for _ in range(10):
        if candidate.is_dir():
            return candidate
        parent = candidate.parent
        if parent == candidate:
            return None
        candidate = parent
    return None


def free_bytes(path: Optional[Path] = None) -> Optional[int]:
    """Free bytes on the volume holding `path`, or None if it cannot be read."""
    target = _existing_ancestor(path or ollama_models_dir())
    if target is None:
        return None
    try:
        return int(shutil.disk_usage(target).free)
    except Exception as exc:  # noqa: BLE001 — an unreadable mount is not a full one
        logger.debug("disk usage probe failed for %s: %s", target, exc)
        return None


def min_free_bytes(conn: Any = None) -> int:
    """The owner's floor for this node, or the shipped default.

    Resolved lazily and defensively: this module is imported by the CLI and by
    background pull threads, neither of which is guaranteed a database. A node
    that cannot answer keeps the default rather than dropping the floor to zero
    — losing the setting must never be the thing that fills the volume.
    """
    try:
        from ..config.settings import resolve_min_free_disk_bytes, settings

        if conn is None:
            from ..core.state import get_db_connection

            conn = get_db_connection()
        return int(resolve_min_free_disk_bytes(settings, conn))
    except Exception as exc:  # noqa: BLE001 — no DB is not a reason to stop checking
        logger.debug("min free disk floor unreadable, using default: %s", exc)
        return DEFAULT_MIN_FREE_BYTES


def space_check_applies(base_url: Any) -> bool:
    """Whether THIS machine's disk is the one the download would land on.

    False for a remote Ollama: that owner's free space is not ours to judge, and
    refusing on our own would be a fault we invented.
    """
    text = str(base_url or "").strip()
    if not text:
        return True
    try:
        host = (urlparse(text).hostname or "").strip().lower()
    except Exception:  # noqa: BLE001
        return True
    return host in _LOCAL_HOSTS


def check_space_for(
    needed_bytes: Any,
    *,
    base_url: Any = None,
    reserve_bytes: Optional[int] = None,
    path: Optional[Path] = None,
    conn: Any = None,
) -> Optional[SpaceVerdict]:
    """A verdict when the pull would not fit, or None to go ahead.

    None covers every "we do not know": a remote Ollama, an unreadable volume,
    or a size we were never told. Only a positive finding — a real free-space
    number that is smaller than a real requirement — refuses.

    `reserve_bytes` defaults to the owner's configured floor. Passing one
    explicitly is for callers reasoning about a hypothetical floor (the settings
    preview), not for routine checks — those must see what the owner set.
    """
    if not space_check_applies(base_url):
        return None
    try:
        needed = int(needed_bytes or 0)
    except (TypeError, ValueError):
        return None
    if needed <= 0:
        return None
    reserve = min_free_bytes(conn) if reserve_bytes is None else int(reserve_bytes)
    target = path or ollama_models_dir()
    available = free_bytes(target)
    if available is None:
        return None
    if available >= needed + max(0, reserve):
        return None
    resolved = _existing_ancestor(target)
    return SpaceVerdict(
        needed_bytes=needed,
        free_bytes=available,
        reserve_bytes=max(0, reserve),
        path=str(resolved or target),
    )


class NodeBackups(Protocol):
    """What the data plane lets this floor see, and spend, of its own backups.

    The floor does not go looking for a database. This module runs on the
    machine the models land on, and on a split node that is the GPU box while
    the database — with the backups beside it — is on the other one (SYS-node
    I1, D-001). A floor that resolved a backup directory for itself would, on
    any box that happens to hold a ``~/.topos`` of its own, find a *stranger's*
    rollback ladder and prune it to make room for our download.

    So the data plane hands its backups over instead, and a process that never
    does leaves this floor with none — which is the safe answer and, once the
    planes split, the correct one.
    """

    def directory(self) -> Optional[Path]:
        """Where this node's pre-migration backups live, or None."""

    def report(self, directory: Path) -> Dict[str, Any]:
        """Retention split into prunable / retained / manual, as `backup_space` reports it."""

    def prune(self, directory: Path) -> Tuple[List[str], int]:
        """Delete what retention already condemned; return the names and the bytes freed."""


#: Empty until the data plane installs one. Empty is not a degraded state — it
#: is what a machine holding no database of its own should report.
_CUSTODY: Optional[NodeBackups] = None


def install_node_backups(custody: Optional[NodeBackups]) -> None:
    """Data plane only: hand this node's backups to the disk floor.

    Called once from node startup. Passing None withdraws them, which is what a
    process holding no database should leave in place.
    """
    global _CUSTODY
    _CUSTODY = custody


def node_backups() -> Optional[NodeBackups]:
    """The custody the data plane handed over, or None if it never did."""
    return _CUSTODY


def backup_directory() -> Optional[Path]:
    """Where this node's pre-migration backups live, or None if we hold none.

    None covers both "nothing was handed to us" — a remote engine, the CLI, a
    test — and "custody could not resolve one". Neither is a disk fault, and
    both mean the same thing to every caller: decide the floor without them.
    """
    custody = _CUSTODY
    if custody is None:
        return None
    try:
        return custody.directory()
    except Exception as exc:  # noqa: BLE001 — no database is not a disk fault
        logger.debug("backup directory unresolvable: %s", exc)
        return None


def on_same_volume(left: Path, right: Path) -> Optional[bool]:
    """Whether two paths sit on one filesystem, or None when it cannot be read."""
    here = _existing_ancestor(left)
    there = _existing_ancestor(right)
    if here is None or there is None:
        return None
    try:
        return here.stat().st_dev == there.stat().st_dev
    except OSError as exc:
        logger.debug("device probe failed for %s / %s: %s", here, there, exc)
        return None


def _no_backup_space() -> dict:
    """The shape `backup_space` returns when it has nothing to say.

    Every caller reads the same keys whether or not the numbers were reachable,
    so "we could not look" and "there is nothing there" do not need two branches
    at every call site — they are the same instruction: decide without us.
    """
    return {
        "applies": False,
        "path": None,
        "keep": 0,
        "prunable_bytes": 0,
        "prunable_count": 0,
        "retained_bytes": 0,
        "retained_count": 0,
        "manual_bytes": 0,
        "manual_count": 0,
    }


def backup_space(*, models_path: Optional[Path] = None) -> dict:
    """What the backup directory holds, and how much of it this floor can count.

    `applies` is False unless the backups share a volume with the models. An
    owner who moved `OLLAMA_MODELS` to a second drive would otherwise be told
    that deleting files on their home volume makes room on the other one, which
    is worse than saying nothing: it is a number that justifies a deletion
    which cannot help. Same rule as everywhere else in this module — an
    unreadable device is not a match, so it reports False rather than guessing.
    """
    blank = _no_backup_space()
    custody = _CUSTODY
    directory = backup_directory()
    if custody is None or directory is None or not directory.is_dir():
        return blank
    if on_same_volume(directory, models_path or ollama_models_dir()) is not True:
        return {**blank, "path": str(directory)}
    try:
        report = custody.report(directory)
    except Exception as exc:  # noqa: BLE001 — a directory we cannot read is not a finding
        logger.debug("backup retention report failed for %s: %s", directory, exc)
        return {**blank, "path": str(directory)}
    return {
        "applies": True,
        "path": report["path"],
        "keep": report["keep"],
        "prunable_bytes": report["prunable_bytes"],
        "prunable_count": report["prunable_count"],
        "retained_bytes": report["retained_bytes"],
        "retained_count": report["retained_count"],
        "manual_bytes": report["manual_bytes"],
        "manual_count": report["manual_count"],
    }


def disk_status(conn: Any = None, *, base_url: Any = None) -> dict:
    """What the app shows: the volume, the floor, and whether we are under it.

    `below_floor` is the fact the sidebar warns on, and it is deliberately
    tri-state-safe: `free_bytes` is None when the volume could not be read, and
    an unreadable volume is not a full one, so `below_floor` stays False.
    `applies` is False for a remote Ollama — that machine's disk is not ours to
    report on, and the UI says so rather than showing this node's numbers.

    The `backups` block is what stops a breached floor from spending an Ollama
    model first. A model is the one large thing here that a download can put
    back; a superseded database copy is dead weight the next migration deletes
    anyway. Reporting both means the decision can be made in that order instead
    of on models alone, which was the only number this payload used to carry.
    """
    floor = min_free_bytes(conn)
    target = ollama_models_dir()
    resolved = _existing_ancestor(target)
    applies = space_check_applies(base_url)
    free = free_bytes(target) if applies else None
    total: Optional[int] = None
    if applies and resolved is not None:
        try:
            total = int(shutil.disk_usage(resolved).total)
        except Exception as exc:  # noqa: BLE001
            logger.debug("disk total probe failed for %s: %s", resolved, exc)
    below = free is not None and free < floor
    return {
        # 2 adds the `backups` block below. Additive — every key schema 1
        # carried is still here and still means what it meant.
        "schema": 2,
        "applies": applies,
        "path": str(resolved or target),
        "free_bytes": free,
        "total_bytes": total,
        "min_free_bytes": floor,
        "below_floor": below,
        "shortfall_bytes": max(0, floor - free) if below and free is not None else 0,
        # Remote Ollama: not our volume, so not our backups to offer up either.
        "backups": backup_space(models_path=target) if applies else _no_backup_space(),
    }
