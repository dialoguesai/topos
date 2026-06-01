"""Checkpoint store contract for ingestion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional


@dataclass(frozen=True)
class IngestionCheckpoint:
    dataset_id: str
    schema_id: str
    last_record_id: str
    metadata: Dict[str, str]


class CheckpointStore:
    """Persist and retrieve ingestion checkpoints."""

    def get_checkpoint(self, dataset_id: str, schema_id: str) -> Optional[IngestionCheckpoint]:
        raise NotImplementedError

    def save_checkpoint(self, checkpoint: IngestionCheckpoint) -> None:
        raise NotImplementedError
