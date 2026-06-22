"""Fan out Grow journal rows with location into location_events."""

from __future__ import annotations

from typing import Any, Dict, Optional


def grow_location_event_from_journal(
    journal: Dict[str, Any],
    *,
    source_id: str,
) -> Optional[Dict[str, Any]]:
    """Build a location_events row linked to a grow journal entry, or None if no place."""
    entry_id = str(journal.get("entry_id") or journal.get("source_record_id") or "").strip()
    place_name = str(journal.get("place_name") or "").strip()
    if not entry_id or not place_name:
        return None
    return {
        "event_id": f"{entry_id}-loc",
        "place_name": place_name,
        "event_at": journal.get("entry_at"),
        "event_type": str(journal.get("category") or "activity").strip() or "activity",
        "source_id": source_id,
        "source_record_id": entry_id,
        "metadata_json": {
            "journal_entry_id": entry_id,
            "fanout": "grow_location",
        },
    }


def grow_location_signal_record(location_event: Dict[str, Any]) -> Dict[str, Any]:
    """Signal-ready dict for enrichment (Places dimension loaders)."""
    return {
        "event_id": location_event.get("event_id"),
        "record_id": location_event.get("event_id"),
        "message_id": location_event.get("event_id"),
        "place_name": location_event.get("place_name"),
        "content": str(location_event.get("place_name") or ""),
        "event_at": location_event.get("event_at"),
        "category": location_event.get("event_type"),
        "source_id": location_event.get("source_id"),
        "canonical_table": "location_events",
    }
