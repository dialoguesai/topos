"""Contextual embed-text construction.

Personal-data chunks often lose meaning in isolation ("she said yes"). Prepending
a compact header (record kind, date, title/source) before embedding materially
improves retrieval; the stored search_text/text_preview stay raw so FTS and
display are unaffected.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .vector_settings import embed_context_headers_enabled

_TABLE_LABELS = {
    "ai_chat_messages": "ai chat",
    "ai_chat_message": "ai chat",
    "activity_events": "web activity",
    "activity_event": "web activity",
    "journal_entries": "journal",
    "journal_entry": "journal",
    "conversation_messages": "message",
    "conversation_message": "message",
    "profile_records": "profile",
    "profile_record": "profile",
    "calendar_events": "calendar",
    "calendar_event": "calendar",
    "contacts": "contact",
    "financial_transactions": "finance",
    "location_events": "place",
}


def _event_date(msg: Dict[str, Any]) -> str:
    for field in ("event_at", "ts", "entry_at", "occurred_at", "starts_at", "created_at"):
        value = msg.get(field)
        if value:
            return str(value)[:10]
    return ""


def context_header(msg: Dict[str, Any], *, record_type: Optional[str] = None) -> str:
    """Compact one-line context header, or empty string when disabled."""
    if not embed_context_headers_enabled():
        return ""
    kind = str(
        record_type
        or msg.get("record_type")
        or msg.get("_table")
        or msg.get("canonical_table")
        or ""
    )
    label = _TABLE_LABELS.get(kind, kind.replace("_", " ")).strip()
    parts = [p for p in (label, _event_date(msg)) if p]
    title = str(msg.get("title") or "").strip()
    org = str(msg.get("organization") or "").strip()
    place = str(msg.get("place_name") or "").strip()
    people = str(msg.get("people") or msg.get("attendees") or "").strip()
    for extra in (title, org, place, people):
        if extra and extra not in parts:
            parts.append(extra)
    if not parts:
        return ""
    return " | ".join(parts[:4])


def embeddable_content(msg: Dict[str, Any]) -> str:
    """Primary text for embedding; falls back through descriptive fields."""
    content = str(msg.get("content") or "").strip()
    if content:
        return content
    parts = [
        str(msg.get(field) or "").strip()
        for field in ("title", "organization", "description", "url")
    ]
    return " — ".join(p for p in parts if p)


def build_embed_text(msg: Dict[str, Any], chunk: str, *, record_type: Optional[str] = None) -> str:
    """Header + chunk (the text actually sent to the embedding model)."""
    header = context_header(msg, record_type=record_type)
    if not header:
        return chunk
    return f"{header}\n{chunk}"
