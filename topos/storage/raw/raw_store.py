from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class RawFile:
    file_path: str
    metadata: Dict[str, str]


@dataclass(frozen=True)
class RawFileRef:
    file_id: str
    file_path: str


@dataclass(frozen=True)
class RawRecordRef:
    record_id: str


class RawStore:
    def write_file(self, file: RawFile) -> RawFileRef:
        raise NotImplementedError

    def write_record(self, record: Dict[str, str]) -> RawRecordRef:
        raise NotImplementedError
