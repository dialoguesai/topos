"""Rows whose text quotes a message someone else sent.

An iMessage reaction ("tapback") is a row authored by whoever reacted, but its
text reproduces the message it reacts to, for example 'Loved “I work at X”'.
When the owner reacts to a correspondent, that text is the correspondent's, not
the owner's. Native readers mark such a row with ``associated_message_guid``
and a non-zero ``associated_message_type`` (0 is an ordinary message). Neither
fact extractor may read a first-person statement out of one.

Permission evidence refuses the same keys independently
(``permissions_v2/evidence.py``); this module decides only extraction.
"""
from __future__ import annotations

import json
from typing import Any, Dict

REACTION_METADATA_KEYS = ("associated_message_guid", "associated_message_type")


def _metadata(row: Dict[str, Any]) -> list:
    found = []
    for key in ("metadata_json", "_metadata", "metadata"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                # Unreadable metadata cannot show the row is ordinary.
                return [None]
        if isinstance(value, dict):
            found.append(value)
    return found


def quotes_another_message(row: Dict[str, Any]) -> bool:
    """True when a row carries reaction metadata, or metadata too malformed to rule it out."""
    for metadata in _metadata(row):
        if metadata is None:
            return True
        if any(metadata.get(key) not in (None, False, 0, "0", "", [], {}) for key in REACTION_METADATA_KEYS):
            return True
    return False
