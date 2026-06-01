"""Job entry points for ingestion processing."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

import httpx
from google.cloud import storage

from .manager import IngestionManager
from .state_machine import IngestionJob
from ..storage.raw.file_store import RawFileStore
from ..uma_rpt import get_control_plane_http_base

logger = logging.getLogger("topos.ingestion.jobs")


def _download_ingestion_bytes(file_url: str) -> bytes:
    url = (file_url or "").strip()
    if not url:
        raise ValueError("FILE_URL is empty")
    if url.startswith("gs://"):
        rest = url.removeprefix("gs://")
        bucket_name, _, blob_name = rest.partition("/")
        if not bucket_name or not blob_name:
            raise ValueError(f"Invalid gs:// URL: {url!r}")
        client = storage.Client()
        return client.bucket(bucket_name).blob(blob_name).download_as_bytes()
    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


async def main() -> int:
    logging.basicConfig(
        level=os.getenv("TOPOS_LOG_LEVEL", "INFO"),
        format="%(levelname)s %(name)s: %(message)s",
    )

    job_id = os.getenv("JOB_ID")
    schema_id = os.getenv("SCHEMA_ID")
    dataset_id = os.getenv("DATASET_ID")
    file_format = os.getenv("FILE_FORMAT", "jsonl")
    file_url = (os.getenv("FILE_URL") or "").strip()
    source_id = (os.getenv("SOURCE_ID") or "").strip() or None
    topos_key = (os.getenv("TOPOS_KEY") or "").strip() or None

    if not all([job_id, schema_id, dataset_id]):
        logger.error("Missing JOB_ID, SCHEMA_ID, or DATASET_ID")
        return 1

    file_store = RawFileStore()
    if file_url:
        logger.info("Downloading ingestion input from FILE_URL for job_id=%s", job_id)
        data = await asyncio.to_thread(_download_ingestion_bytes, file_url)
        file_store.write_bytes(dataset_id, schema_id, data)
    else:
        logger.info(
            "No FILE_URL set; expecting raw file under %s (dataset=%s schema=%s)",
            file_store.base_path,
            dataset_id,
            schema_id,
        )

    manager = IngestionManager(file_store=file_store)
    job = IngestionJob(
        job_id=job_id,
        dataset_id=dataset_id,
        schema_id=schema_id,
        metadata={"file_format": file_format},
    )
    progress_base = get_control_plane_http_base()
    await manager.process_job(
        job,
        source_id=source_id,
        progress_api_url=progress_base,
        progress_api_key=topos_key,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as exc:
        logging.getLogger("topos.ingestion.jobs").exception("Ingestion job failed: %s", exc)
        sys.exit(1)
