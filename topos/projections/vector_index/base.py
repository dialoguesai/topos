from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List


@dataclass(frozen=True)
class EmbeddingRow:
    record_id: str
    vector: List[float]


@dataclass(frozen=True)
class ProjectionStatus:
    status: str
    count: int


class ProjectionBuilder:
    def build(self, embeddings: Iterable[EmbeddingRow]) -> ProjectionStatus:
        raise NotImplementedError
