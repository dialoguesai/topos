from __future__ import annotations

from dataclasses import dataclass

from ..sources.base import RawRecord
from ..validation.base import ValidationResult
from .base import NormalizedRecord, Parser


@dataclass
class GrokParser(Parser):
    dataset_id: str

    def parse(self, raw: RawRecord) -> NormalizedRecord:
        return NormalizedRecord(record_id=raw.record_id, payload=raw.payload)

    def validate(self, record: RawRecord) -> ValidationResult:
        return ValidationResult(is_valid=True, errors=[], metadata={})

    def schema_id(self) -> str:
        return "grok.conversation.v1"
