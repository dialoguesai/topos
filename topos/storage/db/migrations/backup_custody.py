"""Hand this node's backups to the disk floor.

`engine/disk_space.py` decides whether there is room for a model, and since
2026-08-26 it prefers spending a superseded pre-migration backup over evicting
a model the owner would have to re-download. Both of those live on one volume
today — but only today. The engine plane is meant to be movable to a second
machine (SYS-node I1, D-001), and the floor running there must not resolve a
database path of its own: on any box that happens to hold a ``~/.topos``, it
would find *that* node's rollback ladder and prune it for our download.

So the knowledge stays where it belongs. Where this node's backups are, and
what retention has already condemned in them, is data-plane knowledge, and the
data plane is what carries it across — one call at startup, from the process
that actually holds the database.

A process that never makes that call — a remote engine, the CLI, a test —
leaves the floor with no backups to count or spend. That is not a degraded
floor. It is the correct reading of a machine whose backups are somewhere else.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..paths import resolve_active_database
from .backup import backup_dir_for, prune_to_retention, retention_report

logger = logging.getLogger("topos.storage.db.migrations.backup_custody")


class ActiveNodeBackups:
    """The backups of the Topos this process serves.

    Resolved through the same two functions the migration path uses, so the
    floor looks at the directory that actually gets written rather than a second
    guess at it — ``TOPOS_BACKUP_DIR`` and the profile slot both move it.

    Resolved per call, not cached: the active profile can change under a running
    node, and a cached directory would let a floor prune the backups of the
    Topos it was serving an hour ago.
    """

    def directory(self) -> Optional[Path]:
        return backup_dir_for(resolve_active_database().path)

    def report(self, directory: Path) -> Dict[str, Any]:
        return retention_report(directory)

    def prune(self, directory: Path) -> Tuple[List[str], int]:
        removed, freed = prune_to_retention(directory)
        return [path.name for path in removed], freed


def hand_backups_to_the_disk_floor() -> None:
    """Install this node's backup custody. Called once, from node startup.

    Never raises. A floor with no backups behind it still holds the line — it
    just has one fewer thing to reach for before it reaches for a model, which
    is a worse trade for the owner but never an unsafe one.
    """
    try:
        from topos.engine.disk_space import install_node_backups

        install_node_backups(ActiveNodeBackups())
    except Exception as exc:  # noqa: BLE001 — never block startup on the disk floor
        logger.debug("backup custody not installed: %s", exc)
