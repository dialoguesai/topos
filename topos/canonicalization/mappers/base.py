from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

from ...ingestion.parsers.base import NormalizedRecord


@dataclass(frozen=True)
class CanonicalRecord:
    record_id: str
    payload: Dict[str, str]


@dataclass(frozen=True)
class MappingMetadata:
    source_id: str
    mapping_version: str


class CanonicalMapper:
    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        raise NotImplementedError

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        raise NotImplementedError
