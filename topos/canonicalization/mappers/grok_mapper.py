from __future__ import annotations

from dataclasses import dataclass

from ...ingestion.parsers.base import NormalizedRecord
from .base import CanonicalMapper, CanonicalRecord, MappingMetadata


@dataclass
class GrokCanonicalMapper(CanonicalMapper):
    version: str = "v1"

    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        return CanonicalRecord(record_id=normalized.record_id, payload=normalized.payload)

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        return MappingMetadata(source_id="grok", mapping_version=self.version)
