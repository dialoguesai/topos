"""Regression guard for the start_ingestion handler's payload parsing.

resolve_file_format() must run only after file_path/source_definition are
read from the payload; a bad ordering raised UnboundLocalError on every
start_ingestion dispatch before any validation could respond.
"""
from __future__ import annotations

import pytest

from topos.core.handlers import handle_control_plane_request


@pytest.mark.asyncio
async def test_start_ingestion_without_job_id_returns_error_response():
    result = await handle_control_plane_request(
        {
            "id": "req-ingest-1",
            "type": "start_ingestion",
            "payload": {"dataset_id": "user-x_dataset-y"},
        }
    )
    assert result == {
        "id": "req-ingest-1",
        "status": "error",
        "error": "job_id required",
    }
