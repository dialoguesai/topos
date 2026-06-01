from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Literal


@dataclass(frozen=True)
class SourcePayload:
    payload: Dict[str, str]


@dataclass(frozen=True)
class RawRecord:
    record_id: str
    payload: Dict[str, str]


@dataclass(frozen=True)
class SourceIdentity:
    source_system: str
    source_record_id: str
    source_export_id: str


class SourceConnector:
    source_name: str
    source_type: Literal["file", "sqlite"]

    def ingest(self, payload: SourcePayload) -> str:
        raise NotImplementedError

    def schema(self) -> Dict[str, str]:
        raise NotImplementedError

    def identity(self, record: RawRecord) -> SourceIdentity:
        raise NotImplementedError

    def canonical_eligible(self) -> bool:
        raise NotImplementedError
