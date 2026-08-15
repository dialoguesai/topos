"""WS/HTTP enrichment entrypoint parity tests."""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock

import pytest

from topos.api.enrichment import _process_enrichment_core
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up

# Imported HERE, at module scope, and deliberately not inside the tests below.
# topos.core.handlers re-exports get_db_connection by value (via .common), so
# whatever topos.core.state.get_db_connection happens to be at first-import time
# is what the package binds — permanently. A test that patches state and only
# then triggers that first import bakes its own lambda into the package;
# monkeypatch then "restores" the poisoned value it recorded, and every later
# test in the session gets this test's closed connection. That is what made
# tests/core/test_graph_cypher_handler.py fail with "SQLite objects created in
# a thread can only be used in that same thread" when run after this file.
import topos.core.handlers as _handlers_hub  # noqa: E402,F401
import topos.core.handlers.enrichment as _handlers_enrichment  # noqa: E402,F401
import topos.core.handlers.ingest as _handlers_ingest  # noqa: E402,F401


@pytest.mark.asyncio
async def test_ws_executor_uses_process_enrichment_core_with_signal(monkeypatch, tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "parity.db"), check_same_thread=False)
    apply_pipeline_jobs_v1_up(conn)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)

    expected = {
        "status": "ok",
        "messages_processed": 2,
        "records_created": {"signal_embeddings": 2},
        "signal_derivation": {"jobs_run": 1},
    }
    mock_core = AsyncMock(return_value=expected)
    monkeypatch.setattr("topos.api.enrichment._process_enrichment_core", mock_core)

    from topos.pipeline.job_runner import _execute_enrichment_process_source

    result = await _execute_enrichment_process_source(
        {
            "source_id": "browser_visits",
            "dataset_id": None,
            "job_names": ["embeddings"],
            "force_reprocess": False,
        }
    )
    assert result == expected
    mock_core.assert_awaited_once()
    assert mock_core.await_args.kwargs["include_signal"] is True


@pytest.mark.asyncio
async def test_enrichment_progress_reads_sqlite_not_memory(monkeypatch, tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "progress.db"), check_same_thread=False)
    apply_pipeline_jobs_v1_up(conn)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    monkeypatch.setattr(_handlers_hub, "get_db_connection", lambda: conn)
    monkeypatch.setattr(_handlers_ingest.hub, "get_db_connection", lambda: conn)
    monkeypatch.setattr(_handlers_enrichment.hub, "get_db_connection", lambda: conn)

    from topos.core.handlers.enrichment import handle_enrichment_progress
    from topos.pipeline.job_store import enqueue_job, update_job_progress

    job_id = enqueue_job(
        conn,
        kind="enrichment_process_source",
        payload={"source_id": "browser_visits"},
        job_id="progress-job-1",
    )
    update_job_progress(
        conn,
        job_id,
        {"status": "processing", "progress_percent": 42.0, "messages_total": 10},
    )

    response = await handle_enrichment_progress(
        {"id": "req-progress", "payload": {"job_id": job_id}}
    )
    assert response["status"] == "ok"
    assert response["payload"]["progress_percent"] == 42.0
    assert response["payload"]["messages_total"] == 10
