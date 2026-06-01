from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from ..state_machine import IngestionJob
from ...storage.raw.file_store import RawFileStore
from ...storage.raw.raw_store import RawFile


@dataclass
class FileTrigger:
    file_store: RawFileStore

    def create_job(
        self,
        job_id: str,
        dataset_id: str,
        schema_id: str,
        file_path: str,
        file_format: str = "jsonl",
    ) -> IngestionJob:
        raw_file = RawFile(file_path=file_path, metadata={"dataset_id": dataset_id, "schema_id": schema_id})
        self.file_store.write_file(raw_file)
        return IngestionJob(job_id=job_id, dataset_id=dataset_id, schema_id=schema_id, metadata={"file_format": file_format})

    def create_job_from_bytes(
        self,
        job_id: str,
        dataset_id: str,
        schema_id: str,
        payload: bytes,
        file_format: str = "jsonl",
    ) -> IngestionJob:
        self.file_store.write_bytes(dataset_id, schema_id, payload)
        return IngestionJob(job_id=job_id, dataset_id=dataset_id, schema_id=schema_id, metadata={"file_format": file_format})
