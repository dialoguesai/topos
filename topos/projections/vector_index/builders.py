from __future__ import annotations

from typing import Iterable

from .base import EmbeddingRow, ProjectionBuilder, ProjectionStatus


class VectorIndexBuilder(ProjectionBuilder):
    def build(self, embeddings: Iterable[EmbeddingRow]) -> ProjectionStatus:
        count = sum(1 for _ in embeddings)
        return ProjectionStatus(status="stub", count=count)
