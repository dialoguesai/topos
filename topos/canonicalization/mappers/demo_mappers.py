"""Canonical mappers for signal-dimension demo harness sources."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ...ingestion.parsers.base import NormalizedRecord
from .base import CanonicalMapper, CanonicalRecord, MappingMetadata


@dataclass
class DemoCalendarMapper(CanonicalMapper):
    version: str = "v1"

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        p = normalized.payload
        event_id = str(p.get("event_id") or normalized.record_id)
        metadata = {
            "location": p.get("location"),
            "attendees": p.get("attendees"),
            "is_busy": p.get("is_busy"),
        }
        canonical = {
            "event_id": event_id,
            "title": p.get("title"),
            "starts_at": p.get("starts_at"),
            "ends_at": p.get("ends_at"),
            "source_record_id": event_id,
            "metadata_json": metadata,
        }
        return CanonicalRecord(record_id=event_id, payload=canonical)

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="demo_calendar", mapping_version=self.version)


@dataclass
class DemoJournalMapper(CanonicalMapper):
    version: str = "v1"

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        p = normalized.payload
        entry_id = str(p.get("entry_id") or normalized.record_id)
        canonical = {
            "entry_id": entry_id,
            "entry_at": p.get("entry_at"),
            "mood_tag": p.get("mood_tag"),
            "category": p.get("category"),
            "content": p.get("content"),
            "people": p.get("people"),
            "place_name": p.get("place_name"),
            "source_record_id": entry_id,
        }
        return CanonicalRecord(record_id=entry_id, payload=canonical)

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="demo_journal", mapping_version=self.version)


@dataclass
class DemoProfileMapper(CanonicalMapper):
    version: str = "v1"

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        p = normalized.payload
        record_id = str(p.get("record_id") or normalized.record_id)
        canonical = {
            "record_id": record_id,
            "record_type": p.get("record_type"),
            "title": p.get("title"),
            "organization": p.get("organization"),
            "start_date": p.get("start_date"),
            "end_date": p.get("end_date"),
            "description": p.get("description"),
            "source_record_id": record_id,
        }
        return CanonicalRecord(record_id=record_id, payload=canonical)

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="demo_profile", mapping_version=self.version)


@dataclass
class DemoFinancialMapper(CanonicalMapper):
    version: str = "v1"

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        p = normalized.payload
        transaction_id = str(p.get("transaction_id") or normalized.record_id)
        canonical = {
            "transaction_id": transaction_id,
            "account_type": p.get("account_type"),
            "account_name": p.get("account_name"),
            "posted_at": p.get("posted_at"),
            "amount": p.get("amount"),
            "currency": p.get("currency"),
            "category": p.get("category"),
            "description": p.get("description"),
            "source_record_id": transaction_id,
        }
        return CanonicalRecord(record_id=transaction_id, payload=canonical)

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="demo_financial", mapping_version=self.version)


@dataclass
class DemoPlacesMapper(CanonicalMapper):
    version: str = "v1"

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        p = normalized.payload
        event_id = str(p.get("event_id") or normalized.record_id)
        canonical = {
            "event_id": event_id,
            "place_name": p.get("place_name"),
            "city": p.get("city"),
            "region": p.get("region"),
            "country": p.get("country"),
            "event_at": p.get("event_at"),
            "event_type": p.get("event_type"),
            "source_record_id": event_id,
        }
        return CanonicalRecord(record_id=event_id, payload=canonical)

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="demo_places", mapping_version=self.version)


@dataclass
class JournalTimeLogMapper(CanonicalMapper):
    version: str = "v1"

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        p = normalized.payload
        entry_id = str(p.get("entry_id") or normalized.record_id)
        entry_at = p.get("entry_at")
        if not entry_at:
            entry_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        metadata = {
            "duration_minutes": p.get("duration_minutes"),
            "goal": p.get("goal"),
            "goal_entities": p.get("goal_entities"),
            "accomplished_entities": p.get("accomplished_entities"),
            "completed": p.get("completed"),
            **(p.get("_metadata") if isinstance(p.get("_metadata"), dict) else {}),
        }
        canonical = {
            "entry_id": entry_id,
            "entry_at": entry_at,
            "starts_at": p.get("starts_at"),
            "ends_at": p.get("ends_at"),
            "mood_tag": p.get("mood_tag"),
            "category": p.get("category"),
            "content": p.get("content"),
            "duration": p.get("duration") or p.get("duration_minutes"),
            "people": p.get("people"),
            "place_name": p.get("place_name"),
            "source_record_id": entry_id,
            "metadata_json": metadata,
        }
        return CanonicalRecord(record_id=entry_id, payload=canonical)

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="journal_time_log", mapping_version=self.version)


@dataclass
class DemoContactsMapper(CanonicalMapper):
    version: str = "v1"

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        p = normalized.payload
        contact_id = str(p.get("contact_id") or normalized.record_id)
        canonical = {
            "contact_id": contact_id,
            "display_name": p.get("display_name"),
            "identifier": p.get("identifier"),
            "identifier_type": p.get("identifier_type"),
            "source_record_id": f"{contact_id}:{p.get('identifier')}",
        }
        return CanonicalRecord(record_id=canonical["source_record_id"], payload=canonical)

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="demo_contacts", mapping_version=self.version)


@dataclass
class DemoMessengerMapper(CanonicalMapper):
    """Pass-through mapper; conversations pipeline uses staging shape."""

    version: str = "v1"

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        return CanonicalRecord(record_id=normalized.record_id, payload=dict(normalized.payload))

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="demo_messenger", mapping_version=self.version)
