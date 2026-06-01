from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class CanonicalRef:
    record_id: str


class CanonicalStore:
    def upsert(self, record: Dict[str, str]) -> CanonicalRef:
        raise NotImplementedError


class InMemoryCanonicalStore(CanonicalStore):
    def __init__(self):
        self._records: Dict[str, Dict[str, str]] = {}

    def upsert(self, record: Dict[str, str]) -> CanonicalRef:
        record_id = record.get("record_id") or record.get("message_id") or ""
        self._records[record_id] = record
        return CanonicalRef(record_id=record_id)
