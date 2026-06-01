"""Safe string previews for ingestion debug logs."""

from __future__ import annotations

import json
from typing import Any


def field_preview(value: Any, max_len: int = 100) -> str:
    """Return a truncated preview safe for logging regardless of field type."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value[:max_len]
    if isinstance(value, (dict, list)):
        try:
            text = json.dumps(value, ensure_ascii=False)
        except TypeError:
            text = str(value)
        return text[:max_len]
    return str(value)[:max_len]
