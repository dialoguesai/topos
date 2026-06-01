from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class MappingRecord:
    source_id: str
    source_record_id: str
    canonical_id: str


class MappingStore:
    def get_mapping(self, source_id: str, source_record_id: str) -> Optional[MappingRecord]:
        raise NotImplementedError

    def save_mapping(self, record: MappingRecord) -> None:
        raise NotImplementedError


class InMemoryMappingStore(MappingStore):
    def __init__(self):
        self._records: Dict[str, MappingRecord] = {}

    def get_mapping(self, source_id: str, source_record_id: str) -> Optional[MappingRecord]:
        return self._records.get(f"{source_id}:{source_record_id}")

    def save_mapping(self, record: MappingRecord) -> None:
        self._records[f"{record.source_id}:{record.source_record_id}"] = record
