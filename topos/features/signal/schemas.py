"""Pydantic-style dict schemas for signal API responses."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

METADATA_EXCLUDE_KEYS = frozenset({"vector", "vector_blob", "embedding"})


def strip_vector_fields(item: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in item.items() if k not in METADATA_EXCLUDE_KEYS}
