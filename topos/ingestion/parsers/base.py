from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict

from ..sources.base import RawRecord
from ..validation.base import ValidationResult


@dataclass(frozen=True)
class NormalizedRecord:
    record_id: str
    payload: Dict[str, str]


class Parser:
    def parse(self, raw: RawRecord) -> NormalizedRecord:
        raise NotImplementedError

    def validate(self, record: RawRecord) -> ValidationResult:
        raise NotImplementedError

    def schema_id(self) -> str:
        raise NotImplementedError
