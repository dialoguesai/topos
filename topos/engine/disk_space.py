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
from typing import Any, NamedTuple, Optional
from urllib.parse import urlparse

logger = logging.getLogger("topos.engine.disk_space")

#: Headroom to leave AFTER the model lands, for the node's own database and the
#: enrichment it runs continuously. Not tuned to a measurement — it is the
#: smallest number that is obviously more than "a few writes".
DEFAULT_RESERVE_BYTES = 2 * 1024**3

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
    reserve_bytes: int = DEFAULT_RESERVE_BYTES,
    path: Optional[Path] = None,
) -> Optional[SpaceVerdict]:
    """A verdict when the pull would not fit, or None to go ahead.

    None covers every "we do not know": a remote Ollama, an unreadable volume,
    or a size we were never told. Only a positive finding — a real free-space
    number that is smaller than a real requirement — refuses.
    """
    if not space_check_applies(base_url):
        return None
    try:
        needed = int(needed_bytes or 0)
    except (TypeError, ValueError):
        return None
    if needed <= 0:
        return None
    target = path or ollama_models_dir()
    available = free_bytes(target)
    if available is None:
        return None
    if available >= needed + max(0, int(reserve_bytes)):
        return None
    resolved = _existing_ancestor(target)
    return SpaceVerdict(
        needed_bytes=needed,
        free_bytes=available,
        reserve_bytes=max(0, int(reserve_bytes)),
        path=str(resolved or target),
    )
