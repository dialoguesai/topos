from __future__ import annotations

import logging
import shutil
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Any

from .raw_store import RawFile, RawFileRef

logger = logging.getLogger("topos.storage.raw.file_store")

INGESTION_DIRNAME = "ingestion"


def active_ingestion_base() -> Path:
    """Where this Topos keeps its raw ingestion files — one answer, one code path.

    ``~/.topos`` holds the active Topos, and ``ingestion`` is on
    ``profiles.MOVE_ALLOWLIST``: this directory archives when you switch away and
    comes back when you switch in. A directory anywhere else belongs to no Topos —
    no switch carries it and no profile owns it — so nothing here goes looking for
    one. Readers that used to union in ``~/.topos_engine/ingestion`` surfaced raw
    records the active Topos did not have.
    """
    env_override = os.getenv("TOPOS_INGESTION_BASE_PATH")
    if env_override:
        return Path(env_override)
    return Path.home() / ".topos" / INGESTION_DIRNAME


@dataclass(frozen=True)
class RawFileStore:
    base_path: Path

    def __init__(self, base_path: Optional[Path] = None):
        object.__setattr__(self, "base_path", base_path or active_ingestion_base())
        self.base_path.mkdir(parents=True, exist_ok=True)

    def get_file_path(self, dataset_id: str, schema_id: str) -> Path:
        safe_dataset_id = dataset_id.replace(":", "_").replace("/", "_")
        safe_schema_id = schema_id.replace(".", "_").replace("/", "_")
        dataset_dir = self.base_path / safe_dataset_id
        dataset_dir.mkdir(parents=True, exist_ok=True)
        return dataset_dir / f"{safe_schema_id}.jsonl"

    def write_file(self, raw_file: RawFile) -> RawFileRef:
        destination = self.get_file_path(
            raw_file.metadata.get("dataset_id", "unknown"),
            raw_file.metadata.get("schema_id", "unknown"),
        )
        source_path = Path(raw_file.file_path)
        if source_path.resolve() == destination.resolve():
            return RawFileRef(file_id=destination.stem, file_path=str(destination))
        if destination.exists():
            backup = destination.with_suffix(".jsonl.backup")
            shutil.copy2(destination, backup)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(raw_file.file_path, destination)
        logger.info("Saved raw file: %s", destination)
        return RawFileRef(file_id=destination.stem, file_path=str(destination))

    def write_stream(self, dataset_id: str, schema_id: str, stream: Any) -> RawFileRef:
        """Write raw content from a stream, without holding it all in memory.

        Used when the source is a container: the ingestible member is streamed
        out of it rather than the container being copied. Copying instead put a
        1.4GB archive in this store under a .jsonl name, which then failed to
        decode as text — the size was the real defect and the decode error was
        how it announced itself.
        """
        destination = self.get_file_path(dataset_id, schema_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with open(destination, "wb") as out:
            while True:
                chunk = stream.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        logger.info("Saved raw file from stream: %s", destination)
        return RawFileRef(file_id=destination.stem, file_path=str(destination))

    def write_bytes(self, dataset_id: str, schema_id: str, payload: bytes) -> RawFileRef:
        destination = self.get_file_path(dataset_id, schema_id)
        if destination.exists():
            backup = destination.with_suffix(".jsonl.backup")
            shutil.copy2(destination, backup)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        logger.info("Saved raw file bytes: %s", destination)
        return RawFileRef(file_id=destination.stem, file_path=str(destination))

    def append_record(self, dataset_id: str, schema_id: str, record: dict) -> RawFileRef:
        destination = self.get_file_path(dataset_id, schema_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record))
            handle.write("\n")
        return RawFileRef(file_id=destination.stem, file_path=str(destination))

    def list_datasets(self) -> list[dict]:
        """List all datasets with their file stats."""
        datasets = []
        if not self.base_path.exists():
            return datasets
        for dataset_dir in self.base_path.iterdir():
            if not dataset_dir.is_dir():
                continue
            dataset_id = dataset_dir.name.replace("_", ":")
            total_size = 0
            message_count = 0
            schemas = []
            for file_path in dataset_dir.glob("*.jsonl"):
                if file_path.name.endswith(".backup"):
                    continue
                file_size = file_path.stat().st_size
                total_size += file_size
                schema_id = file_path.stem.replace("_", ".")
                # Count messages in file
                try:
                    with file_path.open("r", encoding="utf-8") as f:
                        for _ in f:
                            message_count += 1
                except Exception:
                    pass
                schemas.append({"schema_id": schema_id, "file_size": file_size})
            if total_size > 0 or message_count > 0:
                datasets.append({
                    "dataset_id": dataset_id,
                    "total_size": total_size,
                    "message_count": message_count,
                    "schemas": schemas,
                })
        return datasets
