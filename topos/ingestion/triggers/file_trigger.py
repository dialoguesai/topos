from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from ..state_machine import IngestionJob
from ...storage.raw.file_store import RawFileStore
from ...storage.raw.raw_store import RawFile


def _job_metadata(file_format: str, ingest_options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Import-time policy rides on the job so a replay reproduces the corpus."""
    metadata: Dict[str, Any] = {"file_format": file_format}
    if isinstance(ingest_options, dict) and ingest_options:
        metadata["ingest_options"] = ingest_options
    return metadata


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
        ingest_options: Optional[Dict[str, Any]] = None,
    ) -> IngestionJob:
        raw_file = RawFile(file_path=file_path, metadata={"dataset_id": dataset_id, "schema_id": schema_id})
        self.file_store.write_file(raw_file)
        return IngestionJob(
            job_id=job_id,
            dataset_id=dataset_id,
            schema_id=schema_id,
            metadata=_job_metadata(file_format, ingest_options),
        )

    def create_job_from_bytes(
        self,
        job_id: str,
        dataset_id: str,
        schema_id: str,
        payload: bytes,
        file_format: str = "jsonl",
        ingest_options: Optional[Dict[str, Any]] = None,
    ) -> IngestionJob:
        self.file_store.write_bytes(dataset_id, schema_id, payload)
        return IngestionJob(
            job_id=job_id,
            dataset_id=dataset_id,
            schema_id=schema_id,
            metadata=_job_metadata(file_format, ingest_options),
        )
