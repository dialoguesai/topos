"""WS/HTTP enrichment entrypoint parity tests."""

from __future__ import annotations

import sqlite3
from unittest.mock import AsyncMock

import pytest

# Module scope on purpose. These used to be imported INSIDE the tests, after
# `monkeypatch.setattr("topos.core.state.get_db_connection", ...)` had already
# run. When this file was the first to import the handlers package in a process
# (`pytest tests/core`), `topos/core/handlers/__init__.py` re-exported the
# *patched* value, so the "original" monkeypatch saved for
# `handlers_hub.get_db_connection` was the test's own lambda — and undo put that
# lambda back for the rest of the session. test_graph_cypher_handler.py then
# reached a torn-down tmp DB through it and answered 503. Binding before any
# patch runs makes the saved original the real function.
import topos.core.handlers as handlers_hub
from topos.api.enrichment import _process_enrichment_core
from topos.core.handlers.enrichment import handle_enrichment_progress
from topos.pipeline.job_runner import _execute_enrichment_process_source
from topos.pipeline.job_store import enqueue_job, update_job_progress
from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up


@pytest.mark.asyncio
async def test_ws_executor_uses_process_enrichment_core_with_signal(monkeypatch, tmp_path) -> None:
    conn = sqlite3.connect(str(tmp_path / "parity.db"))
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
    conn = sqlite3.connect(str(tmp_path / "progress.db"))
    apply_pipeline_jobs_v1_up(conn)
    monkeypatch.setattr("topos.core.state.get_db_connection", lambda: conn)
    # handlers.ingest and handlers.enrichment both do `import topos.core.handlers
    # as hub`, so all three former patch targets were this one object; patch it
    # once.
    monkeypatch.setattr(handlers_hub, "get_db_connection", lambda: conn)

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
