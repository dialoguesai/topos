"""Optional runtime extensions via setuptools entry points.

Third-party packages register handlers, connectors, or other hooks by declaring
an entry in the ``topos.extensions`` group (see CONTRIBUTING.md). The core node
does not hardcode any extension package names. See the **Plugins** section in
README.md for the full contract.
"""

from __future__ import annotations

import importlib.metadata
import logging

logger = logging.getLogger("topos.extensions")

_EXTENSION_GROUP = "topos.extensions"


def _iter_extension_entry_points():
    try:
        return importlib.metadata.entry_points(group=_EXTENSION_GROUP)
    except TypeError:
        # Python 3.9: entry_points() has no group= kwarg.
        return importlib.metadata.entry_points().get(_EXTENSION_GROUP, [])


def load_extensions() -> None:
    """Load every installed ``topos.extensions`` entry point."""
    for entry_point in _iter_extension_entry_points():
        try:
            loaded = entry_point.load()
        except Exception as exc:  # noqa: BLE001 — extension load must not crash the node
            logger.warning("Failed to load extension %s: %s", entry_point.name, exc)
            continue
        if callable(loaded):
            try:
                loaded()
            except Exception as exc:  # noqa: BLE001
                logger.warning("Extension %s raised during init: %s", entry_point.name, exc)
                continue
        logger.info("Loaded extension: %s", entry_point.name)
