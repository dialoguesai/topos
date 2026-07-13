from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from ...ingestion.parsers.base import NormalizedRecord


@dataclass(frozen=True)
class CanonicalRecord:
    record_id: str
    payload: Dict[str, str]
    # Explicit canonical target table for dual-lane mappers. None means "the
    # default table for the source's canonical_group_id" (existing behavior);
    # single-lane mappers never need to set it.
    table: Optional[str] = None


@dataclass(frozen=True)
class MappingMetadata:
    source_id: str
    mapping_version: str


class CanonicalMapper:
    def map(self, normalized: NormalizedRecord) -> CanonicalRecord:
        raise NotImplementedError

    def map_many(self, normalized: NormalizedRecord) -> List[CanonicalRecord]:
        """Multi-lane hook: one normalized record → one or more canonical rows,
        each optionally targeting a different canonical table via
        ``CanonicalRecord.table``. Default delegates to :meth:`map`, so
        single-lane mappers keep working unchanged."""
        return [self.map(normalized)]

    def mapping_metadata(self, normalized: NormalizedRecord) -> MappingMetadata:
        raise NotImplementedError
