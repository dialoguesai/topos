from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List

from ..state_machine import IngestionJob


@dataclass
class SQLiteTrigger:
    """Stub trigger for raw table writes."""

    def poll(self) -> Iterable[IngestionJob]:
        return []

    def enqueue_from_records(self, records: List[dict]) -> List[IngestionJob]:
        _ = records
        return []
