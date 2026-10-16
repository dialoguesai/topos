"""Where the `topos-node` binary lives — the Topos.app order (CONTRACTS §4).

GUI-launched hosts have an empty PATH (``launchctl getenv PATH``) and launchd
jobs get ``/usr/bin:/bin:/usr/sbin:/sbin``, so nothing that starts the node for
the user may rely on ``which``. The service unit, the plugin launcher and
``doctor`` all resolve an absolute path from this list, in this order:

1. ``$TOPOS_NODE_BIN`` — an explicit override;
2. ``~/.local/bin/topos-node`` — the ``uv tool install`` shim;
3. ``~/.topos/runtime/bin/topos-node`` — the app-managed runtime;
4. ``/opt/homebrew/bin/topos-node``;
5. ``/usr/local/bin/topos-node``.

``shutil.which`` is consulted LAST, after every listed path, and only so a
developer checkout on PATH keeps working; it is never the first answer.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import List, Optional

BINARY_NAME = "topos-node"
ENV_OVERRIDE = "TOPOS_NODE_BIN"

#: The fixed candidates after the env override, relative to HOME where they
#: start with ``~``. Mirrors ``NodeSupervisor.findNodeBinary`` in topos-shell-macos.
CANDIDATES = (
    "~/.local/bin/topos-node",
    "~/.topos/runtime/bin/topos-node",
    "/opt/homebrew/bin/topos-node",
    "/usr/local/bin/topos-node",
)


def candidate_paths() -> List[Path]:
    """Every place the binary may be, most preferred first (no existence check)."""
    out: List[Path] = []
    override = (os.environ.get(ENV_OVERRIDE) or "").strip()
    if override:
        out.append(Path(override).expanduser())
    for raw in CANDIDATES:
        out.append(Path(raw).expanduser() if raw.startswith("~") else Path(raw))
    return out


def _runnable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def resolve_node_binary() -> Optional[Path]:
    """The first candidate that exists and is executable, else a PATH lookup, else None."""
    for candidate in candidate_paths():
        if _runnable(candidate):
            return candidate
    found = shutil.which(BINARY_NAME)
    if found and _runnable(Path(found)):
        return Path(found)
    return None
