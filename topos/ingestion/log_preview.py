"""What ingestion's log lines say about a record's content: its length and a short digest, never its text (NR181)."""

from __future__ import annotations

from typing import Any

from ..core.log_fingerprint import fingerprint


def field_preview(value: Any, max_len: int = 100) -> str:
    """`length=<bytes> sha256=<12 hex>` of the content, for any field type; never raises.

    NR181: this was the content's first `max_len` characters. A message the owner's app sends through the relay
    (`store_message`) reaches `ingest_ui_payload`, which logs it at INFO, so a hundred characters of every captured
    message were in the node's log. The name and `max_len` are kept so the callers do not change."""
    _ = max_len
    return fingerprint(value)
