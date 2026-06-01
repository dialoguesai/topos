from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class OplogEntry:
    op_id: str
    dataset_id: str
    op_type: str
    payload: Dict[str, str]


class OplogStore:
    def append(self, entry: OplogEntry) -> None:
        raise NotImplementedError
