"""Browser visits/events → wiki activity_events canonical mapper.

PLAN_PROVENANCE_SPLIT P2.1: the browser plugin captures a page's ``hostname``
and, for highlight/selection events, the selected span (in the event payload's
``content``, sometimes wrapped as JSON ``{"selectedText": …}``). The v1 mapper
dropped both. This mapper now:

  * carries ``hostname`` onto the canonical row (new activity_events column);
  * for highlight/selection events, sets ``activity_type='browser_highlight'``
    (role=ambient + engaged / Annotate-grade — expression-grade for INTEREST,
    never belief; see features/provenance/roles.is_engaged) and pulls the
    selected span into ``content`` AND mirrors it into ``metadata_json`` under
    ``highlight`` so the query layer can surface the span (the canonical list
    adapter selects metadata_json/content, not the raw event columns).

Page-body text (``page_excerpt``) is deliberately NOT promoted to ``content``
or to the highlight key — that span is the *page author's* words (poison for a
first-person "what did I take away" ask), so it stays only in metadata_json
where the exposure lane can attribute it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

from ...ingestion.parsers.base import NormalizedRecord
from .base import CanonicalMapper, CanonicalRecord, MappingMetadata

# Event types the plugin emits for an owner-selected span (Annotate engagement).
_HIGHLIGHT_EVENT_TYPES = frozenset({"highlight", "selection", "select"})
# Keys a wrapped highlight payload may carry the span under.
_SELECTED_TEXT_KEYS = ("selectedText", "selected_text", "highlight", "text")


def _extract_selected_text(content: Any) -> str:
    """The owner-selected span from a browser event's ``content`` field.

    The plugin sends the span either as a bare string or wrapped in a small
    JSON object ({"selectedText": …}); a dict of pointer coordinates (a click
    event's {x, y, element}) carries no span and yields ''."""
    if content is None:
        return ""
    if isinstance(content, str):
        text = content.strip()
        if not text:
            return ""
        # A JSON-encoded object may hide the span behind selectedText/highlight.
        if text[0] in "{[":
            try:
                parsed = json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return text
            return _extract_selected_text(parsed)
        return text
    if isinstance(content, dict):
        for key in _SELECTED_TEXT_KEYS:
            val = content.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        return ""
    return ""


@dataclass
class BrowserActivityCanonicalMapper(CanonicalMapper):
    version: str = "v2"

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        payload = normalized.payload
        record_id = str(payload.get("id") or normalized.record_id)
        event_type = str(payload.get("event_type") or "").strip().lower()
        activity_type = payload.get("event_type") or payload.get("activity_type") or "visit"

        metadata: dict[str, Any] = {
            "schema": payload.get("schema") or payload.get("schema_id"),
        }
        hostname = payload.get("hostname")
        if not hostname and payload.get("url"):
            # BT6 (PLAN_ATTENTION_TRIAGE.md): some plugin batches send url
            # without hostname; derive it so downstream vocab never goes empty.
            try:
                hostname = urlparse(str(payload.get("url"))).hostname
            except ValueError:
                hostname = None
        if hostname:
            metadata["hostname"] = hostname

        content: Optional[str] = None
        if event_type in _HIGHLIGHT_EVENT_TYPES:
            selected = _extract_selected_text(payload.get("content"))
            if selected:
                # Annotate-grade engagement: the span is the owner's expression
                # of INTEREST. Promote to a stable activity_type + carry the
                # span on both the content column and metadata_json.highlight.
                activity_type = "browser_highlight"
                content = selected
                metadata["highlight"] = selected

        canonical = {
            "event_id": f"browser:{record_id}",
            "activity_type": activity_type,
            "url": payload.get("url"),
            "title": payload.get("title"),
            "hostname": hostname,
            "content": content,
            "occurred_at": payload.get("visited_at") or payload.get("occurred_at") or payload.get("ts"),
            "source_record_id": record_id,
            "metadata_json": metadata,
        }
        return CanonicalRecord(record_id=canonical["event_id"], payload=canonical)

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="browser_activity", mapping_version=self.version)
