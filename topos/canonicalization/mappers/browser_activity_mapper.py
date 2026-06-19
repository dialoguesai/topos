"""Browser visits/events → wiki activity_events canonical mapper."""

from __future__ import annotations

from dataclasses import dataclass

from ...ingestion.parsers.base import NormalizedRecord
from .base import CanonicalMapper, CanonicalRecord, MappingMetadata


@dataclass
class BrowserActivityCanonicalMapper(CanonicalMapper):
    version: str = "v1"

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        payload = normalized.payload
        record_id = str(payload.get("id") or normalized.record_id)
        activity_type = payload.get("event_type") or payload.get("activity_type") or "visit"
        canonical = {
            "event_id": f"browser:{record_id}",
            "activity_type": activity_type,
            "url": payload.get("url"),
            "title": payload.get("title"),
            "occurred_at": payload.get("visited_at") or payload.get("occurred_at") or payload.get("ts"),
            "source_record_id": record_id,
            "metadata_json": {"schema": payload.get("schema") or payload.get("schema_id")},
        }
        return CanonicalRecord(record_id=canonical["event_id"], payload=canonical)

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="browser_activity", mapping_version=self.version)
