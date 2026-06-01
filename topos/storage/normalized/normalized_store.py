from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class NormalizedRef:
    record_id: str


class NormalizedStore:
    def write(self, record: Dict[str, str]) -> NormalizedRef:
        raise NotImplementedError


class InMemoryNormalizedStore(NormalizedStore):
    def __init__(self):
        self._records: Dict[str, Dict[str, str]] = {}

    def write(self, record: Dict[str, str]) -> NormalizedRef:
        record_id = record.get("record_id") or record.get("message_id") or ""
        self._records[record_id] = record
        return NormalizedRef(record_id=record_id)
