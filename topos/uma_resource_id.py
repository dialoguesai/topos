"""Parse UMA ``resource_id`` strings on the engine (no control_plane dependency)."""

from __future__ import annotations

from typing import Optional


def parse_dataset_id_from_uma_dataset_resource_id(resource_id: Optional[str]) -> Optional[str]:
    """
    For ``dataset:{owner}:{dataset_segments...}:{device_id}``, return the dataset key used in SQLite.

    ``dataset_id`` may contain colons (e.g. ``{uuid}:default``), so it is everything after the owner
    and before the final segment (device_id). This must match ``control_plane.uma.models.resource_id_parse``.
    """
    rid = (resource_id or "").strip()
    if not rid:
        return None
    parts = rid.split(":")
    if len(parts) < 4:
        return None
    if (parts[0] or "").lower() != "dataset":
        return None
    inner = ":".join(parts[2:-1])
    return inner.strip() or None
